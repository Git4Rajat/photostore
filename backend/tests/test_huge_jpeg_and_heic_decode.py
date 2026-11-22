"""Two real QA failures: a 144 MP JPEG killed the vision worker for memory, and an iPhone 16 HEIC with a
broken auxiliary image looked undecodable."""
import io
import subprocess
import sys
import textwrap

import pytest
from PIL import Image

import image_utils


def test_heic_auxiliary_images_are_not_parsed():
    pillow_heif = pytest.importorskip('pillow_heif')
    assert pillow_heif.options.AUX_IMAGES is False


def test_ordinary_heic_still_converts_to_jpeg():
    pillow_heif = pytest.importorskip('pillow_heif')
    buffer = io.BytesIO()
    pillow_heif.from_pillow(Image.new('RGB', (320, 240), (200, 80, 40))).save(buffer, format='HEIF')
    preview = image_utils.convert_image_to_jpeg(buffer.getvalue(), 'photo.heic')
    assert preview.startswith(b'\xff\xd8')
    with Image.open(io.BytesIO(preview)) as image:
        assert image.size == (320, 240)


MEASURE = textwrap.dedent('''
    import resource, sys
    Image_limit = None
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    sys.path.insert(0, '.')
    import image_utils
    data = open(sys.argv[1], 'rb').read()
    preview = image_utils.convert_image_to_jpeg(data, 'pano.jpg')
    thumb = image_utils.create_thumbnail_data(data)
    import io
    with Image.open(io.BytesIO(preview)) as image:
        print(image.size[0], image.size[1], resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1048576, bool(thumb))
''')


def test_144_megapixel_jpeg_converts_without_a_full_decode(tmp_path):
    source = tmp_path / 'pano.jpg'
    subprocess.run([sys.executable, '-c', textwrap.dedent(f'''
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        Image.new('RGB', (16000, 9000), (90, 120, 200)).save({str(source)!r}, quality=60)
    ''')], check=True)
    out = subprocess.run([sys.executable, '-c', MEASURE, str(source)], check=True, capture_output=True,
                         text=True, cwd=str(__import__('pathlib').Path(__file__).resolve().parents[1])).stdout.split()
    width, height, peak_mb, has_thumb = int(out[0]), int(out[1]), int(out[2]), out[3] == 'True'
    assert max(width, height) <= image_utils.PREVIEW_MAX_DIMENSION and has_thumb
    # A full RGB decode of 16000x9000 is ~430 MB on top of the interpreter; DCT scaling stays far below.
    assert peak_mb < 300, peak_mb
