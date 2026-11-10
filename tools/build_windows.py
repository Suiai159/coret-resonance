"""Build on Windows, then ship a relocatable onedir ZIP with rebuild sources."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coret import __version__


def make_icon(destination):
    # Rasterize the existing vector asset, without adding an imaging dependency.
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(str(ROOT / 'assets/coret.svg'))
    if not renderer.isValid():
        raise RuntimeError('Invalid application SVG icon')
    images = []
    for size in (16, 32, 48, 64, 128, 256):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, 'PNG'):
            raise RuntimeError('Cannot render application icon')
        images.append((size, bytes(data)))
    offset = 6 + 16 * len(images)
    entries = []
    for size, data in images:
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    destination.write_bytes(struct.pack('<HHH', 0, 1, len(images)) + b''.join(entries) + b''.join(data for _, data in images))



def make_version_resource(destination):
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo,
        StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)
    parts = tuple(map(int, __version__.split('-', 1)[0].split('.')))
    if len(parts) != 3:
        raise ValueError('Expected a three-part desktop version')
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=parts + (0,), prodvers=parts + (0,), mask=0x3f,
                          flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
        kids=[StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', 'CoreT'),
            StringStruct('FileDescription', 'CoreT Desktop - Windows x64'),
            StringStruct('FileVersion', __version__),
            StringStruct('ProductName', 'CoreT Desktop'),
            StringStruct('ProductVersion', __version__),
            StringStruct('OriginalFilename', 'CoreT-Desktop.exe'),
        ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])
    destination.write_text(str(info), encoding='utf-8')


def source_revision():
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                           text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT,
                                             text=True, stderr=subprocess.DEVNULL).strip())
        return revision, dirty
    except (OSError, subprocess.CalledProcessError):
        # Rebuilding from the shipped source/ directory does not require Git.
        return None, None

def main():
    if sys.platform != 'win32' or struct.calcsize('P') != 8 or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Build on Windows with x64 Python 3.10-3.12; cross-compilation is not supported.')
    if not (3, 10) <= sys.version_info[:2] <= (3, 12):
        raise SystemExit('Pinned runtime dependencies require Python 3.10-3.12.')
    # Fail before PyInstaller removes any old files if the running image is locked.
    executable = ROOT / 'dist' / 'CoreT-Desktop' / 'CoreT-Desktop.exe'
    if executable.exists():
        try:
            with executable.open('r+b'):
                pass
        except PermissionError:
            raise SystemExit('Please close CoreT Desktop before replacing the release; existing files were not modified.')
    build = ROOT / '.packaging' / 'windows'
    build.mkdir(parents=True, exist_ok=True)
    icon = build / 'coret.ico'
    make_icon(icon)
    version_resource = build / 'windows-version.txt'
    make_version_resource(version_resource)
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(build / 'cache'))
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
               '--name', 'CoreT-Desktop', '--icon', str(icon), '--version-file', str(version_resource), '--noupx',
               '--distpath', str(ROOT / 'dist'), '--workpath', str(build / 'work'),
               '--specpath', str(build), '--paths', str(ROOT), '--hidden-import', 'importlib.resources',
               # Qt6 SVG support is required for the runtime window icon.
               '--hidden-import', 'PySide6.QtSvg']
    for module in ('matplotlib', 'PIL', 'pytest', 'torch', 'tensorflow', 'jax', 'cupy'):
        command += ['--exclude-module', module]
    for source, target in [('assets', 'assets'), ('coret/qc_algorithm/profile.json', 'coret/qc_algorithm'),
                           ('coret/qc_algorithm/continuous.json', 'coret/qc_algorithm')]:
        command += ['--add-data', str(ROOT / source) + os.pathsep + target]
    subprocess.run(command + [str(ROOT / 'main.py')], cwd=ROOT, env=env, check=True)
    package = ROOT / 'dist' / 'CoreT-Desktop'
    for name in ('README.md', 'README-Windows.md', 'RELEASE-NOTES.md', 'QC-使用说明.txt', 'THIRD-PARTY-NOTICES.txt'):
        shutil.copy2(ROOT / name, package / name)
    shutil.copytree(ROOT / 'licenses', package / 'licenses', dirs_exist_ok=True)
    source = package / 'source'
    source.mkdir(exist_ok=True)
    for name in ('coret', 'assets', 'tools', 'tests'):
        shutil.copytree(ROOT / name, source / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('main.py', 'requirements.txt', 'requirements-build.txt', 'setup-windows.ps1',
                 'setup-windows.bat', 'build-windows.ps1', 'build-windows.bat', 'start-source.bat',
                 'README.md', 'README-Windows.md', 'RELEASE-NOTES.md', 'QC-使用说明.txt', 'THIRD-PARTY-NOTICES.txt'):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / 'licenses', source / 'licenses', dirs_exist_ok=True)
    # Record actual build/runtime versions, not the old Linux prototype versions.
    inventory = subprocess.check_output([sys.executable, '-m', 'pip', 'freeze'], text=True)
    (package / 'build-environment.txt').write_text(
        'Python ' + sys.version + '\nPlatform: ' + platform.platform() + '\n' + inventory,
        encoding='utf-8')
    revision, dirty = source_revision()
    (package / 'release-info.json').write_text(json.dumps(
        dict(application='CoreT Desktop', version=__version__, platform='Windows x64',
             source_commit=revision, source_modified=dirty, code_signed=False),
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    archive = shutil.make_archive(str(ROOT / 'dist' / 'CoreT-Desktop-Windows-x64'), 'zip',
                                  root_dir=ROOT / 'dist', base_dir=package.name)
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    (ROOT / 'dist' / 'SHA256SUMS.txt').write_text(digest + '  ' + Path(archive).name + '\n', encoding='ascii')
    print('Ready:', package / 'CoreT-Desktop.exe')
    print('ZIP:', archive)


if __name__ == '__main__':
    main()
