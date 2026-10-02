"""Build one official EXE with a Windows/Python-only DLL search path."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', default='dist', help='Output directory inside this project')
    args = parser.parse_args()
    output = (ROOT / args.dist).resolve()
    if not output.is_relative_to(ROOT) or output == ROOT:
        parser.error('Build output must be a subdirectory of this project')
    unexpected = [p.name for p in output.glob('*.exe') if p.name != 'Auto-Continue.exe']
    if unexpected:
        parser.error('Move test binaries out of the release directory: ' + ', '.join(unexpected))
    env = os.environ.copy()
    windows = Path(env.get('SystemRoot', r'C:\Windows'))
    python = Path(sys.executable).resolve().parent
    env['PATH'] = os.pathsep.join(str(p) for p in
        dict.fromkeys([python, python / 'Scripts', Path(sys.base_prefix), windows / 'System32', windows]))
    for key in ('PYTHONPATH', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
        env.pop(key, None)
    subprocess.run([sys.executable, '-m', 'PyInstaller', str(ROOT / 'Auto-Continue.spec'),
                    '--clean', '--noconfirm', '--distpath', str(output),
                    '--workpath', str(output.parent / (output.name + '-build'))],
                   env=env, cwd=ROOT, check=True)
    binaries = sorted(p.name for p in output.glob('*.exe'))
    if binaries != ['Auto-Continue.exe']:
        raise RuntimeError('Release must contain exactly one Auto-Continue.exe')
    print('Built', output / 'Auto-Continue.exe')

if __name__ == '__main__':
    main()
