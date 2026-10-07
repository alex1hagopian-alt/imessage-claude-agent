# The always-on agent: reads your iMessages, runs confirmed terminal commands, and starts/stops Claude Code sessions.
import os
import re
import time
import unicodedata
from datetime import datetime
import sqlite3
import json
import signal
import subprocess
import sys
from collections import deque
import anthropic

import config
import imessage_common
from imessage_common import (AGENT_ADDRESS, REPLY_ADDRESS, REPLY_CHAT_GUID, SENT_TEXTS, WATCH_IDENTIFIERS,
                              is_part_label, normalize, reply_target, send_imessage,
                              set_reply_target, truncate_output)

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None, timeout=60)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CHAT_DB = config.CHAT_DB
# Messages from the iPhone land in a different Mac-side chat depending on
# which address they were sent to, so every query below watches all of these.
CHAT_IDENTIFIERS = WATCH_IDENTIFIERS
# Written by morning_brief.py right before it sends the brief
BRIEF_TEXT_FILE = '/tmp/brief_text.txt'
# Present while claude_code_bridge.py owns the chat; this agent stays quiet
BRIDGE_LOCK_FILE = '/tmp/claude_bridge.lock'
# Written by the bridge (pid, project, start time, last text it sent) for "status"
BRIDGE_STATE_FILE = '/tmp/claude_bridge_state.json'
AGENT_STARTED = time.time()
POLL_SECONDS = 2
# Messages sync in from iCloud with old dates but new rowids, so rowid alone
# can't tell "arrived after startup" — also require the message date to be
# after this process started (Apple epoch, nanoseconds).
STARTUP_DATE_NS = int((time.time() - 978307200) * 1_000_000_000)
# Prefixes of texts that come from this agent or claude_code_bridge.py. The two
# run as separate processes, so neither sees the other's SENT_TEXTS.
AUTOMATED_PREFIXES = ('[claude code ·', "i'd like to run:", 'running: ',
                      'done. output:', 'agent online', 'got it — cancelled',
                      'claude code is already running', 'starting claude code',
                      'claude code session ended', 'no active claude code session',
                      'not a directory:', 'created new project:',
                      'projects in ', 'no projects yet',
                      'which project?', 'imessage agent: running', 'restarting agent',
                      "i can't restart myself", 'restart failed', 'pressed return',
                      'pressed space', 'pressed escape', "i can't run that",
                      'this one needs confirm', 'i need accessibility access',
                      "couldn't press", '📝 memory for ', 'no memory found for ', 'memory cleared for ')
# morning_brief.py texts its brief to this same chat at 07:00 (see its launchd
# plist). The brief is generated, so its wording varies; match it three ways.
BRIEF_HOUR, BRIEF_MINUTE = config.BRIEF_HOUR, config.BRIEF_MINUTE  # when the morning brief is sent (BRIEF_TIME)
BRIEF_WINDOW_SECONDS = 60
# Wording only counts when the message OPENS like a brief (after stripping any
# leading emoji/markdown) and is long. Matching a phrase anywhere swallowed real
# tasks like "…the news section of this morning brief script…".
BRIEF_OPENERS = ('good morning', "here's your", 'here’s your')
BRIEF_OPENER_MIN_CHARS = 200
BRIDGE_SCRIPT = os.path.join(PROJECT_DIR, 'claude_code_bridge.py')
BRIDGE_LOG = os.path.join(PROJECT_DIR, 'bridge.log')
PROJECTS_DIR = config.PROJECTS_DIR
# Created by "Stop AI Agent.app"; while it exists this agent stays idle
DISABLED_FLAG = os.path.join(PROJECT_DIR, 'AGENT_DISABLED')
START_RE = re.compile(r'^\s*start\s+claude(?:\s+(.+?))?\s*$', re.IGNORECASE)
STOP_RE = re.compile(r'^\s*stop\s+claude\s*$', re.IGNORECASE)
LIST_RE = re.compile(r'^\s*list\s+projects\s*$', re.IGNORECASE)
# "status" / "update" requests. Deliberately narrow: only short messages, "update"
# only as a noun ("an update", "status update"), never "git status" and never
# something that opens like an instruction, because real tasks such as
# "update the readme" or "add a status column" must reach Claude Code intact.
STATUS_MAX_WORDS = 8
STATUS_RE = re.compile(r'\bstatus\b', re.IGNORECASE)
UPDATE_RE = re.compile(r'^\W*updates?\W*$|\bupdate\s+me\b'
                       r'|\b(an|any|the|quick|latest|status|progress|some|no|your)\s+updates?\b',
                       re.IGNORECASE)
GIT_STATUS_RE = re.compile(r'\bgit\s+status\b', re.IGNORECASE)
# Messages that open like an instruction are tasks, even if they mention "status"
TASK_START_RE = re.compile(
    r'^\W*(add|fix|change|make|build|create|write|implement|remove|delete|rename|refactor|'
    r'modify|move|use|set|display|update\s+(the|my|our|this|that|it|all|these|those))\b',
    re.IGNORECASE)
# "press enter" etc.: keystrokes sent to the frontmost app via System Events
PRESS_RE = re.compile(r'^\s*(?:press|hit)\s+(enter|return|space|escape|esc)\s*[.!]?\s*$',
                      re.IGNORECASE)
KEY_CODES = {'enter': (36, 'Return'), 'return': (36, 'Return'),
             'space': (49, 'Space'), 'escape': (53, 'Escape'), 'esc': (53, 'Escape')}
# Without Accessibility access, a launchd-run osascript doesn't fail — it hangs
# on a permission prompt — so a timeout is part of the failure path.
KEY_TIMEOUT = 10
ACCESSIBILITY_MSG = ("I need Accessibility access. Go to System Settings → Privacy & "
                     "Security → Accessibility and add Python.")
PERMISSION_ERROR_RE = re.compile(r'1002|-25211|-1743|not allowed|assistive|not authorized',
                                 re.IGNORECASE)
RESTART_RE = re.compile(r'^\s*restart\s+agent\s*$', re.IGNORECASE)
AGENT_LABEL = config.LAUNCHD_LABEL
MEMORY_RE = re.compile(r'^\s*memory\s+(\S+)\s*$', re.IGNORECASE)
CLEAR_MEMORY_RE = re.compile(r'^\s*clear\s+memory\s+(\S+)\s*$', re.IGNORECASE)
MAX_MEMORY_CHARS = 20000   # sanity cap only; longer files are sent as several parts
# "stop claude": how long the bridge gets to update CLAUDE.md and exit on its
# own before we fall back to SIGTERM/SIGKILL
STOP_GRACE_SECONDS = 360
BRIDGE_PROC = None  # Popen handle when this agent launched the bridge
# How many polls to wait for a row's body to be written before skipping it
MAX_EMPTY_POLLS = 5

PENDING_ACTION = {}
# Set when "start claude" arrives with no project: the next message is the name
PENDING_PROJECT_UNTIL = 0.0
PROJECT_REPLY_TIMEOUT = 5 * 60
# SENT_TEXTS (what this process has sent, for echo filtering) lives in imessage_common.

SYSTEM_PROMPT = """You are a personal assistant that helps the user control their Mac Mini via iMessage.

You can propose ONE terminal command to fulfill the user's request.

Respond in this exact format:
COMMAND: <the terminal command to run>
EXPLANATION: <one sentence explaining what this will do>

If the request is unclear or you cannot help with a terminal command, respond with:
CANNOT_HELP: <brief explanation>

IGNORE AUTOMATED MESSAGES: This thread also receives automated messages that are not requests from the user. If the message is a weather brief, a news summary or list of headlines, a "Good morning" / daily briefing, anything that looks like it was produced by the morning_brief.py script, or any other automated notification or bot output (including your own earlier replies such as "I'd like to run:", "Running:", "Done. Output:", or "Agent online"), respond with exactly:
IGNORE
and nothing else. Do not explain or comment on it.

Be conservative. Only propose safe, reversible commands. Never propose commands that delete files or make system changes without being obvious about it."""


def connect_db():
    return sqlite3.connect(f'file:{CHAT_DB}?mode=ro', uri=True)


def parse_attributed_body(data):
    # attributedBody is a typedstream; the message text follows the first
    # NSString marker as a length-prefixed UTF-8 string.
    try:
        i = data.index(b'NSString')
        i = data.index(b'+', i) + 1
        length = data[i]
        i += 1
        if length == 0x81:
            length = int.from_bytes(data[i:i + 2], 'little')
            i += 2
        elif length == 0x82:
            length = int.from_bytes(data[i:i + 3], 'little')
            i += 3
        text = data[i:i + length].decode('utf-8', errors='ignore').strip()
        return text or None
    except (ValueError, IndexError):
        return None


def get_latest_rowid():
    # Deliberately not filtered by chat: this is only the "start after
    # everything that already exists" cursor, not a read of any messages.
    db = connect_db()
    row = db.execute('SELECT MAX(rowid) FROM message').fetchone()
    db.close()
    return row[0] or 0


def get_messages_after(last_rowid):
    """New messages in EVERY watched thread (CHAT_IDENTIFIERS), oldest first, as
    (rowid, attributedBody, text, is_from_me, date, chat_identifier). The chat_identifier
    says which of the user's addresses they texted from, so the reply can go back to it.
    Only messages RECEIVED by the agent's own Apple ID (destination_caller_id =
    AGENT_ADDRESS, is_from_me = 0) count: with a separate Apple ID is_from_me is reliable,
    and the user's old text-yourself threads on this Mac are ignored entirely.
    This is the only query that reads messages; the agent and the bridge both use it."""
    db = connect_db()
    cursor = db.cursor()
    placeholders = ', '.join('?' for _ in CHAT_IDENTIFIERS)
    cursor.execute(f'''
        SELECT DISTINCT m.rowid, m.attributedBody, m.text, m.is_from_me, m.date, c.chat_identifier
        FROM message m
        JOIN chat_message_join cmj ON m.rowid = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.rowid
        WHERE c.chat_identifier IN ({placeholders})
        AND m.destination_caller_id = ?
        AND m.is_from_me = 0
        AND m.rowid > ?
        AND m.date > ?
        AND m.item_type = 0
        AND m.associated_message_type = 0
        ORDER BY m.rowid ASC
    ''', (*CHAT_IDENTIFIERS, AGENT_ADDRESS, last_rowid, STARTUP_DATE_NS))
    rows = cursor.fetchall()
    db.close()
    return rows


def parse_message(row):
    rowid, attributed_body, text, is_from_me = row[:4]
    if text and text.strip():
        return text.strip()
    if attributed_body:
        return parse_attributed_body(attributed_body)
    return None


def in_brief_window(date_ns):
    """True if a message's own timestamp is within BRIEF_WINDOW_SECONDS of 07:00."""
    if not date_ns:
        return False
    sent = datetime.fromtimestamp(date_ns / 1_000_000_000 + 978307200)
    brief = sent.replace(hour=BRIEF_HOUR, minute=BRIEF_MINUTE, second=0, microsecond=0)
    return abs((sent - brief).total_seconds()) <= BRIEF_WINDOW_SECONDS


def brief_reason(msg, date_ns=None):
    """Why this looks like morning_brief.py's output, or None."""
    if msg.lstrip().startswith('# '):  # the brief is written in markdown
        return 'starts with "# "'
    if in_brief_window(date_ns):
        return 'sent within a minute of 07:00'
    opener = re.sub(r'^[^a-z0-9]+', '', msg.lower())
    if len(msg) >= BRIEF_OPENER_MIN_CHARS and opener.startswith(BRIEF_OPENERS):
        return 'opens like a morning brief'
    return None


def is_morning_brief(msg, date_ns=None):
    return brief_reason(msg, date_ns) is not None


def own_message_reason(msg, date_ns=None):
    """Why `msg` is not from the user (our own echo, the bridge, the brief), or None."""
    key = normalize(msg)
    if key in SENT_TEXTS:
        return 'echo of a message this process sent'
    if is_part_label(msg):
        return 'part of a split agent/bridge message'
    if key.startswith(AUTOMATED_PREFIXES):
        return 'looks like agent/bridge output'
    try:
        with open(BRIEF_TEXT_FILE, encoding='utf-8') as f:
            if normalize(f.read()) == key:
                return 'matches the saved morning brief text'
    except OSError:
        pass
    reason = brief_reason(msg, date_ns)
    return f'morning brief: {reason}' if reason else None


def is_own_message(msg, date_ns=None):
    return own_message_reason(msg, date_ns) is not None


def is_agent_command(msg):
    """Commands for this agent that a running bridge session must not relay."""
    return (any(r.match(msg) for r in (START_RE, LIST_RE, MEMORY_RE, CLEAR_MEMORY_RE, RESTART_RE, PRESS_RE))
            or is_status_request(msg))


def is_status_request(msg):
    words = msg.split()
    if (not words or len(words) > STATUS_MAX_WORDS or GIT_STATUS_RE.search(msg)
            or TASK_START_RE.match(msg)):
        return False
    return bool(STATUS_RE.search(msg) or UPDATE_RE.search(msg))


def bridge_pid():
    """PID of the running bridge (from its lock file), or None."""
    if BRIDGE_PROC is not None:
        BRIDGE_PROC.poll()  # reap it if it exited, so a zombie isn't "alive"
    try:
        with open(BRIDGE_LOCK_FILE) as f:
            pid = int(f.read().strip())
        if BRIDGE_PROC is not None and BRIDGE_PROC.pid == pid and BRIDGE_PROC.returncode is not None:
            return None
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        # No lock yet: a bridge we just launched is still starting up
        if BRIDGE_PROC is not None and BRIDGE_PROC.poll() is None:
            return BRIDGE_PROC.pid
        return None


def bridge_active():
    # The lock file appears a moment after launch; the handle covers that gap
    return bridge_pid() is not None or (
        BRIDGE_PROC is not None and BRIDGE_PROC.poll() is None)


# ---- Command safety ---------------------------------------------------------
# A pattern guard over the commands Claude proposes; it is not a sandbox (see
# CLAUDE.md). BLOCKED commands are refused outright, whatever the user replies.
# RISKY ones need the exact word CONFIRM instead of YES.
BLOCKED_LOG = os.path.join(PROJECT_DIR, 'blocked.log')
BLOCKED_MESSAGE = ("I can't run that — it matches a blocked pattern. If you really "
                   "need this, run it manually in terminal.")
CONFIRM_ENDING = ("Reply CONFIRM (all caps) to proceed or NO to cancel — this "
                  "command requires extra confirmation.")
CONFIRM_HINT = "This one needs CONFIRM in all caps to proceed (or NO to cancel)."

_SEGMENT_SPLIT = re.compile(r'\s*(?:;|&&|\|\||\||&)\s*')
# (name, regex) run against the normalized command
_BLOCKED_REGEXES = [
    ('sudo rm', re.compile(r'(?<![\w.-])sudo\b[^;&|]*(?<![\w.-])rm\b')),
    ('mkfs', re.compile(r'(?<![\w.-])(?:mkfs|newfs)(?:[._]\w+)?\b')),
    ('diskutil erase', re.compile(r'(?<![\w.-])diskutil\s+(?:\S+\s+)*?(?:erase\w*|reformat|'
                                  r'zerodisk|randomdisk|secureerase|partitiondisk|repartitiondisk)\b')),
    ('dd if=', re.compile(r'(?<![\w.-])dd\s+(?:[^;&|]*\s)?if=')),
    # "format" as a word; not --format=..., .format(...), or a path component
    ('format', re.compile(r'(?<![-=\w./])format(?![=.\w/-])')),
    ('kill -9', re.compile(r'(?<![\w.-])(?:kill|pkill|killall)\b[^;&|]*\s-(?:9|kill|sigkill)\b')),
    ('kill -9', re.compile(r'(?<![\w.-])(?:kill|pkill|killall)\b[^;&|]*\s-s\s+(?:9|kill|sigkill)\b')),
    # writing to a device; /dev/null, stdout, stderr and tty are harmless
    ('> /dev/', re.compile(r'>>?\s*/dev/(?!(?:null|stdout|stderr|tty)(?![\w/]))')),
    ('pipe to shell', re.compile(r'\|\s*(?:sudo\s+(?:-\S+\s+)*)?(?:env\s+(?:\S+=\S+\s+)*)?'
                                 r'(?:/\S+/)?(?:sh|bash|zsh|dash|ksh)\b')),
    ('remote script execution', re.compile(r'(?:sh|bash|zsh)\s+(?:-\S+\s+)*<\(\s*(?:curl|wget)')),
    ('remote script execution', re.compile(r'(?:eval|source)\s+[^;&|]*\$\(\s*(?:curl|wget)')),
    ('remote script execution', re.compile(r'-c\s+\$\(\s*(?:curl|wget)')),
]
_RISKY_REGEXES = [
    ('sudo', re.compile(r'(?<![\w.-])sudo\b')),
    ('pip install', re.compile(r'(?<![\w.-])(?:pip3?|pipx)\s+(?:-\S+\s+)*install\b')),
    ('pip install', re.compile(r'-m\s+pip3?\s+(?:-\S+\s+)*install\b')),
    ('npm install', re.compile(r'(?<![\w.-])npm\s+(?:-\S+\s+)*(?:install|i|add|ci)\b')),
    ('~/.ssh or ~/.aws', re.compile(r'(?<![\w-])\.(?:ssh|aws)(?![\w-])')),
    ('.env file', re.compile(r'(?<![\w-])\.env(?:\.[\w.-]+)?(?![\w-])')),
]
_OVERWRITE_RE = re.compile(r'(?<![>&=-])\d?>(?![>&(])\s*([^\s;&|<>]*)')
_HARMLESS_TARGETS = ('/dev/null', '/dev/stdout', '/dev/stderr', '/dev/tty')


def _normalize_command(command):
    """Lowercase, one-line, quotes/backslashes removed, so that `"rm"  -fr`,
    `rm\\ -rf` and `RM -rf` all look like `rm -fr`."""
    text = command.replace('\\\n', ' ')
    text = re.sub(r'[\'"\\]', '', text)
    text = text.replace('\n', ' ; ').replace('\r', ' ')
    return ' '.join(text.lower().split())


def _invocations(normalized, name):
    """The argument list of every `name` command (also /bin/name) in the line."""
    for segment in _SEGMENT_SPLIT.split(normalized):
        words = segment.split()
        for i, word in enumerate(words):
            if word == name or word.endswith('/' + name):
                yield words[i + 1:]


def _split_args(args):
    """(short flag letters, long flags, non-flag arguments)."""
    flags = [a for a in args if a.startswith('-') and a not in ('-', '--')]
    short = ''.join(f[1:] for f in flags if not f.startswith('--'))
    return short, [f for f in flags if f.startswith('--')], [a for a in args if not a.startswith('-')]


def blocked_pattern(command):
    """Name of the blocked pattern `command` matches, else None."""
    n = _normalize_command(command)
    for args in _invocations(n, 'rm'):
        short, long_flags, targets = _split_args(args)
        recursive = 'r' in short or '--recursive' in long_flags
        if recursive and ('f' in short or '--force' in long_flags):
            return 'rm -rf'
        if recursive and any(t.startswith('/') or t in ('~', '~/', '$home', '$home/')
                             for t in targets):
            return 'rm -r /'
    for args in _invocations(n, 'chmod'):
        short, long_flags, targets = _split_args(args)
        if ('r' in short or '--recursive' in long_flags) and any(
                re.fullmatch(r'0?777', t) for t in targets):
            return 'chmod -R 777'
    for args in _invocations(n, 'chown'):
        short, long_flags, _ = _split_args(args)
        if 'r' in short or '--recursive' in long_flags:
            return 'chown -R'
    for name, regex in _BLOCKED_REGEXES:
        if regex.search(n):
            return name
    compact = n.replace(' ', '')
    if ':(){:|:&};:' in compact or re.search(r'(\w+)\(\)\{[^}]*\1\|\1&', compact):
        return 'fork bomb'
    return None


def risky_pattern(command):
    """Name of the pattern that makes `command` need CONFIRM, else None."""
    n = _normalize_command(command)
    for name, regex in _RISKY_REGEXES:
        if regex.search(n):
            return name
    if any(True for _ in _invocations(n, 'rm')):
        return 'rm'
    for match in _OVERWRITE_RE.finditer(n):
        if match.group(1) not in _HARMLESS_TARGETS:
            return '> overwrite'
    return None


def log_blocked(command, pattern, requested_by=''):
    """Audit trail: ~/projects/infrastructure/blocked.log, one JSON-quoted line each."""
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} BLOCKED [{pattern}] "
            f"command={json.dumps(command, ensure_ascii=False)} "
            f"requested_by={json.dumps(requested_by[:300], ensure_ascii=False)}\n")
    print(f"Blocked command [{pattern}]: {command!r}")
    try:
        with open(BLOCKED_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except OSError as e:
        print(f"could not write {BLOCKED_LOG}: {e}")


def run_terminal_command(command):
    # Last line of defence: nothing blocked ever reaches the shell, whatever
    # the caller or the user's reply said.
    pattern = blocked_pattern(command)
    if pattern:
        log_blocked(command, pattern, 'refused at execution')
        return BLOCKED_MESSAGE
    try:
        result = subprocess.run(command, shell=True,
                                capture_output=True, text=True, timeout=120,
                                cwd=PROJECT_DIR)
    except subprocess.TimeoutExpired:
        return 'Command timed out after 120 seconds'
    output = result.stdout or result.stderr or 'Command completed with no output'
    return truncate_output(output)  # long output: head + tail with a note


def normalize_project_name(text):
    """Turn whatever the user said into a folder name: "Trading Bot",
    "trading_bot" and "trading bot!" all become "trading-bot". May return ''
    if nothing usable is left (e.g. "???")."""
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    text = re.sub(r'[\s_]+', '-', text.strip().lower())  # spaces/underscores -> hyphen
    text = re.sub(r'[^a-z0-9-]', '', text)               # strip everything else
    return re.sub(r'-{2,}', '-', text).strip('-')[:64].strip('-')


def find_project_folder(norm):
    """An existing visible ~/projects folder whose normalized name is `norm`,
    so a hand-made "Video Game" folder still matches "video game"."""
    if not norm or not os.path.isdir(PROJECTS_DIR):
        return None
    matches = sorted(e for e in os.listdir(PROJECTS_DIR)
                     if not e.startswith('.')
                     and os.path.isdir(os.path.join(PROJECTS_DIR, e))
                     and normalize_project_name(e) == norm)
    return norm if norm in matches else (matches[0] if matches else None)


EMPTY_NAME = 'empty-name'


def resolve_project(arg):
    """Map "start claude <arg>" to (folder, created, error).

    A path (starts with "/" or "~") is used as given and never created.
    Anything else is a project name: it is normalized, then opened if
    ~/projects/<name> exists, or created if not. Names are never rejected;
    the only failure is a name with nothing usable left (EMPTY_NAME).
    """
    arg = (arg or '').strip()
    if arg.startswith(('/', '~')):
        return os.path.abspath(os.path.expanduser(arg)), False, None
    norm = normalize_project_name(arg)
    if not norm:
        return None, False, EMPTY_NAME
    existing = find_project_folder(norm)
    if existing:
        return os.path.join(PROJECTS_DIR, existing), False, None
    return os.path.join(PROJECTS_DIR, norm), True, None


def list_projects():
    names = []
    if os.path.isdir(PROJECTS_DIR):
        names = sorted((e for e in os.listdir(PROJECTS_DIR)
                        if not e.startswith('.')
                        and os.path.isdir(os.path.join(PROJECTS_DIR, e))),
                       key=str.lower)
    if not names:
        send_imessage("No projects yet. Say \"start claude <name>\" to create one.")
        return
    send_imessage("Projects in ~/projects:\n" + '\n'.join(f"- {n}" for n in names)
                  + "\n\nSay \"start claude <name>\" to open one.")


def ask_for_project():
    global PENDING_PROJECT_UNTIL
    PENDING_PROJECT_UNTIL = time.time() + PROJECT_REPLY_TIMEOUT
    send_imessage("Which project? Reply with a name. I'll open it if it exists "
                  "or create it if it doesn't.")


def start_bridge(path_arg):
    global BRIDGE_PROC
    if bridge_active():
        send_imessage("Claude Code is already running.")
        return
    if not (path_arg or '').strip():
        return ask_for_project()
    project, created, error = resolve_project(path_arg)
    if error == EMPTY_NAME:
        return ask_for_project()  # nothing usable in that name: just ask again
    if created:
        try:
            os.makedirs(project, exist_ok=True)
        except OSError as e:
            send_imessage(f"Couldn't create {os.path.basename(project)}: {e.strerror}")
            return
        send_imessage(f"Created new project: {os.path.basename(project)}. Starting Claude Code...")
    elif not os.path.isdir(project):
        send_imessage(f"Not a directory: {project}")
        return
    else:
        send_imessage("Starting Claude Code...")
    log = open(BRIDGE_LOG, 'a')
    # New session so the bridge survives this agent and can be killed as a group
    BRIDGE_PROC = subprocess.Popen(
        [sys.executable, BRIDGE_SCRIPT, project], cwd=PROJECT_DIR,
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def wait_for_bridge_exit(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if bridge_pid() is None:  # also reaps our child, so no zombie
            return True
        time.sleep(0.2)
    return bridge_pid() is None


def stop_bridge():
    pid = bridge_pid()
    if pid is None:
        send_imessage("No active Claude Code session.")
        return
    # The bridge reads the same "stop claude" text and, before exiting, asks
    # Claude Code to update CLAUDE.md, so give it time instead of killing it.
    # (The kill-switch app sends SIGTERM directly and skips that on purpose.)
    # A bridge with no lock file yet is still starting and isn't reading
    # messages, so waiting for it would only burn the whole grace period.
    if not (os.path.exists(BRIDGE_LOCK_FILE) and wait_for_bridge_exit(STOP_GRACE_SECONDS)):
        try:
            os.kill(pid, signal.SIGTERM)  # the bridge shuts Claude Code down cleanly
        except ProcessLookupError:
            pass
    if not wait_for_bridge_exit(10):
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        if not wait_for_bridge_exit(5):
            send_imessage("Could not stop Claude Code — kill process "
                          f"{pid} manually.")
            return
    try:
        os.remove(BRIDGE_LOCK_FILE)
    except OSError:
        pass
    send_imessage("Claude Code session ended.")


def project_memory_path(name):
    """(display_name, CLAUDE.md path or None) for the project `name` refers to."""
    norm = normalize_project_name(name)
    folder = find_project_folder(norm) or norm
    if not folder:
        return name, None
    return folder, os.path.join(PROJECTS_DIR, folder, 'CLAUDE.md')


def show_memory(name):
    name, path = project_memory_path(name)
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            content = f.read().strip()
    except (OSError, TypeError):
        content = ''
    if not content:
        return send_imessage(f"No memory found for {name} yet.")
    if len(content) > MAX_MEMORY_CHARS:
        content = (content[:MAX_MEMORY_CHARS]
                   + f"\n\n…(cut off, {len(content) - MAX_MEMORY_CHARS} more characters)")
    send_imessage(f"📝 Memory for {name}:\n\n{content}")


def clear_memory(name):
    name, path = project_memory_path(name)
    try:
        os.remove(path)
    except (FileNotFoundError, TypeError):
        return send_imessage(f"No memory found for {name} yet.")
    except OSError as e:
        return send_imessage(f"Could not clear memory for {name}: {e.strerror}")
    send_imessage(f"Memory cleared for {name}.")


def format_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def read_bridge_state(pid):
    """The bridge's state file, but only if it belongs to the live process `pid`."""
    try:
        with open(BRIDGE_STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
        return state if state.get('pid') == pid else None
    except (OSError, ValueError):
        return None


def status_report():
    now = time.time()
    # Names the destination THIS process loaded, so a stale process (started
    # before an address change) is visible instead of silently texting elsewhere.
    lines = [f"iMessage agent: running (up {format_duration(now - AGENT_STARTED)}), "
             f"sending to {reply_target()[0]}"]
    pid = bridge_pid()
    if pid is None:
        lines.append("Claude Code: no active session.")
        return '\n'.join(lines)
    state = read_bridge_state(pid)
    if not state:
        lines.append("Claude Code: session active (still starting up, no details yet).")
        return '\n'.join(lines)
    lines.append(f"Claude Code: active in {state.get('project_name', '?')} "
                 f"(running for {format_duration(now - state.get('started_at', now))}, "
                 f"sending to {state.get('sending_to', '?')})")
    last = (state.get('last_sent') or '').strip()
    if last:
        ago = format_duration(now - state.get('last_sent_at', now))
        lines.append(f"\nLast message the bridge sent ({ago} ago):\n{last[:600]}")
    else:
        lines.append("\nThe bridge hasn't sent anything yet.")
    return '\n'.join(lines)


def press_key(name):
    code, label = KEY_CODES[name.lower()]
    try:
        result = subprocess.run(
            ['osascript', '-e', f'tell application "System Events" to key code {code}'],
            capture_output=True, text=True, timeout=KEY_TIMEOUT)
    except subprocess.TimeoutExpired:  # run() has already killed osascript
        return send_imessage(ACCESSIBILITY_MSG + "\n(macOS may also be showing a "
                             "permission prompt on the Mac's screen — approve it and try again.)")
    if result.returncode == 0:
        return send_imessage(f"Pressed {label}.")
    error = (result.stderr or '').strip()
    if PERMISSION_ERROR_RE.search(error):
        return send_imessage(ACCESSIBILITY_MSG)
    send_imessage(f"Couldn't press {label}: {error[:200] or result.returncode}")


def launchd_agent_pid():
    """PID launchd has for the agent job, or None if it isn't loaded/running."""
    result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{AGENT_LABEL}'],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    match = re.search(r'^\s*pid = (\d+)', result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def restart_agent():
    """Restart through launchd so the new instance has launchd's Full Disk Access.

    `launchctl kickstart -k` makes launchd itself kill and relaunch the job.
    Doing bootout + bootstrap from in here would not work: bootout kills this
    process, so the bootstrap that follows would never run and the agent would
    stay dead. A running bridge session is unaffected (it has its own session).
    """
    if launchd_agent_pid() != os.getpid():
        send_imessage("I can't restart myself: this agent wasn't started by launchd "
                      "(probably run by hand), and restarting the launchd job would "
                      "start a second copy. Use Start AI Agent instead.")
        return
    send_imessage("Restarting agent — back in a few seconds...")
    result = subprocess.run(['launchctl', 'kickstart', '-k',
                             f'gui/{os.getuid()}/{AGENT_LABEL}'],
                            capture_output=True, text=True)
    if result.returncode != 0:
        send_imessage(f"Restart failed: {result.stderr.strip() or result.returncode}")
        return
    # launchd is killing this process; the new instance texts "Agent online".
    time.sleep(30)
    send_imessage("Restart was requested but this agent is still running — "
                  "check agent_error.log.")


def handle_pending_project(msg):
    """The reply to "Which project?". True if the message was consumed."""
    global PENDING_PROJECT_UNTIL
    if not PENDING_PROJECT_UNTIL:
        return False
    expired = time.time() > PENDING_PROJECT_UNTIL
    PENDING_PROJECT_UNTIL = 0.0  # one-shot either way
    if expired:
        return False  # too late: treat it as an ordinary message
    if is_agent_command(msg) or STOP_RE.match(msg):
        return False  # they moved on to another command
    if msg.strip().lower() in ('cancel', 'never mind', 'nevermind'):
        send_imessage("Got it — cancelled.")
        return True
    start_bridge(msg.strip())
    return True


def handle_bridge_command(msg):
    """'start claude [name|path]' / 'stop claude' / 'list projects' / 'memory <project>' / 'clear memory <project>' / 'status' / 'restart agent' / 'press enter|space|escape'. True if the message was one."""
    if handle_pending_project(msg):
        return True
    match = START_RE.match(msg)
    if match:
        start_bridge(match.group(1))
        return True
    if STOP_RE.match(msg):
        stop_bridge()
        return True
    if LIST_RE.match(msg):
        list_projects()
        return True
    match = CLEAR_MEMORY_RE.match(msg)
    if match:
        clear_memory(match.group(1))
        return True
    match = MEMORY_RE.match(msg)
    if match:
        show_memory(match.group(1))
        return True
    if RESTART_RE.match(msg):
        restart_agent()
        return True
    match = PRESS_RE.match(msg)
    if match:
        press_key(match.group(1))
        return True
    if is_status_request(msg):
        send_imessage(status_report())
        return True
    return False


def process_command(user_message):
    global PENDING_ACTION

    reply = user_message.strip()
    if PENDING_ACTION:
        needs_confirm = PENDING_ACTION.get('confirm')
        if reply.upper() in ('NO', 'N'):
            PENDING_ACTION = {}
            send_imessage("Got it — cancelled.")
            return
        approved = reply == 'CONFIRM' if needs_confirm else reply.upper() in ('YES', 'Y')
        if approved:
            action = PENDING_ACTION
            PENDING_ACTION = {}
            if action['type'] == 'terminal':
                pattern = blocked_pattern(action['command'])
                if pattern:  # can't happen via the normal path; never run it anyway
                    log_blocked(action['command'], pattern, 'refused after approval')
                    send_imessage(BLOCKED_MESSAGE)
                    return
                send_imessage(f"Running: {action['command']}")
                output = run_terminal_command(action['command'])
                send_imessage(f"Done. Output:\n{output}")
            return
        if needs_confirm and reply.upper() in ('YES', 'Y', 'CONFIRM'):
            send_imessage(CONFIRM_HINT)  # keep waiting: YES / lowercase isn't enough
            return
        # Anything else abandons the proposal, so a late "YES" can't run it
        PENDING_ACTION = {}

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}]
    )

    reply = response.content[0].text.strip()

    if reply.startswith('IGNORE'):
        print("Ignored automated message")
        return

    if reply.startswith('CANNOT_HELP:'):
        send_imessage(reply.replace('CANNOT_HELP:', '').strip())
        return

    if 'COMMAND:' in reply:
        command = ''
        explanation = ''
        for line in reply.split('\n'):
            if line.startswith('COMMAND:'):
                command = line.replace('COMMAND:', '').strip()
            if line.startswith('EXPLANATION:'):
                explanation = line.replace('EXPLANATION:', '').strip()

        if command:
            pattern = blocked_pattern(command)
            if pattern:
                PENDING_ACTION = {}
                log_blocked(command, pattern, user_message)
                send_imessage(BLOCKED_MESSAGE)
                return
            confirm = risky_pattern(command) is not None
            PENDING_ACTION = {'type': 'terminal', 'command': command, 'confirm': confirm}
            ending = CONFIRM_ENDING if confirm else "Reply YES to confirm or NO to cancel."
            send_imessage(f"I'd like to run:\n\n{command}\n\n{explanation}\n\n{ending}")


def handle_incoming(row, msg):
    """Handle one message from the user (row from get_messages_after)."""
    rowid = row[0]
    reason = own_message_reason(msg, row[4])
    if reason:
        print(f"Skipping message (rowid {rowid}, {reason}): {msg[:60]!r}")
        return

    # Reply in the thread this message came from. While a bridge session owns
    # the chat its replies are the bridge's business (it tracks the thread itself);
    # only messages this agent will actually answer move the target.
    if not bridge_active() or is_agent_command(msg) or STOP_RE.match(msg):
        set_reply_target(row[5] if len(row) > 5 else None)

    # start/stop must work even while a bridge session owns the chat
    if handle_bridge_command(msg):
        print(f"Bridge command (rowid {rowid}): {msg}")
        return

    if bridge_active():
        print(f"Bridge session active, skipping rowid {rowid}")
        return

    print(f"New message (rowid {rowid}, from {row[5] if len(row) > 5 else '?'}): {msg}")
    try:
        process_command(msg)
    except Exception as e:
        print(f"Error handling rowid {rowid}: {e}")


def main():
    config.require()  # stop with a clear message if .env is incomplete
    # launchd restarts this process, so honour the kill switch by idling
    if os.path.exists(DISABLED_FLAG):
        print("AGENT_DISABLED present — idle until it is removed")
        while os.path.exists(DISABLED_FLAG):
            time.sleep(30)

    print("iMessage agent running — waiting for messages...")
    print(f"Replying in the thread you text from (currently: {reply_target()[0]}; "
          f"fallback {REPLY_ADDRESS}) | agent identity: {AGENT_ADDRESS} | user addresses: {', '.join(WATCH_IDENTIFIERS)}")

    # Only messages that arrive after startup are considered
    last_rowid = get_latest_rowid()
    print(f"Starting from rowid: {last_rowid}")

    send_imessage("Agent online. Send me a command.")

    empty_polls = {}

    while True:
        try:
            for row in get_messages_after(last_rowid):
                rowid = row[0]
                msg = parse_message(row)

                if msg is None:
                    # Body may not be written yet; retry on later polls
                    # rather than skipping past it for good.
                    empty_polls[rowid] = empty_polls.get(rowid, 0) + 1
                    if empty_polls[rowid] < MAX_EMPTY_POLLS:
                        break
                    empty_polls.pop(rowid)
                    last_rowid = rowid
                    continue

                empty_polls.pop(rowid, None)
                # Advance before processing so a failure can't cause a
                # message to be handled twice or block the ones after it.
                last_rowid = rowid

                handle_incoming(row, msg)

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
