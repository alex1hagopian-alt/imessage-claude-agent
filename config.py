# Loads every personal setting from the .env file next to this script, so no script has to contain addresses, phone numbers or paths.
"""Settings for the iMessage agent, read from `.env` in this folder.

Copy `config.example.env` to `.env` and fill it in. Run `python config.py` to
check it. Scripts import values from here; `require()` is called at start-up so
a missing or still-placeholder value stops the script with a clear message.
"""
import os
import re
import sys

from dotenv import load_dotenv

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# Explicit path: works under launchd and from any working directory.
load_dotenv(os.path.join(PROJECT_DIR, '.env'))


def _get(name, default=''):
    return os.environ.get(name, default).strip()


def _csv(name):
    return tuple(part.strip() for part in _get(name).split(',') if part.strip())


# --- Required --------------------------------------------------------------
ANTHROPIC_API_KEY = _get('ANTHROPIC_API_KEY')
# The phone number of the iPhone the agent talks to (yours). Replies and the
# morning brief are sent here, and you text the agent from it.
AGENT_PHONE_NUMBER = _get('AGENT_PHONE_NUMBER')
# The Apple ID that Messages on the Mac is signed into: the agent's own
# iMessage address. You text THIS address; the Mac sends from it.
IMESSAGE_SEND_ADDRESS = _get('IMESSAGE_SEND_ADDRESS')

# --- Optional --------------------------------------------------------------
# Other addresses of yours the iPhone might text the agent from (comma-separated).
# Their messages are read; replies still go to AGENT_PHONE_NUMBER.
EXTRA_USER_ADDRESSES = _csv('EXTRA_USER_ADDRESSES')
PROJECTS_DIR = os.path.expanduser(_get('PROJECTS_DIR') or '~/projects')
CHAT_DB = os.path.expanduser(_get('CHAT_DB') or '~/Library/Messages/chat.db')
LAUNCHD_LABEL = _get('LAUNCHD_LABEL') or 'com.example.imessageagent'
LOCATION = _get('LOCATION') or 'Washington DC'  # used for the morning brief's weather, and told to Claude so it can tailor the brief
BRIEF_TIME = _get('BRIEF_TIME') or '07:00'  # 24-hour HH:MM, local time


def _parse_time(text):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', text)
    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        return int(match.group(1)), int(match.group(2))
    return None


BRIEF_HOUR, BRIEF_MINUTE = _parse_time(BRIEF_TIME) or (7, 0)

_PLACEHOLDER = re.compile(r'your-|yourname|x{4,}|example\.com|changeme', re.IGNORECASE)


def problems():
    """Human-readable list of what is wrong with the configuration."""
    found = []
    if not os.path.exists(os.path.join(PROJECT_DIR, '.env')):
        found.append(f"no .env file in {PROJECT_DIR} — copy config.example.env to .env and fill it in")
    if not ANTHROPIC_API_KEY or _PLACEHOLDER.search(ANTHROPIC_API_KEY):
        found.append("ANTHROPIC_API_KEY is missing or still the placeholder")
    if not re.fullmatch(r'\+\d{7,15}', AGENT_PHONE_NUMBER) or _PLACEHOLDER.search(AGENT_PHONE_NUMBER):
        found.append("AGENT_PHONE_NUMBER must be your number in international form, e.g. +15555550123")
    if '@' not in IMESSAGE_SEND_ADDRESS or _PLACEHOLDER.search(IMESSAGE_SEND_ADDRESS):
        found.append("IMESSAGE_SEND_ADDRESS must be the agent's Apple ID email, e.g. agent@icloud.com")
    elif IMESSAGE_SEND_ADDRESS.lower() in {a.lower() for a in (AGENT_PHONE_NUMBER, *EXTRA_USER_ADDRESSES)}:
        found.append("IMESSAGE_SEND_ADDRESS must differ from your own addresses (that is what avoids double bubbles)")
    if _PLACEHOLDER.search(PROJECTS_DIR):
        found.append("PROJECTS_DIR is still the placeholder (/Users/yourname/projects)")
    if _parse_time(BRIEF_TIME) is None:
        found.append("BRIEF_TIME must be a 24-hour time like 07:00")
    return found


def require():
    """Stop with a clear message if the configuration is incomplete."""
    found = problems()
    if found:
        sys.exit("Configuration problem(s):\n  - " + "\n  - ".join(found)
                 + f"\nSee config.example.env. (Checked: {os.path.join(PROJECT_DIR, '.env')})")


if __name__ == '__main__':
    found = problems()
    if found:
        print("Configuration problem(s):\n  - " + "\n  - ".join(found))
        sys.exit(1)
    print("Configuration OK")
    print(f"  texts go to        : {AGENT_PHONE_NUMBER}")
    print(f"  agent Apple ID     : {IMESSAGE_SEND_ADDRESS}")
    print(f"  also reads from    : {', '.join(EXTRA_USER_ADDRESSES) or '(none)'}")
    print(f"  projects folder    : {PROJECTS_DIR}")
    print(f"  morning brief      : {BRIEF_TIME} for {LOCATION}")
