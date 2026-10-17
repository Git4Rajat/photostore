import io

import pytest
from PIL import Image
import pillow_heif

import image_utils
import ipwork_thumbnail
import storage_utils


@pytest.mark.parametrize('extension', ['hif', 'HIF', 'heic', 'heif'])
def test_heif_family_uses_upload_and_preview_paths(extension):
    assert image_utils.allowed_file('photo.' + extension)
    assert extension.lower() in image_utils.BROWSER_UNVIEWABLE_EXTENSIONS
    assert extension.lower() in ipwork_thumbnail.HEIF_EXTENSIONS
    assert extension.lower() in storage_utils.HEIF_EXTENSIONS


def test_real_hif_verifies_and_generates_preview_and_thumbnail():
    source = pillow_heif.from_pillow(Image.new('RGB', (80, 60), (10, 120, 200)))
    encoded = io.BytesIO()
    source.save(encoded)
    data = encoded.getvalue()
    assert image_utils.verify_image(data, 'photo.HIF') is None
    preview = image_utils.convert_image_to_jpeg(data, 'photo.HIF')
    thumbnail = ipwork_thumbnail._create_thumbnail_bytes(data, 'photo.HIF')
    for encoded in (preview, thumbnail):
        with Image.open(io.BytesIO(encoded)) as image:
            image.load()
            assert image.format == 'JPEG'
            assert image.size == (80, 60)
    assert image_utils.verify_image(b'not an image', 'photo.HIF') is not None
