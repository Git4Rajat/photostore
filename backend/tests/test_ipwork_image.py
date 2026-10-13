import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from PIL import Image, ImageOps

import ipwork_image


def jpeg(orientation=6):
    image = Image.new('RGB', (24, 12), 'red')
    image.putpixel((0, 0), (0, 255, 0))
    stream = io.BytesIO()
    exif = Image.Exif()
    exif[274] = orientation
    image.save(stream, format='JPEG', exif=exif)
    return stream.getvalue()


def test_single_decode_matches_oriented_pixels_and_closes(monkeypatch):
    data = jpeg()
    with Image.open(io.BytesIO(data)) as source:
        expected = ImageOps.exif_transpose(source).convert('RGB')
    calls = []
    prepare = lambda source, filename: calls.append(filename) or source
    with ipwork_image.image_job(True):
        with ipwork_image.rgb_image(data, 'a.jpg', prepare) as first:
            assert first.size == (12, 24)
            assert first.tobytes() == expected.tobytes()
        with ipwork_image.rgb_image(data, 'a.jpg', prepare) as second:
            assert first is second
        assert calls == ['a.jpg']
    with pytest.raises(ValueError):
        first.tobytes()
    assert not ipwork_image.sharing_enabled()


def test_source_change_and_exception_release_images():
    with pytest.raises(RuntimeError):
        with ipwork_image.image_job(True):
            with ipwork_image.rgb_image(jpeg(), 'a.jpg') as first:
                pass
            with ipwork_image.rgb_image(jpeg(1), 'b.jpg') as second:
                assert second.size == (24, 12)
            with pytest.raises(ValueError):
                first.tobytes()
            raise RuntimeError('processor failed')
    with pytest.raises(ValueError):
        second.tobytes()
    assert not ipwork_image.sharing_enabled()


def test_disabled_and_nested_context_do_not_share():
    data = jpeg()
    with ipwork_image.image_job(True):
        with ipwork_image.rgb_image(data) as outer:
            pass
        with ipwork_image.image_job(False):
            with ipwork_image.rgb_image(data) as inner:
                assert inner is not outer
            with pytest.raises(ValueError):
                inner.tobytes()
        with ipwork_image.rgb_image(data) as restored:
            assert restored is outer


def test_concurrent_jobs_have_separate_images():
    data = jpeg()
    barrier = Barrier(2)
    def run(_):
        with ipwork_image.image_job(True):
            with ipwork_image.rgb_image(data) as image:
                barrier.wait(timeout=5)
                return image
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(run, range(2))
    assert first is not second


@pytest.mark.parametrize('module_name', ['ipwork_ocr', 'ipwork_face', 'ipwork_vision'])
def test_jpeg_preview_under_raw_filename_is_not_extracted_again(monkeypatch, module_name):
    import importlib
    module = importlib.import_module(module_name)
    def extract(*_):
        raise AssertionError('JPEG pixels must not enter RAW extraction')
    monkeypatch.setattr(module, 'extract_raw_preview_bytes', extract)
    data = jpeg()
    assert module._decodable_image_bytes(data, 'photo.CR3') is data
