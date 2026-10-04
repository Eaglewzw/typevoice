"""Validate an extracted deb and smoke-test its launcher in an Xvfb session.

Use the system Python dependencies (or an extracted distro dependency via
PYTHONPATH). No microphone, cloud calls, or host package installation.
"""

import argparse
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import tarfile
import tempfile
import time


def test(package):
    package = package.resolve()
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    assert package.with_suffix('.deb.sha256').read_text().split()[0] == digest
    with tempfile.TemporaryDirectory(prefix='typevoice-package-test-') as temporary:
        base = Path(temporary)
        root = base / 'install root'
        subprocess.run(['dpkg-deb', '-x', str(package), str(root)], check=True)
        subprocess.run(['dpkg-deb', '-e', str(package), str(base / 'control')], check=True)
        for line in (base / 'control/md5sums').read_text().splitlines():
            expected, name = line.split(None, 1)
            assert hashlib.md5((root / name).read_bytes()).hexdigest() == expected, name
        archive = base / 'data.tar'
        with archive.open('wb') as stream:
            subprocess.run(['dpkg-deb', '--fsys-tarfile', str(package)], stdout=stream, check=True)
        with tarfile.open(archive) as tar:
            for member in tar:
                assert member.uid == member.gid == 0, member.name
                if member.isdir():
                    assert member.mode == 0o755, (member.name, oct(member.mode))
                assert not any(s in member.name for s in ('.deps', '__pycache__', 'config.json', 'history.db'))
        print('PASS: archive ownership, permissions, file checksums and clean contents')
        launcher = root / 'usr/bin/typevoice'
        env = dict(os.environ, XDG_CONFIG_HOME=str(base / 'config'),
                   XDG_DATA_HOME=str(base / 'data'), XDG_CACHE_HOME=str(base / 'cache'),
                   NO_AT_BRIDGE='1', GIO_USE_VFS='local')

        def run(*args):
            return subprocess.run([str(launcher), *args], env=env, check=True,
                                  capture_output=True, text=True, timeout=10)

        assert 'TypeVoice' in run('--version').stdout
        run('--help')
        run('status')
        run('autostart', 'on')
        desktop = base / 'config/autostart/typevoice.desktop'
        assert 'run --background' in desktop.read_text()
        subprocess.run(['desktop-file-validate', str(desktop)], check=True)
        run('autostart', 'off')
        assert not desktop.exists()
        print('PASS: extracted launcher, CLI and autostart from a path containing spaces')
        app = subprocess.Popen([str(launcher), 'run'], env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
        try:
            time.sleep(1.2)
            assert app.poll() is None, 'packaged app exited at startup'
            run('run', '--background')  # existing instance must stay alive
            assert app.poll() is None
            app.send_signal(signal.SIGTERM)
            output = app.communicate(timeout=8)[0]
            assert app.returncode == 0, output
            assert 'Traceback' not in output and 'CRITICAL' not in output, output
            assert 'TypeVoice started' in output and 'TypeVoice quitting' in output, output
            assert (base / 'config/typevoice/config.json').exists()
            print('PASS: packaged GTK app startup, duplicate instance handling and graceful shutdown')
        finally:
            if app.poll() is None:
                app.terminate()
                app.communicate(timeout=8)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    test(parser.parse_args().package)
