import importlib.util
from pathlib import Path

import pytest


def load_installer():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'install_exiftool.py'
    spec = importlib.util.spec_from_file_location('install_exiftool', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_checksum_failure_writes_nothing(tmp_path):
    installer = load_installer()
    archive = tmp_path / 'bad.tar.gz'
    archive.write_bytes(b'not the approved runtime')
    prefix, binary = tmp_path / 'runtime', tmp_path / 'bin'
    with pytest.raises(ValueError, match='checksum'):
        installer.install(prefix, binary, archive)
    assert not prefix.exists() and not binary.exists()


def test_both_images_use_the_shared_pinned_runtime():
    backend = Path(__file__).resolve().parents[1]
    for name in ('Dockerfile', 'ipworker.Dockerfile'):
        source = (backend / name).read_text()
        assert 'scripts/install_exiftool.py /tmp/install_exiftool.py' in source
        assert 'python /tmp/install_exiftool.py' in source
        assert 'libimage-exiftool-perl' not in source
    installer = load_installer()
    assert installer.VERSION == '13.55'
    assert len(installer.SHA256) == 64
