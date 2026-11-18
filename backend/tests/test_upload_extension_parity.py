"""The browser drops files the server would reject before registering them
(frontend/src/utils/photoDisplay.ts UPLOADABLE_EXTENSIONS), so that list must
match ALLOWED_EXTENSIONS exactly: an extra entry lets a file through to fail
on every retry, a missing one silently drops media the server accepts."""
from __future__ import annotations

import re
from pathlib import Path

from image_utils import ALLOWED_EXTENSIONS, allowed_file

PHOTO_DISPLAY = Path(__file__).resolve().parents[2] / 'frontend' / 'src' / 'utils' / 'photoDisplay.ts'


def _frontend_uploadable_extensions() -> set[str]:
    source = PHOTO_DISPLAY.read_text(encoding='utf-8')
    match = re.search(r'export const UPLOADABLE_EXTENSIONS = new Set<string>\(\[(.*?)\]\);', source, re.S)
    assert match, 'UPLOADABLE_EXTENSIONS literal not found in photoDisplay.ts'
    body = re.sub(r'//[^\n]*', '', match.group(1))
    return set(re.findall(r"'([^']+)'", body))


def test_frontend_uploadable_extensions_match_backend_allowlist():
    frontend = _frontend_uploadable_extensions()
    assert frontend - ALLOWED_EXTENSIONS == set(), 'browser would send files the server rejects'
    assert ALLOWED_EXTENSIONS - frontend == set(), 'browser would drop files the server accepts'


def test_apple_photos_edit_sidecars_are_not_uploadable():
    assert not allowed_file('IMG_O9346.aae')
    assert 'aae' not in _frontend_uploadable_extensions()


def test_init_marks_unsupported_types_as_permanent():
    from routes.upload import _invalid_filename_error

    assert _invalid_filename_error('IMG_O9346.aae') == {'error': 'Unsupported file type', 'code': 'unsupported_file_type'}
    # Unsafe names stay a plain, retryable-looking 'Invalid filename'.
    assert _invalid_filename_error('../IMG_0001.jpg') == {'error': 'Invalid filename'}
    assert _invalid_filename_error('') == {'error': 'Invalid filename'}
