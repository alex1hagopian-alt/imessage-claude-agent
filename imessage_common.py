# Shared outgoing-iMessage code: splits long texts into labelled parts, sends them via Messages, and records them so their echoes are ignored.
"""Outgoing iMessage helpers shared by imessage_agent.py and claude_code_bridge.py.

Everything either script texts goes through send_imessage(), which
  * splits anything over MAX_MESSAGE_CHARS at natural breaks and labels the
    pieces "Part 1/3:", "Part 2/3:", ... so a dropped part is obvious,
  * sends the pieces in order with a pause between them,
  * remembers what it sent so the echo coming back through chat.db is ignored.
"""
import datetime
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import deque

import config

# Messages on this Mac is signed into its OWN Apple ID (IMESSAGE_SEND_ADDRESS), so
# the user <-> agent chat is an ordinary two-person conversation (no texting
# yourself, no double bubbles). The user texts AGENT_ADDRESS; on the Mac that
# conversation is filed under the USER's address (a chat is named after the other
# party), so we send TO the user's address and read messages RECEIVED AT
# AGENT_ADDRESS (message.destination_caller_id). Never send to AGENT_ADDRESS:
# that would be the Mac texting itself.
AGENT_ADDRESS = config.IMESSAGE_SEND_ADDRESS
USER_PHONE = config.AGENT_PHONE_NUMBER
# Every address the user's iPhone might text the agent from (it picks one per its
# "Start new conversations from" setting); each becomes a chat_identifier.
USER_ADDRESSES = (USER_PHONE, *config.EXTRA_USER_ADDRESSES)
REPLY_ADDRESS = USER_PHONE              # default destination before any message arrives
REPLY_CHAT_GUID = f'iMessage;-;{REPLY_ADDRESS}'
# The optional collaborator is treated like the user, but in their OWN chat: their
# messages are read and every reply to them goes to their number, never to REPLY_ADDRESS.
COLLABORATOR_ADDRESSES = (config.COLLABORATOR_PHONE,) if config.COLLABORATOR_PHONE else ()
WATCH_IDENTIFIERS = USER_ADDRESSES + COLLABORATOR_ADDRESSES  # chat_identifiers of the agent's conversations

# Who is talking. Each person has their own conversation history, pending YES/NO, and
# Claude Code session (bridge); the agent keeps that state per person.
OWNER = 'owner'
COLLABORATOR = 'collaborator'
PEOPLE = (OWNER, COLLABORATOR) if COLLABORATOR_ADDRESSES else (OWNER,)


def person_for(identifier):
    """OWNER or COLLABORATOR, for the chat_identifier a message came from."""
    return COLLABORATOR if identifier in COLLABORATOR_ADDRESSES else OWNER


def person_addresses(person):
    return COLLABORATOR_ADDRESSES if person == COLLABORATOR else USER_ADDRESSES

# ---- Which thread a reply goes to -------------------------------------------
# OFF by default: every reply goes to REPLY_ADDRESS, the user's phone-number chat
# (which belongs to the agent's Apple ID). Chats for the user's other addresses may
# belong to a different Apple ID that is not signed into Messages here, so replying
# into them can fail. Messages from ALL of WATCH_IDENTIFIERS are still read; only
# where replies go is fixed.
# Set to True to reply in whichever thread the user texted from instead: each
# incoming message is recorded with its chat_identifier (set_reply_target) and every
# send resolves its destination from that (reply_target); only WATCH_IDENTIFIERS can
# ever be a target.
REPLY_TO_SOURCE_THREAD = False
# Shared by the agent, the bridge and morning_brief.py (separate processes), so
# unprompted texts (startup, the 7am brief, async bridge messages) follow the
# thread last used, too.
LAST_THREAD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last_thread.json')
_current_target = None


# Group chats the agent has verified (every member is the owner or collaborator), by
# chat_identifier ("chat123...") -> chat guid. Filled in by the agent; only these groups
# can be sent to.
TRUSTED_GROUPS = {}


def is_group(identifier):
    return bool(identifier) and identifier.startswith('chat')


def chat_guid(identifier):
    if identifier in TRUSTED_GROUPS:
        return TRUSTED_GROUPS[identifier]
    return f'iMessage;-;{identifier}'  # always the iMessage chat, never the SMS one


def reply_address_for(identifier):
    """The chat a reply to a message from `identifier` goes to. The collaborator's
    always goes to their own chat; the user's follows REPLY_TO_SOURCE_THREAD; a verified
    group's goes back to the group."""
    if identifier in TRUSTED_GROUPS:
        return identifier
    if identifier in COLLABORATOR_ADDRESSES:
        return identifier
    if REPLY_TO_SOURCE_THREAD and identifier in USER_ADDRESSES:
        return identifier
    return REPLY_ADDRESS


def set_reply_target(identifier):
    """Record who just texted (from `identifier`); replies go back to their chat."""
    global _current_target
    if identifier not in WATCH_IDENTIFIERS and identifier not in TRUSTED_GROUPS:
        return
    _current_target = reply_address_for(identifier)
    if not REPLY_TO_SOURCE_THREAD or identifier in COLLABORATOR_ADDRESSES or identifier in TRUSTED_GROUPS:
        return  # last_thread.json is only for the user's own threads, shared with morning_brief
    tmp = f"{LAST_THREAD_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'chat_identifier': identifier, 'at': time.time(), 'pid': os.getpid()}, f)
        os.replace(tmp, LAST_THREAD_FILE)  # atomic: other processes never read half a file
    except OSError:
        pass


def restore_reply_target(target):
    """Put back a target saved from reply_target()[0] (None = nothing recorded yet)."""
    global _current_target
    _current_target = target


def current_reply_target():
    return _current_target


def reply_target():
    """(chat_identifier, why) that the next send will use."""
    if _current_target:
        return _current_target, 'chat of the last message this process handled'
    if not REPLY_TO_SOURCE_THREAD:
        return REPLY_ADDRESS, 'reply-to-source-thread is off'
    try:
        with open(LAST_THREAD_FILE, encoding='utf-8') as f:
            identifier = json.load(f).get('chat_identifier')
        if identifier in WATCH_IDENTIFIERS:
            return identifier, 'thread last texted from (last_thread.json)'
    except (OSError, ValueError, AttributeError):
        pass
    return REPLY_ADDRESS, 'default (no message received yet)'


# ---- One session per project folder -----------------------------------------
# The user and the collaborator may each run a Claude Code session, but never in the
# same folder at once (two sessions editing one tree would trample each other). The
# bridge claims its project with an exclusive lock file; a lock whose process is gone
# is stale and is taken over. The agent only READS it (project_holder) to say no early.
def _project_lock_path(project):
    digest = hashlib.sha1(os.path.realpath(project).encode()).hexdigest()[:16]
    return f'/tmp/claude_project_{digest}.lock'


def _read_project_lock(path):
    """(pid, person) if `path` is a lock held by a live process, else None."""
    try:
        with open(path, encoding='utf-8') as f:
            held = json.load(f)
        os.kill(int(held['pid']), 0)
        return int(held['pid']), held.get('person', OWNER)
    except PermissionError:
        return int(held['pid']), held.get('person', OWNER)  # alive, just not ours
    except (OSError, ValueError, KeyError, TypeError):
        return None


def project_holder(project):
    """(pid, person) of the live session using `project`, or None."""
    return _read_project_lock(_project_lock_path(project))


def claim_project(project, person):
    """Take `project` for this process. None on success, else (pid, person) of the holder."""
    path = _project_lock_path(project)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder = _read_project_lock(path)
            if holder and holder[0] != os.getpid():
                return holder
            try:
                os.remove(path)  # stale (its process is gone) or ours: retake it
            except OSError:
                pass
            continue
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'pid': os.getpid(), 'person': person, 'project': os.path.realpath(project)}, f)
        return None
    return _read_project_lock(path) or (0, OWNER)


def release_project(project):
    path = _project_lock_path(project)
    held = _read_project_lock(path)
    if held and held[0] == os.getpid():
        try:
            os.remove(path)
        except OSError:
            pass


MAX_MESSAGE_CHARS = 1500   # per iMessage, including the "Part i/n: " label
CHUNK_DELAY = 1.0          # seconds between parts so they arrive in order

# Terminal output: sent whole up to OUTPUT_LIMIT, else head + tail with a note
OUTPUT_LIMIT = 2000
OUTPUT_HEAD = 1000
OUTPUT_TAIL = 500

# Texts this process has sent. The Mac and phone share an Apple ID, so our own
# messages come back as new rows (sometimes with is_from_me=0); is_from_me can't
# be trusted, so incoming messages are matched against this by content.
SENT_TEXTS = deque(maxlen=200)

_PART_LABEL_RE = re.compile(r'^part \d+/\d+:', re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r'[.!?]["\')\]]*\s')


def normalize(text):
    return ' '.join(text.split()).lower()


def is_part_label(text):
    """True for a chunk of a split message ("Part 2/3: ...")."""
    return _PART_LABEL_RE.match(text.strip()) is not None


def _find_break(window, budget):
    """Where to cut `window` (the next budget+1 characters): a paragraph break,
    else a line break, else the end of a sentence, else a space, else mid-word
    only if there is nothing better. Always returns a cut <= budget + 1."""
    floor = budget // 2
    cut = window.rfind('\n\n', 0, budget + 1)
    if cut >= floor:
        return cut
    cut = window.rfind('\n', 0, budget + 1)
    if cut >= floor:
        return cut
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(window) if m.end() <= budget + 1]
    if ends and ends[-1] >= floor:
        return ends[-1]
    cut = window.rfind(' ', 0, budget + 1)
    if cut >= floor // 2:
        return cut
    return budget  # one huge unbroken token: hard cut


def _split_on_breaks(text, budget):
    pieces, rest = [], text
    while len(rest) > budget:
        cut = _find_break(rest[:budget + 1], budget)
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


def split_message(text, limit=MAX_MESSAGE_CHARS):
    """[text] if it fits, else labelled parts, each at most `limit` characters."""
    text = text.rstrip()
    if len(text) <= limit:
        return [text]
    parts = max(2, math.ceil(len(text) / limit))
    while True:
        # budget for the label at its widest ("Part 12/12: ")
        pieces = _split_on_breaks(text, limit - len(f"Part {parts}/{parts}: "))
        if len(pieces) <= parts:
            break
        parts = len(pieces)
    total = len(pieces)
    return [f"Part {i}/{total}: {piece}" for i, piece in enumerate(pieces, 1)]


def truncate_output(output, limit=OUTPUT_LIMIT, head=OUTPUT_HEAD, tail=OUTPUT_TAIL):
    """Terminal output over `limit` characters: the first `head`, the last
    `tail`, and a note saying how many lines were left out."""
    if len(output) <= limit:
        return output
    omitted = output[head:len(output) - tail]
    lines = omitted.count('\n') + 1
    note = (f"[...{lines} line{'s' if lines != 1 else ''} truncated — "
            "run command manually to see full output...]")
    return f"{output[:head]}\n{note}\n{output[-tail:]}"


# Runtime proof of where texts really go: one line per send, written by whichever
# process sends (agent, bridge), with the recipient taken from the AppleScript
# that is actually handed to osascript — not from a constant.
SEND_DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'send_debug.log')
_logged_origin = False


def _debug(message):
    global _logged_origin
    try:
        with open(SEND_DEBUG_LOG, 'a', encoding='utf-8') as f:
            if not _logged_origin:
                _logged_origin = True
                f.write(f"{datetime.datetime.now()} — process started sending: pid={os.getpid()} "
                        f"proc={os.path.basename(sys.argv[0])} imessage_common={os.path.abspath(__file__)}\n")
            f.write(f"{datetime.datetime.now()} — {message}\n")
    except Exception:
        pass  # debugging must never stop a text from being sent


def _send_one(text, identifier=None, why='explicit'):
    """One osascript call. The text goes through a private temp file so any
    characters survive and the agent and bridge (separate processes) can't
    overwrite each other's message. `identifier` is the chat_identifier to send to."""
    identifier = identifier or REPLY_ADDRESS
    guid = chat_guid(identifier)
    fd, path = tempfile.mkstemp(prefix='imessage_', suffix='.txt')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        # A collaborator who has never texted the agent has no usable chat yet, and
        # "send to chat id" then silently does nothing. Sending to the iMessage
        # participant creates the chat (and reuses it afterwards).
        if identifier in COLLABORATOR_ADDRESSES:
            target = f'participant "{identifier}" of (1st account whose service type = iMessage)'
        else:
            target = f'chat id "{guid}"'
        script = f'''set replyFile to open for access POSIX file "{path}"
set replyText to read replyFile as «class utf8»
close access replyFile
tell application "Messages"
send replyText to {target}
end tell'''
        recipient = guid
        _debug(f"sending to: {recipient} | routing={why} | pid={os.getpid()} "
               f"proc={os.path.basename(sys.argv[0])} text={text[:60]!r}")
        result = subprocess.run(['osascript', '-e', script],
                                capture_output=True, text=True, timeout=30)
        _debug(f"  result: returncode={result.returncode} stderr={result.stderr.strip()!r}")
        if result.returncode != 0:
            print(f"Send failed: {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        _debug("  result: osascript TIMED OUT")
        print("Send failed: osascript timed out")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# Callables run once per send_imessage() call with the full (unsplit) text, after it is sent.
# The agent registers one to keep its conversation history; the bridge registers none.
SEND_HOOKS = []


def send_imessage(text, hooks=True, identifier=None):
    """Text `text` to the user, splitting and labelling it if it is long. `identifier`
    sends to one specific verified group chat without moving the normal reply target."""
    chunks = split_message(text)
    if identifier is not None:
        if identifier not in TRUSTED_GROUPS:
            raise ValueError(f"not a verified group chat: {identifier}")
        why = 'verified group chat'
    else:
        identifier, why = reply_target()  # decided once, so all parts go to the same thread
    for i, chunk in enumerate(chunks):
        if i:
            time.sleep(CHUNK_DELAY)
        # Record before sending so the echo is recognised even if it lands fast
        SENT_TEXTS.append(normalize(chunk))
        _send_one(chunk, identifier, why)
    if hooks:
        for hook in SEND_HOOKS:
            try:
                hook(text)
            except Exception:
                pass  # a bookkeeping hook must never break sending
