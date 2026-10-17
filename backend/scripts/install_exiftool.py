"""Install the checksum-pinned EXIF runtime shared by both container images."""
import argparse
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
from urllib.request import urlopen

VERSION = '13.55'
SHA256 = '5c9d422ad128fab728aacc5cc0aa77095d14cde74931389dd961d3d720e6b316'
URL = f'https://github.com/exiftool/exiftool/archive/refs/tags/{VERSION}.tar.gz'


def install(prefix, bin_dir, archive=None):
    if archive:
        data = Path(archive).read_bytes()
    else:
        with urlopen(URL, timeout=60) as response:
            data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError('ExifTool archive checksum mismatch')
    root = f'exiftool-{VERSION}/'
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as source:
        for member in source:
            if member.name.rstrip('/') == root.rstrip('/') and member.isdir():
                continue
            if not member.name.startswith(root):
                raise ValueError('Unexpected ExifTool archive root')
            relative = Path(member.name[len(root):])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Invalid ExifTool archive path')
            if relative.as_posix() not in {'exiftool', 'LICENSE'} and (not relative.parts or relative.parts[0] != 'lib'):
                continue
            if not member.isfile() and not member.isdir():
                raise ValueError('Invalid ExifTool archive member')
            target = prefix / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as handle:
                    target.write_bytes(handle.read())
                target.chmod(0o755 if relative.as_posix() == 'exiftool' else 0o644)
    executable = prefix / 'exiftool'
    if subprocess.check_output([str(executable), '-ver'], text=True).strip() != VERSION:
        raise ValueError('Unexpected ExifTool version')
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / 'exiftool'
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != executable.resolve():
            raise ValueError('ExifTool executable already exists')
    else:
        link.symlink_to(executable.resolve())
    if subprocess.check_output([str(link), '-ver'], text=True).strip() != VERSION:
        raise ValueError('Unexpected installed ExifTool version')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path('/opt/photostore-exiftool'))
    parser.add_argument('--bin-dir', type=Path, default=Path('/usr/local/bin'))
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    install(args.prefix, args.bin_dir, args.archive)
