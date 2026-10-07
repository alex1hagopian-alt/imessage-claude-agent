# Fetches weather and technical AI news, has Claude summarize them, and iMessages the brief to your phone.
"""Morning brief.

    python morning_brief.py             send one brief now and exit (use this from launchd)
    python morning_brief.py --schedule  stay running and send one every day at BRIEF_TIME

The brief starts the conversation, so it has a FIXED destination: your phone number
(AGENT_PHONE_NUMBER), in the chat that belongs to the agent's Apple ID. It never sends
to IMESSAGE_SEND_ADDRESS — that is the Mac's own identity, which would be the Mac
texting itself. Settings come from .env (see config.example.env).
"""
import argparse
import datetime
import os
import re
import subprocess
import time
import urllib.parse

import anthropic
import requests
import schedule

import config

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
HEADERS = {'User-Agent': 'curl/7.68.0'}
BRIEF_CHAT_GUID = f'iMessage;-;{config.AGENT_PHONE_NUMBER}'
# The agent ignores a text that matches this file, so the brief is never mistaken for a command.
BRIEF_TEXT_FILE = '/tmp/brief_text.txt'
SEND_SCRIPT_FILE = '/tmp/send_brief.scpt'
# Same files the agent uses, so the kill switch and the send log cover the brief too.
DISABLED_FLAG = os.path.join(config.PROJECT_DIR, 'AGENT_DISABLED')
SEND_DEBUG_LOG = os.path.join(config.PROJECT_DIR, 'send_debug.log')

# HN front-page stories are kept only if the title matches one of these (word-boundary, case-insensitive)
TECH_KEYWORDS = re.compile(
    r'\b(ai|llms?|gpt|claude|gemini|llama|mistral|qwen|deepseek|transformers?|diffusion|'
    r'fine-?tun\w*|inference|rag|embeddings?|agents?|benchmarks?|open[- ]source|'
    r'pytorch|cuda|gpu|tpu|quantiz\w*|model|models|neural|machine learning|mcp)\b',
    re.IGNORECASE,
)


def get_weather(location=None):
    # LOCATION from .env (default "Washington DC")
    location = location or config.LOCATION
    r = requests.get(f'https://wttr.in/{urllib.parse.quote(location)}?format=3', headers=HEADERS, timeout=15)
    return r.text.strip()


def get_papers(count=3):
    # Community-ranked research papers from Hugging Face daily papers
    r = requests.get('https://huggingface.co/api/daily_papers', headers=HEADERS, timeout=15)
    papers = sorted(r.json(), key=lambda p: p['paper'].get('upvotes', 0), reverse=True)
    lines = []
    for p in papers[:count]:
        abstract = ' '.join(p['paper'].get('summary', '').split())[:300]
        lines.append(f"- {p['title']} ({p['paper'].get('upvotes', 0)} upvotes): {abstract}")
    return lines


def get_hn_tech(count=3):
    # Hacker News front page, filtered to AI/ML/engineering stories
    r = requests.get('https://hn.algolia.com/api/v1/search',
                     params={'tags': 'front_page', 'hitsPerPage': 60}, headers=HEADERS, timeout=15)
    hits = [h for h in r.json()['hits'] if h.get('title') and TECH_KEYWORDS.search(h['title'])]
    return [f"- {h['title']} ({h.get('points', 0)} points on HN)" for h in hits[:count]]


def get_news():
    sections = []
    for title, fetch in (('Trending AI research papers', get_papers),
                         ('Technical AI/ML discussion on Hacker News', get_hn_tech)):
        try:
            lines = fetch()
        except Exception as e:
            print(f'{title} fetch failed: {e}')
            continue
        if lines:
            sections.append(title + ':\n' + '\n'.join(lines))
    return '\n\n'.join(sections) or 'No technical news available today.'


def send_imessage(brief):
    with open(BRIEF_TEXT_FILE, 'w', encoding='utf-8') as f:
        f.write(brief)

    # AppleScript reads the file content and sends it
    script = f'''set briefFile to open for access POSIX file "{BRIEF_TEXT_FILE}"
set briefText to read briefFile as «class utf8»
close access briefFile
tell application "Messages"
send briefText to chat id "{BRIEF_CHAT_GUID}"
end tell'''
    with open(SEND_SCRIPT_FILE, 'w', encoding='utf-8') as f:
        f.write(script)

    # Same runtime proof as imessage_common.py: log the recipient taken from the script that is run
    try:
        recipient = re.search(r'chat id "([^"]*)"', script).group(1)
        with open(SEND_DEBUG_LOG, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.datetime.now()} — sending to: {recipient} | routing=fixed | "
                    f"pid={os.getpid()} proc=morning_brief.py text={brief[:60]!r}\n")
    except Exception:
        pass
    result = subprocess.run(['osascript', SEND_SCRIPT_FILE], capture_output=True, text=True)
    print(f'AppleScript exit code: {result.returncode} {result.stderr.strip()}')
    return result.returncode == 0


def run_brief():
    if os.path.exists(DISABLED_FLAG):
        print('AGENT_DISABLED present — skipping brief')
        return
    print('Running morning brief...')
    location = config.LOCATION
    context = f"{get_weather(location)}\n\n{get_news()}"
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""You are a morning briefing assistant for someone in {location}. Write a brief that's relevant to their area when possible. Write a concise, friendly morning brief in 4-6 sentences: one line for the weather, then the rest on the technical news. The news items below are global, not local, so only tie one to {location} when the connection is genuine, and never invent local news or events. The reader is a technical person, so focus on technical advancements (new models, architectures, techniques, benchmarks, tooling, research results) and say what is actually new or interesting about each. Skip business, politics, and general-interest framing. If an item has no technical substance, leave it out.

{context}"""}],
    )
    brief = message.content[0].text
    print('\n--- MORNING BRIEF ---')
    print(brief)
    print('Brief sent via iMessage!' if send_imessage(brief) else 'Sending failed — see the error above.')


def main():
    config.require()
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--schedule', action='store_true',
                        help=f'keep running and send daily at BRIEF_TIME ({config.BRIEF_TIME})')
    args = parser.parse_args()
    if not args.schedule:
        return run_brief()
    schedule.every().day.at(config.BRIEF_TIME).do(run_brief)
    print(f'Scheduler running — brief will send daily at {config.BRIEF_TIME}.')
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == '__main__':
    main()
