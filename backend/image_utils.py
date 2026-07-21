import hashlib
import io
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from PIL import Image, ImageDraw, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

RESAMPLING_LANCZOS = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS

RAW_EXTENSIONS_RAWPY = {
    'cr2', 'cr3', 'crw', 'nef', 'nrw', 'arw', 'srf', 'sr2',
    'dng', 'orf', 'rw2', 'pef', 'ptx', 'raf', 'raw',
    'rwl', '3fr', 'fff', 'mrw', 'x3f', 'erf', 'mef',
    'mos', 'kdc', 'k25', 'dcr', 'dcs', 'drf', 'mdc',
    'srw', 'rwz', 'bay', 'cap', 'eip', 'gpr', 'pxn', 'iiq',
}

RAW_EXTENSIONS_CINEMA = {
    'ari',
    'braw',
    'r3d',
}

PILLOW_NATIVE = {
    'tif', 'tiff', 'jpg', 'jpeg', 'png',
    'webp', 'heic', 'heif', 'bmp', 'gif',
}

VIDEO_EXTENSIONS = {
    'mp4', 'mov', 'm4v', 'webm', 'avi', 'mkv',
    '3gp', '3g2', 'mts', 'm2ts', 'mpg', 'mpeg', 'wmv',
}

ALLOWED_EXTENSIONS = PILLOW_NATIVE | RAW_EXTENSIONS_RAWPY | RAW_EXTENSIONS_CINEMA | VIDEO_EXTENSIONS


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


THUMBNAIL_SIZE = (120, 120)
THUMBNAIL_QUALITY = 65
THUMBNAIL_FORMAT = 'JPEG'
VISION_MAX_BYTES = 3_900_000
VISION_MAX_DIMENSION = 2048
MIN_SIZE_CINEMA = 1 * 1024 * 1024
MIN_SIZE_RAW = 512 * 1024
MIN_SIZE_VIDEO = 1024
MIN_SIZE_DEFAULT = 1024
VIDEO_THUMBNAIL_TIMEOUT_SECONDS = _env_int('VIDEO_THUMBNAIL_TIMEOUT_SECONDS', 30)
RAW_PREVIEW_SCAN_CHUNK_BYTES = 1024 * 1024
RAW_PREVIEW_SCAN_MAX_BYTES = _env_int('RAW_PREVIEW_SCAN_MAX_BYTES', 512 * 1024 * 1024)
RAW_PREVIEW_MAX_EMBEDDED_JPEG_BYTES = _env_int('RAW_PREVIEW_MAX_EMBEDDED_JPEG_BYTES', 128 * 1024 * 1024)
RAW_PREVIEW_TIMEOUT_SECONDS = _env_int('RAW_PREVIEW_TIMEOUT_SECONDS', 20)
RAW_PREVIEW_EXIFTOOL_TAGS = (
    'PreviewImage',
    'JpgFromRaw',
    'OtherImage',
    'ThumbnailImage',
    'PreviewTIFF',
    'ThumbnailTIFF',
    'FullSizePreview',
    'RawThermalImage',
)


def compute_file_hash(content: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(content)
    return digest.hexdigest()


def compute_file_hash_from_path(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as file_handle:
        for chunk in iter(lambda: file_handle.read(8192), b''):
            digest.update(chunk)
    return digest.hexdigest()


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def is_video_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in VIDEO_EXTENSIONS


def _check_raw_header(content: bytes) -> Optional[Exception]:
    if not content[:4096].strip(b'\x00'):
        return ValueError('RAW file header is empty or zero-filled')
    return None


def verify_image(content: bytes, filename: str) -> Optional[Exception]:
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    if ext in VIDEO_EXTENSIONS:
        if len(content) < MIN_SIZE_VIDEO:
            return ValueError(f'Video file too small ({len(content)} bytes) - likely corrupted')
        return _check_raw_header(content)

    if ext in PILLOW_NATIVE or ext not in (RAW_EXTENSIONS_RAWPY | RAW_EXTENSIONS_CINEMA):
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            return None
        except Exception as exc:
            return exc

    if ext in RAW_EXTENSIONS_CINEMA:
        if len(content) < MIN_SIZE_CINEMA:
            return ValueError(f'Cinema RAW file too small ({len(content)} bytes) - likely corrupted')
        header_error = _check_raw_header(content)
        if header_error is not None:
            return header_error
        return None

    if ext in RAW_EXTENSIONS_RAWPY:
        if len(content) < MIN_SIZE_RAW:
            return ValueError(f'RAW file too small ({len(content)} bytes) - likely corrupted')
        header_error = _check_raw_header(content)
        if header_error is not None:
            return header_error
        return None

    return None


def _save_image_to_bytes(image: Image.Image, fmt: str) -> bytes:
    output = io.BytesIO()
    image.save(output, format=fmt, quality=THUMBNAIL_QUALITY, optimize=True)
    output.seek(0)
    return output.read()


def _encode_vision_jpeg(image: Image.Image) -> bytes:
    if max(image.size) > VISION_MAX_DIMENSION:
        image.thumbnail((VISION_MAX_DIMENSION, VISION_MAX_DIMENSION), RESAMPLING_LANCZOS)

    if image.mode != 'RGB':
        image = image.convert('RGB')

    for quality in (90, 82, 74, 66, 58):
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=quality, optimize=True)
        data = output.getvalue()
        if len(data) <= VISION_MAX_BYTES:
            return data

    image.thumbnail((1400, 1400), RESAMPLING_LANCZOS)
    output = io.BytesIO()
    image.save(output, format='JPEG', quality=58, optimize=True)
    return output.getvalue()


def _normalize_preview_bytes(image_bytes: bytes) -> Optional[bytes]:
    if not image_bytes:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.verify()
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            if image.format == 'JPEG':
                return image_bytes
            return _encode_vision_jpeg(image)
    except Exception:
        return None


def _encode_preview_for_browser(image_bytes: bytes) -> Optional[bytes]:
    if not image_bytes:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode != 'RGB':
                image = image.convert('RGB')
            return _encode_vision_jpeg(image)
    except Exception:
        return None


RAW_PREVIEW_MIN_VISION_EDGE = 512


def _preview_score(image_bytes: Optional[bytes]) -> tuple[int, int]:
    if not image_bytes:
        return (0, 0)
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            width, height = image.size
        return (max(width, height), width * height)
    except Exception:
        return (0, 0)


def _preview_good_enough_for_vision(image_bytes: Optional[bytes]) -> bool:
    return _preview_score(image_bytes)[0] >= RAW_PREVIEW_MIN_VISION_EDGE


def _best_preview(candidates) -> Optional[bytes]:
    best: Optional[bytes] = None
    best_score = (0, 0)
    for candidate in candidates:
        if not candidate:
            continue
        score = _preview_score(candidate)
        if score > best_score:
            best = candidate
            best_score = score
    return best


def _run_preview_command(args: list[str]) -> Optional[bytes]:
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=RAW_PREVIEW_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return None
    return _normalize_preview_bytes(result.stdout)


def _extract_exiftool_preview_from_path(path: str) -> Optional[bytes]:
    exiftool = shutil.which('exiftool')
    if not exiftool:
        return None
    candidates = []
    for tag in RAW_PREVIEW_EXIFTOOL_TAGS:
        preview = _run_preview_command([exiftool, '-b', f'-{tag}', path])
        if _preview_good_enough_for_vision(preview):
            return preview
        if preview:
            candidates.append(preview)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_pattern = os.path.join(temp_dir, 'preview_%t%-c.%s')
            subprocess.run(
                [exiftool, '-q', '-q', '-a', '-b', '-preview:all', '-W', output_pattern, path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=RAW_PREVIEW_TIMEOUT_SECONDS,
                check=False,
            )
            file_candidates = []
            for name in os.listdir(temp_dir):
                candidate_path = os.path.join(temp_dir, name)
                try:
                    size = os.path.getsize(candidate_path)
                except OSError:
                    continue
                if 0 < size <= RAW_PREVIEW_MAX_EMBEDDED_JPEG_BYTES:
                    file_candidates.append((size, candidate_path))
            for _, candidate_path in sorted(file_candidates, reverse=True):
                try:
                    with open(candidate_path, 'rb') as candidate_file:
                        preview = _normalize_preview_bytes(candidate_file.read())
                except Exception:
                    preview = None
                if _preview_good_enough_for_vision(preview):
                    return preview
                if preview:
                    candidates.append(preview)
    except Exception:
        pass
    return _best_preview(candidates)


def _extract_ffmpeg_preview_from_path(path: str) -> Optional[bytes]:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return None
    return _run_preview_command([
        ffmpeg,
        '-nostdin',
        '-hide_banner',
        '-loglevel',
        'error',
        '-i',
        path,
        '-frames:v',
        '1',
        '-an',
        '-f',
        'image2pipe',
        '-vcodec',
        'mjpeg',
        'pipe:1',
    ])


def _extract_libraw_preview_from_path(path: str) -> Optional[bytes]:
    decoder = shutil.which('dcraw_emu') or shutil.which('dcraw')
    if not decoder:
        return None
    candidates = []
    for args in (
        [decoder, '-c', '-e', path],
        [decoder, '-c', '-w', '-h', path],
        [decoder, '-c', '-w', '-q', '0', '-H', '1', path],
    ):
        preview = _run_preview_command(args)
        if _preview_good_enough_for_vision(preview):
            return preview
        if preview:
            candidates.append(preview)
    return _best_preview(candidates)


def _extract_rawpy_preview_from_path(path: str) -> Optional[bytes]:
    try:
        import rawpy
        with rawpy.imread(path) as raw:
            preview = _extract_rawpy_thumbnail(raw, rawpy)
            if _preview_good_enough_for_vision(preview):
                return preview
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8)
        return _encode_vision_jpeg(Image.fromarray(rgb))
    except Exception:
        return None


def _extract_rawpy_preview_from_bytes(image_bytes: bytes) -> Optional[bytes]:
    try:
        import rawpy
        with rawpy.imread(io.BytesIO(image_bytes)) as raw:
            preview = _extract_rawpy_thumbnail(raw, rawpy)
            if _preview_good_enough_for_vision(preview):
                return preview
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8)
        return _encode_vision_jpeg(Image.fromarray(rgb))
    except Exception:
        return None


def _extract_rawpy_thumbnail(raw, rawpy_module) -> Optional[bytes]:
    try:
        thumbnail = raw.extract_thumb()
    except Exception:
        return None
    try:
        if thumbnail.format == rawpy_module.ThumbFormat.JPEG:
            return _normalize_preview_bytes(thumbnail.data)
        if thumbnail.format == rawpy_module.ThumbFormat.BITMAP:
            return _encode_vision_jpeg(Image.fromarray(thumbnail.data))
    except Exception:
        return None
    return None


def _temporary_preview_from_bytes(image_bytes: bytes, filename: str) -> Optional[bytes]:
    ext = filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else 'raw'
    suffix = f'.{ext}' if ext else '.raw'
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix) as temp_file:
            temp_file.write(image_bytes)
            temp_file.flush()
            return extract_raw_preview_from_path(temp_file.name)
    except Exception:
        return None


def create_placeholder_thumbnail() -> bytes:
    image = Image.new('RGB', THUMBNAIL_SIZE, (232, 236, 242))
    draw = ImageDraw.Draw(image)
    # Make placeholder visually obvious so users can distinguish fallback tiles.
    draw.rectangle((0, 0, THUMBNAIL_SIZE[0] - 1, THUMBNAIL_SIZE[1] - 1), outline=(110, 120, 140), width=2)
    draw.line((0, 0, THUMBNAIL_SIZE[0] - 1, THUMBNAIL_SIZE[1] - 1), fill=(150, 160, 180), width=2)
    draw.line((THUMBNAIL_SIZE[0] - 1, 0, 0, THUMBNAIL_SIZE[1] - 1), fill=(150, 160, 180), width=2)
    draw.rectangle((34, 44, 86, 76), outline=(110, 120, 140), width=2)
    return _save_image_to_bytes(image, THUMBNAIL_FORMAT)


def create_thumbnail_data(source_bytes: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(source_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(THUMBNAIL_SIZE, RESAMPLING_LANCZOS)
            if image.mode != 'RGB':
                image = image.convert('RGB')
            return _save_image_to_bytes(image, THUMBNAIL_FORMAT)
    except Exception:
        converted = convert_image_to_jpeg(source_bytes)
        if converted != source_bytes:
            try:
                with Image.open(io.BytesIO(converted)) as image:
                    image.thumbnail(THUMBNAIL_SIZE, RESAMPLING_LANCZOS)
                    if image.mode != 'RGB':
                        image = image.convert('RGB')
                    return _save_image_to_bytes(image, THUMBNAIL_FORMAT)
            except Exception:
                pass
        return create_placeholder_thumbnail()


def extract_video_frame_jpeg(video_bytes: bytes, filename: str = '') -> Optional[bytes]:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not video_bytes:
        return None
    ext = filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else 'mp4'
    with tempfile.TemporaryDirectory() as temp_dir:
        source_path = os.path.join(temp_dir, f'source.{ext}')
        frame_path = os.path.join(temp_dir, 'frame.jpg')
        with open(source_path, 'wb') as source_file:
            source_file.write(video_bytes)
        for seek in ('1', '0'):
            try:
                result = subprocess.run(
                    [ffmpeg, '-y', '-ss', seek, '-i', source_path, '-frames:v', '1', '-q:v', '3', frame_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=VIDEO_THUMBNAIL_TIMEOUT_SECONDS,
                    check=False,
                )
            except Exception:
                return None
            if result.returncode == 0 and os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
                with open(frame_path, 'rb') as frame_file:
                    return frame_file.read()
    return None


def create_video_thumbnail_data(video_bytes: bytes, filename: str = '') -> Optional[bytes]:
    frame_bytes = extract_video_frame_jpeg(video_bytes, filename)
    if not frame_bytes:
        return None
    return create_thumbnail_data(frame_bytes)


def create_thumbnail_data_from_path(path: str) -> bytes:
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(THUMBNAIL_SIZE, RESAMPLING_LANCZOS)
            if image.mode != 'RGB':
                image = image.convert('RGB')
            return _save_image_to_bytes(image, THUMBNAIL_FORMAT)
    except Exception:
        preview = extract_raw_preview_from_path(path)
        if preview:
            try:
                with Image.open(io.BytesIO(preview)) as image:
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail(THUMBNAIL_SIZE, RESAMPLING_LANCZOS)
                    if image.mode != 'RGB':
                        image = image.convert('RGB')
                    return _save_image_to_bytes(image, THUMBNAIL_FORMAT)
            except Exception:
                pass
        return create_placeholder_thumbnail()


def extract_embedded_jpeg(raw_bytes: bytes) -> Optional[bytes]:
    candidates = []
    start = 0
    while True:
        soi = raw_bytes.find(b'\xff\xd8', start)
        if soi == -1:
            break
        eoi = raw_bytes.find(b'\xff\xd9', soi + 2)
        if eoi == -1:
            break
        segment = raw_bytes[soi:eoi + 2]
        if len(segment) > 2:
            candidates.append(segment)
        start = eoi + 2

    if not candidates:
        return None

    best = max(candidates, key=len)
    return _normalize_preview_bytes(best)


def extract_embedded_jpeg_from_path(path: str) -> Optional[bytes]:
    best: Optional[bytes] = None
    pending = b''
    scanned = 0
    try:
        with open(path, 'rb') as file_handle:
            while scanned < RAW_PREVIEW_SCAN_MAX_BYTES:
                chunk = file_handle.read(min(RAW_PREVIEW_SCAN_CHUNK_BYTES, RAW_PREVIEW_SCAN_MAX_BYTES - scanned))
                if not chunk:
                    break
                scanned += len(chunk)
                data = pending + chunk
                next_pending = data[-1:]
                start = 0
                while True:
                    soi = data.find(b'\xff\xd8', start)
                    if soi == -1:
                        break
                    eoi = data.find(b'\xff\xd9', soi + 2)
                    if eoi == -1:
                        next_pending = data[soi:]
                        if len(next_pending) > RAW_PREVIEW_MAX_EMBEDDED_JPEG_BYTES:
                            next_pending = b''
                        break
                    segment = data[soi:eoi + 2]
                    if len(segment) > 16_384 and (best is None or len(segment) > len(best)):
                        normalized = _normalize_preview_bytes(segment)
                        if normalized:
                            best = normalized
                    start = eoi + 2
                pending = next_pending
        return best
    except Exception:
        return None


def extract_raw_preview_from_path(path: str) -> Optional[bytes]:
    candidates = []
    for extractor in (
        _extract_exiftool_preview_from_path,
        extract_embedded_jpeg_from_path,
        _extract_rawpy_preview_from_path,
        _extract_libraw_preview_from_path,
        _extract_ffmpeg_preview_from_path,
    ):
        preview = extractor(path)
        if _preview_good_enough_for_vision(preview):
            return preview
        if preview:
            candidates.append(preview)
    return _best_preview(candidates)


def extract_raw_preview_bytes(image_bytes: bytes, filename: str = '') -> Optional[bytes]:
    candidates = []
    preview = extract_embedded_jpeg(image_bytes)
    if _preview_good_enough_for_vision(preview):
        return preview
    if preview:
        candidates.append(preview)
    preview = _extract_rawpy_preview_from_bytes(image_bytes)
    if _preview_good_enough_for_vision(preview):
        return preview
    if preview:
        candidates.append(preview)
    if filename:
        preview = _temporary_preview_from_bytes(image_bytes, filename)
        if _preview_good_enough_for_vision(preview):
            return preview
        if preview:
            candidates.append(preview)
    return _best_preview(candidates)


def convert_image_to_jpeg(image_bytes: bytes, filename: str = '') -> bytes:
    ext = filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else ''
    if ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA:
        preview = extract_raw_preview_bytes(image_bytes, filename)
        if preview:
            return _encode_preview_for_browser(preview) or preview
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            return _encode_vision_jpeg(image)
    except Exception:
        preview = extract_raw_preview_bytes(image_bytes, filename)
        if preview:
            return _encode_preview_for_browser(preview) or preview
        return image_bytes
