"""Generate a benchmark-only image patch from a pre-edit snapshot."""
import difflib
from pathlib import Path
import sys


def main():
    before = Path(sys.argv[1])
    root = Path(__file__).resolve().parents[2]
    patch = []
    for source, destination in [('backend/app.py', 'app.py'),
                                ('backend/storage_utils.py', 'storage_utils.py'),
                                ('backend/routes/upload.py', 'routes/upload.py')]:
        old = (before / Path(source).name).read_text().splitlines(keepends=True)
        new = (root / source).read_text().splitlines(keepends=True)
        patch.extend(difflib.unified_diff(old, new, fromfile=f'a/{destination}', tofile=f'b/{destination}', n=3))
    if not patch:
        raise RuntimeError('No overlay changes found')
    (root / 'deploy/benchmark/overlay.patch').write_text(''.join(patch))


if __name__ == '__main__':
    main()
