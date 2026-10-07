# Runs one headless Claude Code session in a project and relays its permission prompts and results over iMessage.
"""Drive a headless Claude Code session from iMessage.

Usage:
    venv/bin/python claude_code_bridge.py /path/to/project "optional first task"

Claude Code runs as a subprocess in stream-json mode. Each tool call that needs
permission is texted to you as a numbered menu; whatever you reply is relayed
back (1/2/3, yes/no, or free text for Claude to read). When Claude finishes a
task you get its summary and can reply with a follow-up task, or DONE to end the
session. STOP at any time kills it.

iMessage send/receive and echo filtering are reused from imessage_agent.py.
"""
import argparse
import atexit
import datetime
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

import imessage_agent as im
import imessage_common
from imessage_common import reply_target, send_imessage, set_reply_target

LOCK_FILE = im.BRIDGE_LOCK_FILE
# Always passed to claude explicitly, so sessions don't depend on whatever model
# the Claude Code install happens to have configured. Override with --model.
DEFAULT_MODEL = 'claude-sonnet-5-5'
LOG_FILE = os.path.join(im.PROJECT_DIR, 'bridge.log')
PERMISSION_TIMEOUT = 60 * 60       # no answer in an hour -> deny
NUDGE_AFTER = 15 * 60              # remind every 15 min while waiting
FOLLOWUP_TIMEOUT = 2 * 60 * 60     # idle after a task -> end session

# Sent automatically when the user ends the session with DONE or STOP, so
# Claude Code's own CLAUDE.md memory stays current. No confirmation is asked.
MEMORY_PROMPT = ("Update CLAUDE.md in this project folder to reflect everything we did "
                 "this session. Include: what this project does, current status, key "
                 "decisions made, things that didn't work and why, and what needs to "
                 "happen next. Keep it concise but complete — this file is your memory "
                 "for future sessions.")
MEMORY_TIMEOUT = 240       # seconds Claude gets to write CLAUDE.md
INTERRUPT_TIMEOUT = 20     # seconds to abort a task that is mid-flight

NEW_PROJECT_PROMPT = ("This is a new project called {name}. "
                      "Wait for the user to describe what they want to build.")

YES_WORDS = {'1', 'YES', 'Y', 'OK', 'ALLOW', 'APPROVE'}
ALWAYS_WORDS = {'2', 'ALWAYS'}
NO_WORDS = {'NO', 'N', 'DENY'}
END_WORDS = {'DONE', 'END', 'EXIT', 'QUIT'}


def log(message):
    """Timestamped line appended to bridge.log (the agent also points the
    bridge's stdout/stderr at that file, so tracebacks land there too)."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {message}\n"
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    except OSError:
        pass


def one_line(text, limit=500):
    text = ' ⏎ '.join(str(text).strip().splitlines())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def describe_event(event):
    """A log line for the Claude Code events worth recording (None = noise)."""
    kind, sub = event.get('type'), event.get('subtype')
    if kind == 'system' and sub == 'init':
        return (f"claude init: session={event.get('session_id')} "
                f"model={event.get('model')} cwd={event.get('cwd')}")
    if kind == 'assistant':
        parts = []
        for block in event.get('message', {}).get('content', []):
            if block.get('type') == 'text' and block.get('text', '').strip():
                parts.append('text: ' + one_line(block['text']))
            elif block.get('type') == 'tool_use':
                parts.append(f"tool_use {block.get('name')}: {one_line(json.dumps(block.get('input')), 300)}")
        return ('claude: ' + ' | '.join(parts)) if parts else None
    if kind == 'user':
        content = event.get('message', {}).get('content')
        if isinstance(content, list):
            for block in content:
                if block.get('type') == 'tool_result':
                    err = ' (error)' if block.get('is_error') else ''
                    return f"tool_result{err}: {one_line(block.get('content'), 200)}"
        return None
    if kind == 'control_request':
        req = event.get('request', {})
        return (f"permission request: {req.get('tool_name')} "
                f"{one_line(json.dumps(req.get('input')), 300)}")
    if kind == 'result':
        return (f"result: subtype={sub} is_error={event.get('is_error')} "
                f"cost=${event.get('total_cost_usd') or 0:.3f} text={one_line(event.get('result') or '')}")
    return None


def is_stop(text):
    # "stop claude" is also an imessage_agent.py command; treat it as STOP here
    return text.strip().upper() == 'STOP' or im.STOP_RE.match(text) is not None


class StopSession(Exception):
    pass


def clip(text, limit=300):  # short previews only (tool descriptions); never used for outgoing texts
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def describe_tool_request(tool, tool_input):
    """One compact, human-readable line (or block) for a permission prompt."""
    if tool == 'Bash':
        return f"Bash:\n{tool_input.get('command', '')}"
    if tool in ('Write', 'Edit', 'MultiEdit', 'NotebookEdit'):
        path = tool_input.get('file_path') or tool_input.get('notebook_path', '')
        preview = tool_input.get('content') or tool_input.get('new_string') or ''
        return f"{tool}: {path}" + (f"\n{clip(preview, 300)}" if preview else '')
    if tool in ('Read', 'Glob', 'Grep'):
        return f"{tool}: {tool_input.get('file_path') or tool_input.get('pattern', '')}"
    if tool in ('WebFetch', 'WebSearch'):
        return f"{tool}: {tool_input.get('url') or tool_input.get('query', '')}"
    return f"{tool}: {clip(json.dumps(tool_input), 1500)}"


def describe_suggestion(suggestion):
    if suggestion.get('type') == 'addRules':
        return ', '.join(f"{r.get('toolName')}({r['ruleContent']})" if r.get('ruleContent')
                         else str(r.get('toolName'))
                         for r in suggestion.get('rules', []))
    if suggestion.get('type') == 'addDirectories':
        return 'access to ' + ', '.join(suggestion.get('directories', []))
    return suggestion.get('type', 'permission')


class Bridge:
    def __init__(self, project_dir, permission_mode='default', model=DEFAULT_MODEL,
                 send=None, claude_bin='claude'):
        self.project_dir = os.path.abspath(project_dir)
        self.name = os.path.basename(self.project_dir)
        self.permission_mode = permission_mode
        self.model = model
        self.claude_bin = claude_bin
        self.send = send or send_imessage  # splits long texts into labelled parts
        self.events = queue.Queue()
        self.proc = None
        # Only iMessages that arrive after the bridge starts count as replies
        self.cursor = im.get_latest_rowid()
        self.empty_polls = {}
        self.preamble_pending = False
        self.turn_active = False   # a message was sent and no result seen yet
        self.did_work = False      # at least one real task was sent
        self.ack_pending = False   # tell the user Claude Code picked the task up
        # For the agent's "status" command; main() turns this on
        self.state_file = None
        self.started_at = time.time()
        self.last_sent = ''
        self.last_sent_at = None

    # ---- iMessage side -------------------------------------------------
    def write_state(self):
        if not self.state_file:
            return
        state = {'pid': os.getpid(), 'project': self.project_dir,
                 'project_name': self.name, 'started_at': self.started_at,
                 'sending_to': reply_target()[0],
                 'last_sent': self.last_sent, 'last_sent_at': self.last_sent_at}
        tmp = f"{self.state_file}.{os.getpid()}.tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f)
            os.replace(tmp, self.state_file)  # atomic: the agent never reads half a file
        except OSError as e:
            log(f"could not write state file: {e}")

    def notify(self, text):
        # No clipping here: send_imessage splits anything long into "Part i/n" texts
        message = f"[Claude Code · {self.name}]\n{text}".strip()
        log(f"SEND -> user: {one_line(message, 1500)}")
        self.last_sent, self.last_sent_at = message, time.time()
        self.write_state()
        self.send(message)

    def maybe_ack(self, event):
        """First sign of life after a task was sent: tell the user right away."""
        if self.ack_pending and event.get('type') in (
                'system', 'assistant', 'user', 'control_request', 'result'):
            self.ack_pending = False
            self.notify("Claude Code is running — working on your task...")

    def poll_inbox(self):
        """New texts from the user since the last call, oldest first."""
        texts = []
        for row in im.get_messages_after(self.cursor):
            rowid = row[0]
            msg = im.parse_message(row)
            if msg is None:
                # body may not be written yet: retry a few polls, then skip
                self.empty_polls[rowid] = self.empty_polls.get(rowid, 0) + 1
                if self.empty_polls[rowid] < im.MAX_EMPTY_POLLS:
                    break
                self.empty_polls.pop(rowid)
                self.cursor = rowid
                continue
            self.empty_polls.pop(rowid, None)
            self.cursor = rowid
            reason = im.own_message_reason(msg, row[4])
            if reason is None and im.is_agent_command(msg):
                reason = 'command for the agent'
            if reason:
                log(f"IGNORED rowid {rowid} ({reason}): {one_line(msg, 120)}")
                continue
            chat = row[5] if len(row) > 5 else None
            log(f"RECEIVED rowid {rowid} (from {chat}): {one_line(msg, 300)}")
            set_reply_target(chat)  # our replies follow whichever thread the user texts from
            texts.append(msg)
        return texts

    def wait_reply(self, timeout, nudge=None):
        """Block until the user sends any text. Returns None on timeout.
        STOP raises. If `nudge` is given it is re-sent every NUDGE_AFTER
        seconds of silence."""
        start = time.time()
        next_nudge = start + NUDGE_AFTER
        while time.time() - start < timeout:
            if self.proc.poll() is not None:
                return None
            for text in self.poll_inbox():
                if is_stop(text):
                    raise StopSession()
                return text.strip()
            if nudge and time.time() >= next_nudge:
                self.notify(nudge)
                next_nudge += NUDGE_AFTER
            time.sleep(im.POLL_SECONDS)
        return None

    # ---- Claude Code side ----------------------------------------------
    def start(self):
        cmd = [self.claude_bin, '-p',
               '--input-format', 'stream-json',
               '--output-format', 'stream-json', '--verbose',
               '--permission-mode', self.permission_mode,
               '--permission-prompt-tool', 'stdio']
        if self.model:
            cmd += ['--model', self.model]
        self.proc = subprocess.Popen(
            cmd, cwd=self.project_dir, text=True, bufsize=1,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        log(f"started claude (pid {self.proc.pid}) in {self.project_dir}: {' '.join(cmd)}")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                log(f"unparseable claude output: {one_line(line, 200)}")
                continue
            described = describe_event(event)
            if described:
                log(described)
            self.events.put(event)
        log(f"claude stdout closed (exit code {self.proc.poll()})")
        self.events.put(None)  # EOF: process exited

    def _read_stderr(self):
        for line in self.proc.stderr:
            if line.strip():
                log(f"claude stderr: {one_line(line, 300)}")

    def write(self, obj):
        try:
            self.proc.stdin.write(json.dumps(obj) + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def send_task(self, text, count_as_work=True):
        log(f"TO CLAUDE ({'task' if count_as_work else 'internal'}): {one_line(text, 300)}")
        self.turn_active = True
        self.did_work = self.did_work or count_as_work
        self.ack_pending = self.ack_pending or count_as_work
        self.write({'type': 'user',
                    'message': {'role': 'user', 'content': text}})

    def answer_permission(self, request_id, allow, tool_input, reason='',
                          permissions=None):
        log(f"permission decision: {'ALLOW' if allow else 'DENY'}"
            f"{' (+session rules)' if permissions else ''} {one_line(reason, 120)}")
        if allow:
            body = {'behavior': 'allow', 'updatedInput': tool_input}
            if permissions:
                body['updatedPermissions'] = permissions
        else:
            body = {'behavior': 'deny',
                    'message': reason or 'The user denied this request over iMessage.'}
        self.write({'type': 'control_response',
                    'response': {'subtype': 'success',
                                 'request_id': request_id, 'response': body}})

    def handle_control_request(self, event):
        request_id = event['request_id']
        req = event.get('request', {})
        if req.get('subtype') != 'can_use_tool':
            self.write({'type': 'control_response',
                        'response': {'subtype': 'error', 'request_id': request_id,
                                     'error': 'unsupported request'}})
            return
        tool_input = req.get('input', {})
        # "Don't ask again" = Claude's own suggestions, kept to this session
        # so nothing is written into the project's settings files.
        suggestions = [dict(sg, destination='session')
                       for sg in req.get('permission_suggestions', [])]
        menu = ["1) Yes"]
        if suggestions:
            menu.append("2) Yes, and don't ask again this session for: "
                        + '; '.join(describe_suggestion(sg) for sg in suggestions))
            menu.append("3) No")
        else:
            menu.append("2) No")
        no_number = '3' if suggestions else '2'

        self.notify(f"Claude wants to use {describe_tool_request(req.get('tool_name', '?'), tool_input)}"
                    f"\n\n" + '\n'.join(menu)
                    + "\n\nReply with a number, yes/no, or type feedback for Claude.")
        try:
            reply = self.wait_reply(
                PERMISSION_TIMEOUT,
                nudge="Still waiting on your reply to the permission request above.")
        except StopSession:
            self.answer_permission(request_id, False, tool_input,
                                   'The user ended the session.')
            raise
        word = (reply or '').strip().upper()
        if reply is None:
            self.answer_permission(request_id, False, tool_input,
                                   'No reply from the user; request timed out.')
            self.notify("No reply — denied.")
        elif word in YES_WORDS:
            self.answer_permission(request_id, True, tool_input)
        elif suggestions and word in ALWAYS_WORDS:
            self.answer_permission(request_id, True, tool_input,
                                   permissions=suggestions)
        elif word in NO_WORDS or word == no_number:
            self.answer_permission(request_id, False, tool_input)
        else:
            # Free text: never treated as approval, but Claude sees it verbatim
            self.answer_permission(request_id, False, tool_input,
                                   f"The user did not approve this. They replied: {reply}")

    def interrupt(self):
        self.write({'type': 'control_request', 'request_id': f'interrupt-{time.time_ns()}',
                    'request': {'subtype': 'interrupt'}})

    def auto_permission(self, event):
        """While closing, nobody is asked anything: only CLAUDE.md may be written."""
        request_id = event['request_id']
        req = event.get('request', {})
        if req.get('subtype') != 'can_use_tool':
            self.write({'type': 'control_response',
                        'response': {'subtype': 'error', 'request_id': request_id,
                                     'error': 'unsupported request'}})
            return
        tool_input = req.get('input', {})
        path = os.path.expanduser(tool_input.get('file_path') or '')
        target = os.path.realpath(os.path.join(self.project_dir, 'CLAUDE.md'))
        if (req.get('tool_name') in ('Write', 'Edit', 'MultiEdit') and path
                and os.path.realpath(os.path.join(self.project_dir, path)) == target):
            self.answer_permission(request_id, True, tool_input)
        else:
            self.answer_permission(request_id, False, tool_input,
                                   'The session is closing; only CLAUDE.md may be modified now.')

    def await_result(self, timeout):
        """Drain events until the current turn's result; None on timeout/exit.
        A second STOP from the user raises StopSession (skip the wait)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = self.events.get(timeout=im.POLL_SECONDS)
            except queue.Empty:
                for text in self.poll_inbox():
                    if is_stop(text):
                        raise StopSession()
                continue
            if event is None:
                return None
            if event.get('type') == 'control_request':
                self.auto_permission(event)
            elif event.get('type') == 'result':
                self.turn_active = False
                return event
        return None

    def update_memory(self):
        """Ask Claude Code to refresh CLAUDE.md before the session closes."""
        if not self.did_work or self.proc is None or self.proc.poll() is not None:
            return
        claude_md = os.path.join(self.project_dir, 'CLAUDE.md')
        mtime = lambda: os.path.getmtime(claude_md) if os.path.exists(claude_md) else None
        before = mtime()
        self.notify("Updating CLAUDE.md before closing… (send STOP again to skip)")
        try:
            if self.turn_active:  # STOP mid-task: abort it, don't queue behind it
                self.interrupt()
                self.await_result(INTERRUPT_TIMEOUT)
            self.send_task(MEMORY_PROMPT, count_as_work=False)
            self.await_result(MEMORY_TIMEOUT)
        except StopSession:
            return self.notify("Skipped the CLAUDE.md update.")
        after = mtime()
        if after is not None and (before is None or after > before):
            self.notify("CLAUDE.md updated.")
        else:
            self.notify("CLAUDE.md was NOT updated (Claude Code didn't write it).")

    def is_new_project(self):
        """True for a brand-new folder (nothing in it but macOS clutter)."""
        try:
            return not [e for e in os.listdir(self.project_dir) if e != '.DS_Store']
        except OSError:
            return False

    def prompt_for_task(self):
        self.notify("What should I work on?")
        return self.wait_reply(FOLLOWUP_TIMEOUT)

    def finish_turn(self, result):
        text = (result.get('result') or '').strip() or '(no text output)'
        ok = not result.get('is_error')
        cost = result.get('total_cost_usd')
        tail = f"\n\n(${cost:.2f} so far)" if cost else ''
        self.notify(("Finished:\n" if ok else "Ended with an error:\n") + text + tail
                    + "\n\nReply with a follow-up task, or DONE to end.")

    # ---- main loop -----------------------------------------------------
    def run(self, first_task=None):
        try:
            return self._session(first_task)
        except StopSession:
            self.update_memory()
            raise

    def _session(self, first_task=None):
        self.start()
        self.notify(f"Session started in {self.project_dir}")
        if self.is_new_project():
            # Brief Claude Code first so it waits for direction. Its reply to
            # this is swallowed (see the 'result' branch); then we ask you.
            self.preamble_pending = True
            self.send_task(NEW_PROJECT_PROMPT.format(name=self.name), count_as_work=False)
        else:
            task = first_task or self.prompt_for_task()
            if task is None:
                return self.notify("No task received — ending session.")
            self.send_task(task)

        while True:
            try:
                event = self.events.get(timeout=im.POLL_SECONDS)
            except queue.Empty:
                self.check_stop_while_busy()
                continue
            if event is None:
                return self.notify("Claude Code exited.")
            self.maybe_ack(event)
            kind = event.get('type')
            if kind == 'control_request':
                self.handle_control_request(event)
            elif kind == 'result':
                self.turn_active = False
                if self.preamble_pending and not event.get('is_error'):
                    self.preamble_pending = False
                    task = first_task or self.prompt_for_task()
                    if task is None:
                        return self.notify("No task received — ending session.")
                    self.send_task(task)
                    continue
                self.preamble_pending = False
                self.finish_turn(event)
                follow = self.wait_reply(FOLLOWUP_TIMEOUT)
                if follow is None:
                    return self.notify("Session ended.")
                if follow.upper() in END_WORDS:
                    self.update_memory()
                    return self.notify("Session ended.")
                self.send_task(follow)

    def check_stop_while_busy(self):
        for text in self.poll_inbox():
            if is_stop(text):
                raise StopSession()
            self.notify("Still working — I'll ask for your next instruction "
                        "when this task finishes. (STOP to kill it.)")

    def close(self):
        log("closing session")
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def startup_debug():
    """First thing the bridge does: record in send_debug.log exactly what it
    loaded and where its first text will go, before anything else can change it."""
    try:
        try:
            with open(imessage_common.LAST_THREAD_FILE, encoding='utf-8') as f:
                last_thread = f.read().strip()
        except OSError as e:
            last_thread = f'<unreadable: {e.strerror}>'
        target, why = imessage_common.reply_target()
        now = datetime.datetime.now()
        lines = [
            f"BRIDGE STARTUP pid={os.getpid()} ppid={os.getppid()} python={sys.executable} cwd={os.getcwd()} argv={sys.argv[1:]}",
            f"  imported imessage_common from: {os.path.abspath(imessage_common.__file__)}",
            f"  imported imessage_agent  from: {os.path.abspath(im.__file__)}   (sys.path[0]={sys.path[0]})",
            f"  loaded: REPLY_ADDRESS={imessage_common.REPLY_ADDRESS} REPLY_TO_SOURCE_THREAD={imessage_common.REPLY_TO_SOURCE_THREAD} "
            f"WATCH_IDENTIFIERS={imessage_common.WATCH_IDENTIFIERS}",
            f"  last_thread.json ({imessage_common.LAST_THREAD_FILE}) = {last_thread}",
            f"  => first text will go to: {imessage_common.chat_guid(target)}  (because: {why})",
            f"  env PYTHONPATH={os.environ.get('PYTHONPATH', '<unset>')}",
        ]
        with open(imessage_common.SEND_DEBUG_LOG, 'a', encoding='utf-8') as f:
            f.write(''.join(f"{now} — {line}\n" for line in lines))
    except Exception:
        pass  # diagnostics must never stop the bridge


def main():
    import config
    config.require()  # stop with a clear message if .env is incomplete
    startup_debug()
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('project_dir')
    parser.add_argument('task', nargs='?', help='first task (otherwise asked over iMessage)')
    parser.add_argument('--permission-mode', default='default',
                        choices=['default', 'acceptEdits', 'plan'],
                        help='bypassPermissions is intentionally not offered')
    parser.add_argument('--model', default=DEFAULT_MODEL,
                        help=f'Claude model for the session (default: {DEFAULT_MODEL})')
    args = parser.parse_args()

    if not os.path.isdir(args.project_dir):
        sys.exit(f"Not a directory: {args.project_dir}")
    if os.path.exists(im.DISABLED_FLAG):
        sys.exit("AGENT_DISABLED is present; remove it (Start AI Agent.app) first.")

    # Tell imessage_agent.py to stay quiet while this session owns the chat
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(LOCK_FILE) and os.remove(LOCK_FILE))
    atexit.register(lambda: os.path.exists(im.BRIDGE_STATE_FILE) and os.remove(im.BRIDGE_STATE_FILE))

    # SIGTERM (from "stop claude") must unwind so Claude Code is shut down too
    def on_sigterm(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_sigterm)

    bridge = Bridge(args.project_dir, args.permission_mode, args.model)
    bridge.state_file = im.BRIDGE_STATE_FILE
    bridge.write_state()
    log(f"bridge started (pid {os.getpid()}) for {bridge.project_dir}, replying in {reply_target()}"
        + (f", first task: {one_line(args.task, 200)}" if args.task else ""))
    try:
        bridge.run(args.task)
    except StopSession:
        log("stopped by user")
        bridge.notify("Stopped.")
    except KeyboardInterrupt:
        bridge.notify("Session interrupted on the Mac.")
    except Exception as e:  # never die silently: log it and tell the user
        import traceback
        log("BRIDGE CRASHED:\n" + traceback.format_exc())
        try:
            bridge.notify(f"The bridge crashed: {e}. Details are in bridge.log.")
        except Exception:
            pass
    finally:
        bridge.close()
        log("bridge exited")


if __name__ == '__main__':
    main()
