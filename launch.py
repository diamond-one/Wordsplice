"""Double-click launcher; starts the studio locally and opens Chrome or Edge."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

def alive():
    try:
        data = json.load(urllib.request.urlopen(URL + '/api/status', timeout=1))
        return data.get('app') == 'wordsplice'
    except Exception:
        return False

def open_browser():
    roots = [os.environ.get('PROGRAMFILES', ''), os.environ.get('PROGRAMFILES(X86)', ''), os.environ.get('LOCALAPPDATA', '')]
    for browser in ['Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe']:
        for root in roots:
            candidate = Path(root) / browser
            if root and candidate.is_file():
                subprocess.Popen([str(candidate), URL], creationflags=FLAGS)
                return
    webbrowser.open(URL)

def run_setup(command, log_path, message):
    """Keep progress visible while retaining installer output, even if interrupted."""
    print(f'{message}\nDetails: {log_path}', flush=True)
    with log_path.open('a', encoding='utf-8') as log:
        log.write(f'\n[{time.strftime("%Y-%m-%d %H:%M:%S")}] {message}\n')
        log.flush()
        process = subprocess.Popen(command, stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, creationflags=FLAGS)
        next_update = time.monotonic() + 10
        try:
            while process.poll() is None:
                if time.monotonic() >= next_update:
                    print('Setup is still working. Keep this window open.', flush=True)
                    next_update = time.monotonic() + 10
                time.sleep(.25)
        except KeyboardInterrupt:
            process.terminate()
            process.wait()
            raise
    if process.returncode:
        raise RuntimeError(f'{message} failed (exit code {process.returncode}). '
                           f'See {log_path}. Run Start Wordsplice again to retry setup.')


def dependencies_ready(python, log_path):
    with log_path.open('a', encoding='utf-8') as log:
        log.write('\nChecking audio dependencies...\n')
        log.flush()
        try:
            result = subprocess.run([str(python), '-c', 'import faster_whisper, numpy, av'],
                                    stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                    creationflags=FLAGS, timeout=90)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'Audio dependency checks took too long. See {log_path}.') from None
    return result.returncode == 0


def prepare_runtime(data):
    python = ROOT / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    log_path = data / 'setup.log'
    if not python.is_file():
        run_setup([sys.executable, '-m', 'venv', str(ROOT / '.venv')], log_path,
                  'Creating the local Python environment')
    print('Checking Wordsplice setup...', flush=True)
    if not dependencies_ready(python, log_path):
        print('Setup is incomplete. Installing or repairing missing audio libraries.', flush=True)
        # An interrupted venv creation can leave Python present but pip missing.
        run_setup([str(python), '-m', 'ensurepip', '--upgrade'], log_path,
                  'Preparing the package installer')
        run_setup([str(python), '-u', '-m', 'pip', '--disable-pip-version-check',
                   'install', '--no-input', '--progress-bar', 'off', '-r', str(ROOT / 'requirements.txt')],
                  log_path, 'Installing audio libraries (this may take a few minutes)')
        if not dependencies_ready(python, log_path):
            raise RuntimeError(f'Installation finished, but audio libraries still cannot load. See {log_path}.')
    print('Audio libraries are ready.', flush=True)
    return python


def main(no_browser=False):
    if not alive():
        data = Path(os.environ.get('WORDSPLICE_DATA', str(ROOT / 'data'))).resolve()
        data.mkdir(parents=True, exist_ok=True)
        python = prepare_runtime(data)
        print('Starting the local studio...', flush=True)
        with (data / 'studio.log').open('a', encoding='utf-8') as log:
            process = subprocess.Popen([str(python), str(ROOT / 'server.py')], cwd=str(ROOT), stdout=log, stderr=log,
                                       creationflags=FLAGS, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if alive():
                break
            if process.poll() is not None:
                raise RuntimeError(f'The studio exited (code {process.returncode}). See {data / "studio.log"}.')
            time.sleep(.25)
        else:
            raise RuntimeError(f'The studio did not respond in time. See {data / "studio.log"}. '
                               'Another program may be using port 8765.')
    print(f'Wordsplice is running at {URL}. Speech alignment may still be warming up.', flush=True)
    if not no_browser:
        open_browser()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser', action='store_true', help='Start the studio without opening a browser')
    args = parser.parse_args()
    exit_code = 0
    try:
        main(no_browser=args.no_browser)
    except KeyboardInterrupt:
        print('\nSetup stopped. Run Start Wordsplice again to resume.')
        exit_code = 130
    except Exception as error:
        print('\nWordsplice: ' + str(error))
        exit_code = 1
    if exit_code and sys.stdin.isatty():
        try:
            input('Press Enter to close.')
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(exit_code)
