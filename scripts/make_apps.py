# Builds the double-clickable "Start AI Agent" and "Stop AI Agent" kill-switch apps for this checkout and your .env.
"""Build the kill-switch apps.

    python scripts/make_apps.py                    # writes both apps to ~/Desktop
    python scripts/make_apps.py --output-dir DIR

Stop AI Agent: creates the AGENT_DISABLED flag (so nothing restarts), unloads the
launchd job and kills the agent, any Claude Code bridge and the morning brief.
Start AI Agent: removes the flag and loads the job again.
They are AppleScript applets created with osacompile, so they contain your paths and
are git-ignored; rebuild them with this script on each machine.
"""
import argparse
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config  # noqa: E402


def q(text):
    """An AppleScript string literal."""
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def sources():
    flag = os.path.join(ROOT, 'AGENT_DISABLED')
    label = config.LAUNCHD_LABEL
    plist = os.path.expanduser(f'~/Library/LaunchAgents/{label}.plist')
    stop = f"""-- Flag first so nothing restarts, then unload the jobs, then kill stragglers.
do shell script {q(f"touch '{flag}'")}
do shell script {q(f"launchctl bootout gui/$(id -u)/{label} >/dev/null 2>&1; launchctl bootout gui/$(id -u)/{label}.morningbrief >/dev/null 2>&1; pkill -f '[c]laude_code_bridge.py'; pkill -f '[m]orning_brief.py'; pkill -f '[i]message_agent.py'; rm -f /tmp/claude_bridge.lock; true")}
display notification "AI Agent stopped — all scripts have been shut down" with title "Stop AI Agent"
"""
    start = f"""do shell script {q(f"rm -f '{flag}'")}
do shell script {q(f"launchctl bootstrap gui/$(id -u) '{plist}' >/dev/null 2>&1; launchctl kickstart gui/$(id -u)/{label} >/dev/null 2>&1; true")}
display notification "AI Agent started" with title "Start AI Agent"
"""
    return {'Stop AI Agent': stop, 'Start AI Agent': start}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--output-dir', default=os.path.expanduser('~/Desktop'))
    args = parser.parse_args()
    config.require()
    os.makedirs(args.output_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, script in sources().items():
            source = os.path.join(tmp, f'{name}.applescript')
            with open(source, 'w', encoding='utf-8') as f:
                f.write(script)
            target = os.path.join(args.output_dir, f'{name}.app')
            result = subprocess.run(['osacompile', '-o', target, source], capture_output=True, text=True)
            print(f'built {target}' if result.returncode == 0 else f'osacompile failed for {name}: {result.stderr.strip()}')


if __name__ == '__main__':
    main()
