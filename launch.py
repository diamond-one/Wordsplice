"""Double-click launcher; starts the studio locally and opens Chrome or Edge."""
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

def main():
    if not alive():
        python = ROOT / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
        if not python.is_file():
            print('Setting up Wordsplice. This is only needed once.', flush=True)
            subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
        # Retry dependency installation if an earlier first-run setup was interrupted.
        dependencies = subprocess.run([str(python), '-c', 'import faster_whisper, numpy'],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if dependencies.returncode:
            subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], check=True)
        data = ROOT / 'data'
        data.mkdir(exist_ok=True)
        with (data / 'studio.log').open('a', encoding='utf-8') as log:
            subprocess.Popen([str(python), str(ROOT / 'server.py')], cwd=str(ROOT), stdout=log, stderr=log,
                             creationflags=FLAGS, stdin=subprocess.DEVNULL)
        for _ in range(100):
            if alive():
                break
            time.sleep(.1)
        else:
            raise RuntimeError('The studio could not start. See data/studio.log for details. Another program may be using port 8765.')
    open_browser()

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('\nWordsplice: ' + str(error))
        input('Press Enter to close.')
