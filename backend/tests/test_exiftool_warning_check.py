import json
from types import SimpleNamespace

import pytest

import exif_utils

EXPECTED = '[Minor] Not decoding some large array(s). Ignore minor errors to decode'


@pytest.mark.parametrize('output,allowed', [
    ([{'SourceFile': 'photo.dng', 'Warning': EXPECTED}], True),
    ([{'SourceFile': 'photo.dng', 'Warning': EXPECTED, 'Copy1:Warning': EXPECTED}], True),
    ([{'SourceFile': 'photo.dng', 'Warning': EXPECTED, 'Copy1:Warning': 'Corrupt directory'}], False),
    ([{'SourceFile': 'photo.dng', 'Warning': EXPECTED, 'Error': 'Corrupt directory'}], False),
    ([{'SourceFile': 'photo.dng'}], False),
    ([{'Warning': [EXPECTED]}], False),
    ([{'Warning': EXPECTED}, {'Warning': EXPECTED}], False),
    ({'Warning': EXPECTED}, False),
])
def test_all_diagnostics_must_be_the_exact_allowed_warning(monkeypatch, output, allowed):
    monkeypatch.setattr(exif_utils.shutil, 'which', lambda _: '/usr/bin/exiftool')
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(output).encode(), stderr=b'')
    monkeypatch.setattr(exif_utils.subprocess, 'run', run)
    assert exif_utils._exiftool_has_only_warning('photo.dng', EXPECTED) is allowed
    assert calls[0][0] == ['/usr/bin/exiftool', '-j', '-G4', '-Warning', '-Error', 'photo.dng']
    assert calls[0][1]['timeout'] == exif_utils.EXIFTOOL_TIMEOUT_SECONDS


@pytest.mark.parametrize('failure', ['unavailable', 'timeout', 'exit', 'stderr', 'json'])
def test_warning_check_fails_closed(monkeypatch, failure):
    monkeypatch.setattr(exif_utils.shutil, 'which', lambda _: None if failure == 'unavailable' else '/usr/bin/exiftool')
    def run(*_, **__):
        if failure == 'timeout':
            raise TimeoutError()
        return SimpleNamespace(returncode=1 if failure == 'exit' else 0,
            stdout=b'bad json' if failure == 'json' else json.dumps([{'Warning': EXPECTED}]).encode(),
            stderr=b'Parser warning' if failure == 'stderr' else b'')
    monkeypatch.setattr(exif_utils.subprocess, 'run', run)
    assert not exif_utils._exiftool_has_only_warning('photo.dng', EXPECTED)
