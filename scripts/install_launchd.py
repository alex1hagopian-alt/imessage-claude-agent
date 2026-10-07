# Generates (and optionally loads) the launchd jobs that keep the agent running and send the morning brief, using this checkout's paths and your .env.
"""Install the launchd jobs.

    python scripts/install_launchd.py --show-python     # path to give Full Disk Access
    python scripts/install_launchd.py --print           # show the agent plist, write nothing
    python scripts/install_launchd.py --load            # write ~/Library/LaunchAgents/<label>.plist and start it
    python scripts/install_launchd.py --job brief --load  # the daily morning brief
    python scripts/install_launchd.py --job all --load

Run it with the project's virtualenv Python so the job finds the installed packages.
The job runs the Python.app binary directly (not venv/bin/python): macOS grants Full
Disk Access to the first binary launchd starts, so that is the one to add in System
Settings. Re-run this after a Homebrew Python upgrade (the path changes).
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import sysconfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config  # noqa: E402


def python_app():
    path = os.path.join(os.path.realpath(sys.base_prefix), 'Resources', 'Python.app', 'Contents', 'MacOS', 'Python')
    if not os.path.exists(path):
        sys.exit(f"Could not find Python.app at {path}. Use Homebrew's python@3.13 to create the venv.")
    return path


def environment():
    path = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin']
    claude = shutil.which('claude')
    if claude and os.path.dirname(claude) not in path:
        path.insert(0, os.path.dirname(claude))  # so the bridge can find `claude`
    return {'PATH': ':'.join(path), 'HOME': os.path.expanduser('~'),
            'PYTHONPATH': sysconfig.get_paths()['purelib'], 'PYTHONUNBUFFERED': '1'}


def job_plist(job):
    base = {'WorkingDirectory': ROOT, 'EnvironmentVariables': environment(), 'RunAtLoad': False}
    if job == 'agent':
        return config.LAUNCHD_LABEL, {
            **base, 'Label': config.LAUNCHD_LABEL,
            'ProgramArguments': [python_app(), os.path.join(ROOT, 'imessage_agent.py')],
            'KeepAlive': True,
            'StandardOutPath': os.path.join(ROOT, 'agent.log'),
            'StandardErrorPath': os.path.join(ROOT, 'agent_error.log')}
    label = f'{config.LAUNCHD_LABEL}.morningbrief'
    return label, {
        **base, 'Label': label,
        'ProgramArguments': [python_app(), os.path.join(ROOT, 'morning_brief.py')],
        'StartCalendarInterval': {'Hour': config.BRIEF_HOUR, 'Minute': config.BRIEF_MINUTE},
        'StandardOutPath': os.path.join(ROOT, 'brief.log'),
        'StandardErrorPath': os.path.join(ROOT, 'brief_error.log')}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--job', choices=['agent', 'brief', 'all'], default='agent')
    parser.add_argument('--output-dir', default=os.path.expanduser('~/Library/LaunchAgents'))
    parser.add_argument('--print', action='store_true', dest='show', help='print the plist(s); write nothing')
    parser.add_argument('--load', action='store_true', help='(re)load the job(s) with launchctl after writing')
    parser.add_argument('--show-python', action='store_true', help='print the Python.app path to add to Full Disk Access')
    args = parser.parse_args()
    if args.show_python:
        print(python_app())
        return
    config.require()
    for job in (['agent', 'brief'] if args.job == 'all' else [args.job]):
        label, plist = job_plist(job)
        if args.show:
            print(plistlib.dumps(plist).decode())
            continue
        os.makedirs(args.output_dir, exist_ok=True)
        path = os.path.join(args.output_dir, f'{label}.plist')
        with open(path, 'wb') as f:
            plistlib.dump(plist, f)
        print(f'wrote {path}')
        if args.load:
            domain = f'gui/{os.getuid()}'
            subprocess.run(['launchctl', 'bootout', f'{domain}/{label}'], capture_output=True)
            result = subprocess.run(['launchctl', 'bootstrap', domain, path], capture_output=True, text=True)
            print(f'loaded {label}' if result.returncode == 0 else f'launchctl bootstrap failed: {result.stderr.strip()}')


if __name__ == '__main__':
    main()
