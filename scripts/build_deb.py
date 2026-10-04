#!/usr/bin/env python3
"""Build a small, architecture-independent deb using system Python packages.

No root, pip, network, or third-party build tool is required.
"""

import argparse
import gzip
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]


def build(output):
    version = re.search(r'__version__ = "([^"]+)"',
                        (PROJECT / 'typevoice/__init__.py').read_text()).group(1) + '-1'
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f'typevoice_{version}_all.deb'
    with tempfile.TemporaryDirectory(prefix='typevoice-deb-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        app = root / 'usr/share/typevoice'
        app.mkdir(parents=True)
        shutil.copytree(PROJECT / 'typevoice', app / 'typevoice',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(PROJECT / 'assets', app / 'assets')
        shutil.copy2(PROJECT / 'packaging/run-installed.sh', app / 'run.sh')
        binary = root / 'usr/bin/typevoice'
        binary.parent.mkdir(parents=True)
        shutil.copy2(PROJECT / 'packaging/launcher', binary)
        for executable in (binary, app / 'run.sh'):
            executable.chmod(0o755)
        desktop = root / 'usr/share/applications/typevoice.desktop'
        desktop.parent.mkdir(parents=True)
        shutil.copy2(PROJECT / 'packaging/typevoice.desktop', desktop)
        icon = root / 'usr/share/icons/hicolor/scalable/apps/typevoice.svg'
        icon.parent.mkdir(parents=True)
        shutil.copy2(PROJECT / 'assets/typevoice.svg', icon)
        doc = root / 'usr/share/doc/typevoice'
        doc.mkdir(parents=True)
        shutil.copy2(PROJECT / 'packaging/copyright', doc / 'copyright')
        shutil.copy2(PROJECT / 'LICENSE', doc / 'COPYING')
        shutil.copy2(PROJECT / 'NOTICE', doc / 'NOTICE')
        (doc / 'README.md.gz').write_bytes(gzip.compress((PROJECT / 'README.md').read_bytes(), mtime=0))
        changelog = f'''typevoice ({version}) unstable; urgency=medium

  * Add Linux dictation HUD, settings panel and Debian desktop integration.
  * Rename application, package and modules to TypeVoice.
  * Import legacy configuration and encrypted history without deleting originals.

 -- Eaglewzw <1460853569@qq.com>  Sun, 04 Oct 2026 12:00:00 +0800
'''
        (doc / 'changelog.Debian.gz').write_bytes(gzip.compress(changelog.encode(), mtime=0))
        installed_size = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
        meta = root / 'DEBIAN'
        meta.mkdir()
        (meta / 'control').write_text(f'''Package: typevoice
Version: {version}
Section: utils
Priority: optional
Architecture: all
Maintainer: Eaglewzw <1460853569@qq.com>
Installed-Size: {(installed_size + 1023) // 1024}
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, python3-cairo, python3-numpy, python3-cryptography, python3-xlib, gir1.2-gtk-3.0, alsa-utils, pulseaudio-utils, xdg-utils
Recommends: gir1.2-ayatanaappindicator3-0.1
Conflicts: suiyan
Replaces: suiyan
Homepage: https://github.com/Eaglewzw/typevoice
Description: Voice typing with a compact desktop overlay for X11
 TypeVoice records speech with a global shortcut, transcribes and optionally
 polishes it using your own cloud API credentials, then pastes the result
 into the focused application. Includes a settings panel and tray menu.
 Requires an X11 desktop session. See the included copyright and NOTICE.
''')
        sums = []
        for path in sorted((root / 'usr').rglob('*')):
            if path.is_file():
                if path not in (binary, app / 'run.sh'):
                    path.chmod(0o644)
                sums.append(f'{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(root)}')
            elif path.is_dir():
                path.chmod(0o755)
        (meta / 'md5sums').write_text('\n'.join(sums) + '\n')
        (root / 'usr').chmod(0o755)
        meta.chmod(0o755)
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(root), str(artifact)], check=True)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_suffix('.deb.sha256').write_text(f'{digest}  {artifact.name}\n')
    print(artifact)
    return artifact


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=PROJECT / 'dist')
    args = parser.parse_args()
    build(args.output.resolve())
