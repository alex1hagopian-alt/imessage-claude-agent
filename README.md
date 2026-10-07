# iMessage agent for macOS

A small set of Python scripts that let you control a Mac from an iPhone over iMessage. A background agent reads your texts and can run terminal commands (after you confirm each one), start and stop [Claude Code](https://www.anthropic.com/claude-code) sessions in your project folders, and send you a daily morning brief. Claude Code's permission prompts and results are relayed back to your phone, so you can steer a coding session without sitting at the Mac.

It runs entirely on your own Mac, reading the local Messages database and sending through the Messages app. It is not affiliated with Apple or Anthropic, and it executes commands on your machine when you approve them, so read [Safety](#safety-features) first.

## Requirements

- A Mac, Apple Silicon recommended, that stays on and logged in (Messages must be running)
- macOS Full Disk Access for **Terminal** and for the **Python.app** binary the agent runs under (to read `~/Library/Messages/chat.db`)
- Python 3.13 and [Homebrew](https://brew.sh)
- [Claude Code](https://www.anthropic.com/claude-code) installed, with `claude` on your `PATH`
- An Anthropic API key
- An iPhone with iMessage
- A **second Apple ID** for the Mac (see below)

Optional: Accessibility permission for Python, only needed for the `press enter` family of commands.

## Why a separate Apple ID

If the Mac and your iPhone use the same Apple ID, every message between them is a text to yourself. It shows up twice on the iPhone (a sent bubble in one thread and a received one in another), replying from a notification goes out as the wrong address, and the agent cannot tell its own messages from yours.

Create a second Apple ID for the agent and sign **only that account** into Messages on the Mac (Messages → Settings → iMessage). Then you and the agent are two separate people in one ordinary conversation:

- On the iPhone, start a new iMessage to the agent's Apple ID address. That is the conversation you use.
- Your own Apple ID stays signed in on the iPhone only.
- Set the Apple ID's address as `IMESSAGE_SEND_ADDRESS` (below). The agent sends *from* it and replies *to* your phone number.

New Apple IDs sometimes can't send iMessages for a while. If texts show "Not Delivered", try signing out of iMessage on the Mac and back in, and check that the address is verified.

## Installation

```sh
git clone <this-repo-url> imessage-agent && cd imessage-agent

# 1. Environment (Homebrew's Python, so Full Disk Access can be granted to it)
/opt/homebrew/bin/python3.13 -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Configuration
cp config.example.env .env
$EDITOR .env
venv/bin/python config.py            # checks the values

# 3. Full Disk Access: System Settings > Privacy & Security > Full Disk Access.
#    Add Terminal, and the Python.app binary printed by:
venv/bin/python scripts/install_launchd.py --show-python

# 4. Try it in the foreground (uses Terminal's Full Disk Access)
venv/bin/python imessage_agent.py    # then text "status" to the agent from your iPhone

# 5. Run it in the background and at login
venv/bin/python scripts/install_launchd.py --load
```

Optional extras:

```sh
venv/bin/python scripts/make_apps.py                  # "Start AI Agent" / "Stop AI Agent" apps on your Desktop
venv/bin/python scripts/install_launchd.py --job brief --load   # the daily morning brief
```

After a Homebrew Python upgrade the interpreter path changes: re-run `install_launchd.py --load` and re-add the new `Python.app` to Full Disk Access. After editing the code, restart the agent by texting `restart agent`.

## Configuration

Everything personal lives in `.env` (git-ignored); `config.example.env` documents each value.

| Variable | Required | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Used for free-text commands and the brief. Claude Code sessions started by the bridge also see it and bill this key. |
| `AGENT_PHONE_NUMBER` | yes | The phone number of **your iPhone**, e.g. `+15555550123`. Replies and the brief are sent here. |
| `IMESSAGE_SEND_ADDRESS` | yes | The agent's Apple ID address, signed into Messages on the Mac. Must differ from your own addresses. |
| `PROJECTS_DIR` | no | Where `start claude <name>` opens or creates projects. Default `~/projects`. |
| `EXTRA_USER_ADDRESSES` | no | Other addresses of yours (comma-separated) to also read messages from. |
| `LAUNCHD_LABEL` | no | launchd job label. Default `com.example.imessageagent`. |
| `LOCATION` | no | Where you are, e.g. `Washington DC` (default). Used for the brief's weather and told to Claude so it can tailor the brief. |
| `BRIEF_TIME` | no | Send time for the morning brief, 24h `HH:MM` (default `07:00`). |
| `CHAT_DB` | no | Path to the Messages database. Default `~/Library/Messages/chat.db`. |

The agent only acts on messages from your own addresses (`AGENT_PHONE_NUMBER` plus `EXTRA_USER_ADDRESSES`) that were received by the agent's Apple ID. Texts from anyone else are ignored.

## iMessage commands

Text the agent's address from your iPhone.

| Say | What happens |
|---|---|
| any request, e.g. `show disk usage` | Claude proposes one terminal command; reply `YES` to run it or `NO` to cancel. Riskier commands need `CONFIRM`. |
| `start claude <name>` | Open `<PROJECTS_DIR>/<name>` (created if new) in a Claude Code session. The name is normalized (`Trading Bot` becomes `trading-bot`). A path starting with `/` or `~` is used as given. |
| `start claude` | Asks which project. |
| `stop claude` | Ends the session. If Claude did work, it first updates the project's `CLAUDE.md` (its memory for next time). |
| `list projects` | Lists the folders in `PROJECTS_DIR`. |
| `memory <project>` / `clear memory <project>` | Show or delete a project's `CLAUDE.md`. |
| `status` (or `update`, `status update`) | Agent uptime, whether a session is active, its project, and the last text it sent. |
| `restart agent` | Restarts the agent through launchd. |
| `press enter`, `press space`, `press escape` | Sends that key to the frontmost app (needs Accessibility access for Python). |
| `cancel` | Cancels a pending "Which project?" question. |

During a Claude Code session, permission requests arrive as a menu: reply `1` (yes), `2` (yes and don't ask again for this session), `3`/`no`, or type feedback for Claude. When it finishes you get a summary; reply with a follow-up task, `DONE` to end, or `STOP` at any time. Long messages are split into labelled parts (`Part 1/3: ...`).

## Morning brief

`morning_brief.py` fetches the weather for your `LOCATION`, the top Hugging Face daily papers and AI-related Hacker News stories, asks Claude to summarize them in a few sentences, and texts the result to `AGENT_PHONE_NUMBER`. It starts the conversation, so it always sends to your phone number and never consults reply routing. Run `venv/bin/python morning_brief.py` for one now, or install the daily job with `scripts/install_launchd.py --job brief --load` (time from `BRIEF_TIME`). The agent recognizes the brief and never treats it as a command.

## Safety features

This tool can run commands on your computer from a text message. It has guards, but they are pattern checks, not a sandbox.

- **Confirmation for every command.** Claude only *proposes* one terminal command per request; nothing runs until you reply `YES`. Any other message cancels a pending proposal, so a late `YES` can't run it.
- **Blocklist.** Some commands are refused outright, whatever you reply, and logged to `blocked.log` with a timestamp: `rm -rf` and recursive `rm` on absolute paths, `sudo rm`, `mkfs`/`diskutil erase`, `dd if=`, `format`, `kill -9`, `chmod -R 777`, `chown -R`, writing to devices (`> /dev/...`), piping anything into a shell (`| sh`, `| bash`), `curl | bash` style remote execution, and fork bombs. The check normalizes quoting and flag order, and runs again immediately before execution. It can be bypassed indirectly (for example `find / -delete`), and it does not cover Claude Code's own tool use, which relies on the per-request permission prompts.
- **Elevated confirmation.** Commands using `sudo`, `rm`, `>` to overwrite a file, `pip`/`npm install`, or touching `~/.ssh`, `~/.aws` or any `.env` file require the exact word `CONFIRM` (YES is not enough).
- **Kill switch.** The `Stop AI Agent` app (from `scripts/make_apps.py`) creates the `AGENT_DISABLED` flag, unloads the launchd jobs, and kills the agent, any Claude Code session and the morning brief. `Start AI Agent` removes the flag and starts it again.
- **`AGENT_DISABLED` flag.** While the file `AGENT_DISABLED` exists in the project folder, the agent idles, the bridge refuses to start, and the morning brief skips. launchd can restart the agent but cannot get past it. You can create or delete the file by hand.
- **Scoped access.** Only your configured addresses are read. "Don't ask again" in a Claude Code session lasts for that session only and writes nothing to the project's settings. At the end of a session only `CLAUDE.md` may be written.

## Project layout

| File | Purpose |
|---|---|
| `imessage_agent.py` | The always-on agent: reads messages, handles commands, starts the bridge. |
| `claude_code_bridge.py` | Runs one headless Claude Code session and relays its prompts and results. |
| `imessage_common.py` | Shared sending code: splitting, labelling, echo tracking. |
| `config.py`, `config.example.env` | Settings loaded from `.env`, and a template for it. |
| `morning_brief.py` | The daily brief. |
| `scripts/install_launchd.py` | Generates and loads the launchd jobs. |
| `scripts/make_apps.py` | Builds the Start/Stop kill-switch apps. |

Debugging: `agent.log` and `agent_error.log` (the agent), `bridge.log` (Claude Code sessions), `blocked.log` (refused commands), and `send_debug.log` (the recipient of every outgoing text).

## Limitations

- macOS only; it depends on the Messages database layout and AppleScript, which Apple can change.
- Messages in iMessage groups are not supported.
- If the API key runs out of credit, free-text commands and Claude Code sessions fail; local commands like `status` and `restart agent` keep working.
- Anyone who controls your iPhone or your Apple ID can control this agent. Treat access to the agent's conversation like access to a terminal on the Mac.
