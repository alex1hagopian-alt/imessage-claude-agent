# Research consultant: reads a project's CLAUDE.md, searches GitHub for tools/skills/harnesses that fit it, and writes a ranked digest.
"""Research agent (version 1).

    python research_agent.py <project>            print a ranked digest for ~/projects/<project>
    python research_agent.py <project> --send     same, and also text it to the owner
    python research_agent.py <project> --topic "options data APIs"   research one topic for that project

Auto mode (on by default; text "research off" / "research on"): before each task of a Claude Code session the
bridge calls plan(), where a cheap model decides whether the task is worth researching and whether a recent digest
already covers it. If it is, the bridge holds the task, runs research(), and hands Claude the digest with the task.
Max MAX_RUNS_PER_DAY runs per project per day. State (on/off, recent topics and digests) is in research_state.json.

The agent also profiles every OTHER project in ~/projects so the digest can say what the owner already has
(an API already set up, a library already in use) before recommending anything new.

Steps: (1) profile the project from its CLAUDE.md/README (cached in research_profiles/),
(2) have a cheap model write a few GitHub search queries, (3) fetch candidates from the
GitHub search API, (4) have a model pick and rank the best ones. The digest is also saved
to research_inbox/<project>.md so a Claude Code session in that project can read it.

Costs are capped: MAX_QUERIES searches, MAX_PER_QUERY repos each, one ranking call.
Respects the kill switch (AGENT_DISABLED). research_profiles/ and research_inbox/ describe
private projects and are git-ignored.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

import anthropic
import requests

import config

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None, timeout=90)
CHEAP_MODEL = 'claude-haiku-5-5'   # profiling and query writing
RANK_MODEL = 'claude-sonnet-5-5'   # final judgement
PROFILE_DIR = os.path.join(config.PROJECT_DIR, 'research_profiles')
INBOX_DIR = os.path.join(config.PROJECT_DIR, 'research_inbox')
DISABLED_FLAG = os.path.join(config.PROJECT_DIR, 'AGENT_DISABLED')
MAX_QUERIES = 4
MAX_PER_QUERY = 8
MAX_NOTES_CHARS = 40_000   # of one project's notes sent for profiling
TOP_N = 5
STATE_FILE = os.path.join(config.PROJECT_DIR, 'research_state.json')
GITHUB_HEADERS = {'Accept': 'application/vnd.github+json', 'User-Agent': 'research-agent'}


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)  # atomic: the agent and a bridge may both write


def is_enabled():
    return load_state().get('enabled', True)  # auto mode is on unless the owner turned it off


def set_enabled(on):
    state = load_state()
    state['enabled'] = bool(on)
    save_state(state)


MAX_RUNS_PER_DAY = 3     # per project, a cost safety on top of the topic dedupe
KEEP_TOPICS = 6          # recent digests remembered per project
REUSE_DAYS = 7           # a digest older than this is never reused


def topics_for(name):
    return load_state().get('topics', {}).get(name, [])


def record(name, topic, digest):
    state = load_state()
    items = state.setdefault('topics', {}).setdefault(name, [])
    items.append({'topic': topic, 'date': str(datetime.date.today()), 'digest': digest})
    state['topics'][name] = items[-KEEP_TOPICS:]
    save_state(state)


def runs_today(name):
    today = str(datetime.date.today())
    return sum(1 for t in topics_for(name) if t['date'] == today)


def plan(project_path, task):
    """Decide what the bridge should do with a task before Claude Code sees it. One cheap-model call.
    Returns {'action': 'skip'} (no research: off, kill switch, trivial task, daily cap, or any failure),
    {'action': 'reuse', 'topic', 'digest'} (a recent digest already covers it) or {'action': 'run', 'topic'}.
    Never raises: research must not break a session."""
    skip = {'action': 'skip'}
    try:
        name = os.path.basename(os.path.normpath(project_path))
        if (not is_enabled() or os.path.exists(DISABLED_FLAG) or not (task or '').strip()
                or os.path.dirname(os.path.normpath(project_path)) != os.path.normpath(config.PROJECTS_DIR)):
            return skip
        cutoff = str(datetime.date.today() - datetime.timedelta(days=REUSE_DAYS))
        recent = [t for t in topics_for(name) if t['date'] >= cutoff]
        cache = os.path.join(PROFILE_DIR, f'{name}.md')
        prof = open(cache, encoding='utf-8').read() if os.path.isfile(cache) else '(no profile yet)'
        listing = '\n'.join(f"{i}: {t['topic']}" for i, t in enumerate(recent)) or '(none)'
        reply = ask(CHEAP_MODEL,
                    f"Project '{name}':\n{prof}\n\nThe owner just asked their coding assistant:\n\"{' '.join(task.split())[:1500]}\"\n\n"
                    "Would researching open-source tools, libraries, APIs, Claude Code skills or approaches first "
                    "materially help? Answer true only if the task involves choosing, adding or replacing a "
                    "tool/library/API/service, or a significant new capability or change of approach. Answer false "
                    "for small edits, bug fixes, wording, refactors, questions and anything routine.\n"
                    f"Recent research already done for this project:\n{listing}\n"
                    "If one of those topics already covers this task, give its number as covered_by.\n"
                    'Reply with JSON only: {"research": true|false, "topic": "<=12 word topic", "covered_by": number|null}',
                    max_tokens=2000)
        data = parse_json(reply)
        covered = data.get('covered_by')
        if isinstance(covered, int) and not isinstance(covered, bool) and 0 <= covered < len(recent):
            # free and useful even when the task itself is routine ("use alpaca" after researching brokers)
            return {'action': 'reuse', 'topic': recent[covered]['topic'], 'digest': recent[covered]['digest']}
        if not data.get('research'):
            return skip
        topic = str(data.get('topic') or 'general').strip()[:120]
        if runs_today(name) >= MAX_RUNS_PER_DAY:
            return skip
        return {'action': 'run', 'topic': topic}
    except Exception as e:
        log(f"research plan failed: {e}")
        return skip


def log(message):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {message}", file=sys.stderr)


def ask(model, prompt, max_tokens=4000):
    response = client.messages.create(model=model, max_tokens=max_tokens,
                                      messages=[{'role': 'user', 'content': prompt}])
    text = ''.join(block.text for block in response.content if block.type == 'text').strip()
    if not text:  # the model can spend a small max_tokens entirely on thinking
        raise ValueError(f"empty model reply (stop_reason={response.stop_reason}); max_tokens={max_tokens} too small?")
    return text


def parse_json(text):
    """The model's JSON, tolerating a code fence or a sentence around it."""
    match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON in model reply: {text[:200]!r}")
    return json.loads(match.group(1))


def project_dir(project):
    name = re.sub(r'[^a-z0-9-]', '', re.sub(r'[\s_]+', '-', project.strip().lower()))
    path = os.path.join(config.PROJECTS_DIR, name)
    if not name or not os.path.isdir(path):
        sys.exit(f"No project folder named '{name or project}' in {config.PROJECTS_DIR}.")
    return name, path


def read_notes(path):
    for filename in ('CLAUDE.md', 'README.md'):
        full = os.path.join(path, filename)
        if os.path.isfile(full):
            with open(full, encoding='utf-8', errors='replace') as f:
                return f.read()[:MAX_NOTES_CHARS], full
    return '', None


def profile(name, path):
    """Short description of the project, rebuilt only when its notes change."""
    notes, source = read_notes(path)
    if not source:
        return f"A project called {name}. It has no CLAUDE.md or README yet, so little is known about it."
    os.makedirs(PROFILE_DIR, exist_ok=True)
    cache = os.path.join(PROFILE_DIR, f'{name}.md')
    mtime = os.path.getmtime(source)
    if os.path.isfile(cache) and os.path.getmtime(cache) >= mtime:
        with open(cache, encoding='utf-8') as f:
            return f.read()
    log(f"profiling {name} from {os.path.basename(source)}")
    text = ask(CHEAP_MODEL,
               "Summarise this software project for someone who will search for useful tools, libraries, "
               "Claude Code skills, MCP servers and agent harnesses for it. Plain text, under 250 words: "
               "what it does, the stack, current goals and pain points, and anything already tried or rejected.\n\n"
               f"Project notes:\n{notes}")
    with open(cache, 'w', encoding='utf-8') as f:
        f.write(text)
    return text


def other_profiles(current):
    """{project: profile} for every other project that has notes (cached, so cheap after the first run)."""
    found = {}
    for entry in sorted(os.listdir(config.PROJECTS_DIR), key=str.lower):
        path = os.path.join(config.PROJECTS_DIR, entry)
        if entry.startswith('.') or entry == current or not os.path.isdir(path) or not read_notes(path)[1]:
            continue
        try:
            found[entry] = profile(entry, path)
        except anthropic.APIError as e:
            log(f"could not profile {entry}: {e}")
    return found


def others_text(others):
    return '\n\n'.join(f"[{n}]\n{p}" for n, p in others.items()) or '(none)'


def write_queries(name, prof, topic, others):
    focus = f"Focus on this topic: {topic}\n" if topic else ""
    reply = ask(CHEAP_MODEL,
                f"Project '{name}':\n{prof}\n\n{focus}"
                f"The owner's other projects (do not search for what these already provide):\n{others_text(others)}\n\n"
                f"Write {MAX_QUERIES} different GitHub repository search queries (short keyword phrases, no "
                "qualifiers like stars: or language:) that would find open-source tools, libraries, Claude Code "
                "skills, MCP servers or agent harnesses useful to this project. "
                'Reply with JSON only: {"queries": ["...", "..."]}')
    return [str(q) for q in parse_json(reply)['queries']][:MAX_QUERIES]


def github_search(query):
    response = requests.get('https://api.github.com/search/repositories', headers=GITHUB_HEADERS, timeout=30,
                            params={'q': f'{query} archived:false', 'sort': 'stars', 'order': 'desc',
                                    'per_page': MAX_PER_QUERY})
    if response.status_code == 403:
        log("GitHub rate limit reached; using what was found so far")
        return None
    response.raise_for_status()
    return response.json().get('items', [])


def gather(queries):
    found = {}
    for i, query in enumerate(queries):
        if i:
            time.sleep(7)  # unauthenticated search allows about 10 requests a minute
        log(f"searching GitHub: {query}")
        try:
            items = github_search(query)
        except requests.RequestException as e:
            log(f"search failed: {e}")
            continue
        if items is None:
            break
        for repo in items:
            found.setdefault(repo['full_name'], {
                'repo': repo['full_name'], 'url': repo['html_url'], 'stars': repo['stargazers_count'],
                'pushed': (repo.get('pushed_at') or '')[:10], 'license': (repo.get('license') or {}).get('spdx_id'),
                'description': (repo.get('description') or '')[:200]})
    return list(found.values())


def rank(name, prof, topic, candidates, others):
    listing = '\n'.join(f"- {c['repo']} | {c['stars']} stars | last push {c['pushed']} | {c['license']} | {c['description']}"
                        for c in candidates)
    focus = f"The owner asked specifically about: {topic}\n" if topic else ""
    reply = ask(RANK_MODEL,
                f"Today is {datetime.date.today()}. Project '{name}':\n{prof}\n\n{focus}"
                f"The owner's other projects:\n{others_text(others)}\n\n"
                f"Candidate GitHub repos:\n{listing}\n\n"
                "First list what the owner ALREADY HAS in those other projects (APIs/accounts already set up, "
                "libraries, scripts, patterns) that this project can reuse, so they never have to point at another "
                "project's notes. Only include real, specific overlaps, naming the project; an empty list is fine. "
                f"Then pick at most {TOP_N} that would genuinely help this project, best first. Skip anything stale "
                "(no push in over a year), unlicensed when that matters, off-topic, or already what the project uses. "
                "For each give one concrete sentence on how it would help THIS project and one on any risk. "
                'Reply with JSON only: {"reuse": [{"project": "name", "what": "..."}], '
                '"picks": [{"repo": "owner/name", "why": "...", "risk": "..."}]}. '
                'Use only repos from the list. If nothing fits, use "picks": [].',
                max_tokens=6000)
    by_name = {c['repo']: c for c in candidates}
    picks = []
    data = parse_json(reply)
    reuse = [r for r in data.get('reuse', []) if r.get('project') in others and r.get('what')]
    for pick in data.get('picks', []):
        base = by_name.get(pick.get('repo'))
        if base:
            picks.append({**base, 'why': pick.get('why', ''), 'risk': pick.get('risk', '')})
    return picks[:TOP_N], reuse


def format_digest(name, topic, picks, reuse, queries):
    title = f"Research for {name}" + (f" — {topic}" if topic else "")
    lines = [title, ""]
    if reuse:
        lines += ["Already in your other projects:"] + [f"- {r['project']}: {r['what']}" for r in reuse] + [""]
    if not picks:
        lines.append(f"No new tools worth recommending turned up (searched: {'; '.join(queries)}).")
        return '\n'.join(lines).strip()
    for i, p in enumerate(picks, 1):
        lines += [f"{i}. {p['repo']} ({p['stars']:,} stars, pushed {p['pushed']}, {p['license'] or 'no license'})",
                  f"   {p['why']}", f"   Risk: {p['risk']}", f"   {p['url']}", ""]
    return '\n'.join(lines).strip()


def research(project, topic=None):
    name, path = project_dir(project)
    prof = profile(name, path)
    others = other_profiles(name)
    queries = write_queries(name, prof, topic, others)
    candidates = gather(queries)
    log(f"{len(candidates)} candidates")
    picks, reuse = rank(name, prof, topic, candidates, others) if candidates else ([], [])
    digest = format_digest(name, topic, picks, reuse, queries)
    os.makedirs(INBOX_DIR, exist_ok=True)
    with open(os.path.join(INBOX_DIR, f'{name}.md'), 'w', encoding='utf-8') as f:
        f.write(f"<!-- generated {datetime.datetime.now():%Y-%m-%d %H:%M} -->\n{digest}\n")
    if picks or reuse:
        record(name, topic or 'general overview', digest)
    return digest


def send_to_owner(text):
    import imessage_common
    imessage_common.set_reply_target(imessage_common.person_addresses(imessage_common.OWNER)[0])
    imessage_common.send_imessage(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('project')
    parser.add_argument('--topic')
    parser.add_argument('--send', action='store_true', help='also text the digest to the owner')
    args = parser.parse_args()
    if os.path.exists(DISABLED_FLAG):
        sys.exit("Kill switch is engaged (AGENT_DISABLED exists); not running.")
    config.require()
    try:
        digest = research(args.project, args.topic)
    except (anthropic.APIError, ValueError, requests.RequestException) as e:
        digest = f"Research on {args.project} failed: {e}"
    print(digest)
    if args.send:
        send_to_owner(digest)


if __name__ == '__main__':
    main()
