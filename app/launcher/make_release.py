"""Makes the two downloads in dist/: scrumdash-windows.zip (the committed files plus the official small Python) and scrumdash-mac-linux.zip (the committed files without the Windows parts). Also puts that Python in app/python here.
Usage: python app/launcher/make_release.py   (run after committing; downloads the package once and checks it against the fingerprint below)"""
import hashlib, io, os, subprocess, urllib.request, zipfile

VERSION = '3.14.10'
SHA256 = '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
NAME = f'python-{VERSION}-embed-amd64.zip'
URL = f'https://www.python.org/ftp/python/{VERSION}/{NAME}'
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIST = os.path.join(ROOT, 'dist')
# This Python only looks where its _pth file says. Scrumdash's own programs need app and app/tools added.
PTH = 'python314._pth'
PATHS = 'python314.zip\n.\n..\n..\\tools\n'


def package():
    os.makedirs(DIST, exist_ok=True)
    p = os.path.join(DIST, NAME)
    if not os.path.exists(p):
        urllib.request.urlretrieve(URL, p)
    data = open(p, 'rb').read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        os.remove(p)
        raise SystemExit('The Python package does not match its fingerprint. Nothing was made.')
    return zipfile.ZipFile(io.BytesIO(data))


def main():
    python = package()
    here = os.path.join(ROOT, 'app', 'python')
    os.makedirs(here, exist_ok=True)
    if not os.path.exists(os.path.join(here, 'python.exe')):   # a running board may be using it, so an existing copy stays
        python.extractall(here)
        open(os.path.join(here, PTH), 'w', newline='\r\n').write(PATHS)
    code = subprocess.run(['git', 'archive', '--format=zip', '--prefix=scrumdash/', 'HEAD'], cwd=ROOT, capture_output=True, check=True).stdout
    source = zipfile.ZipFile(io.BytesIO(code))
    files = [(i.filename, i.external_attr, source.read(i)) for i in source.infolist() if not i.is_dir()]
    def add(z, name, attr, data):
        info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
        info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, attr
        z.writestr(info, data)
    out = os.path.join(DIST, 'scrumdash-windows.zip')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for item in files:
            add(z, *item)
        for item in python.infolist():
            if not item.is_dir():
                z.writestr('scrumdash/app/python/' + item.filename, PATHS if item.filename == PTH else python.read(item))
    print(f'{out}: {os.path.getsize(out) / 1e6:.1f} MB')
    out = os.path.join(DIST, 'scrumdash-mac-linux.zip')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for item in files:
            if not item[0].endswith('Scrumdash.exe') and '/app/launcher/' not in item[0]:
                add(z, *item)
    print(f'{out}: {os.path.getsize(out) / 1e6:.1f} MB')


if __name__ == '__main__':
    main()
