"""Coverage checks for camera metadata omitted by older range extractors.

This is an additional packet check, not a warning or camera qualification policy.
Only retained bytes are inspected; missing ranges are never filled with zeroes.
"""
from bisect import bisect_right
import struct
from typing import NamedTuple


_SRW = {'NX mini', 'NX3000', 'NX3300'}
_KDC_LEGACY = {'KODAK P712 ZOOM DIGITAL CAMERA', 'KODAK P880 ZOOM DIGITAL CAMERA'}
_KDC = _KDC_LEGACY | {
    'KODAK EasyShare Z981 Digital Camera', 'KODAK EasyShare Z990 Digital Camera',
    'KODAK EASYSHARE Z1015 IS DIGITAL CAMERA',
}
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2,
          9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_MAX_BYTES = 8 * 1024 * 1024


class _Entry(NamedTuple):
    tag: int
    kind: int
    count: int
    offset: int
    data: bytes


class _Ranges:
    def __init__(self, ranges, source_size):
        if type(source_size) is not int or not 8 <= source_size <= 256 * 1024 * 1024:
            raise ValueError('Invalid source size')
        if not isinstance(ranges, (list, tuple)) or not 0 < len(ranges) <= 4096:
            raise ValueError('Invalid ranges')
        self.parts, self.starts = [], []
        end = total = 0
        for offset, data in ranges:
            if type(offset) is not int or not isinstance(data, (bytes, bytearray, memoryview)):
                raise ValueError('Invalid range')
            data = memoryview(data).cast('B')
            total += len(data)
            if offset < end or not data or offset + len(data) > source_size or total > _MAX_BYTES:
                raise ValueError('Invalid range bounds')
            self.starts.append(offset)
            self.parts.append(data)
            end = offset + len(data)
        self.size, self.read_bytes, self.read_calls = source_size, 0, 0
        self.directories = set()

    def read(self, offset, length):
        self.read_bytes += length
        self.read_calls += 1
        if (offset < 0 or length < 0 or offset + length > self.size or length > _MAX_BYTES
                or self.read_bytes > _MAX_BYTES * 8 or self.read_calls > 16384):
            raise ValueError('Metadata bounds or budget exceeded')
        if not length:
            return b''
        index = bisect_right(self.starts, offset) - 1
        parts = []
        while length:
            if index < 0 or index >= len(self.parts):
                raise ValueError('Missing metadata range')
            relative = offset - self.starts[index]
            data = self.parts[index]
            if relative < 0 or relative >= len(data):
                raise ValueError('Metadata gap')
            count = min(length, len(data) - relative)
            parts.append(data[relative:relative + count])
            offset, length, index = offset + count, length - count, index + 1
        return b''.join(parts)

    def ifd(self, offset, order, base=0, next_link=True):
        if not offset or offset in self.directories or len(self.directories) >= 256:
            raise ValueError('Invalid or cyclic IFD')
        self.directories.add(offset)
        count = struct.unpack(order + 'H', self.read(offset, 2))[0]
        if count > 4096:
            raise ValueError('IFD entry limit')
        table = self.read(offset + 2, count * 12 + (4 if next_link else 0))
        entries = []
        for index in range(count):
            pos = index * 12
            tag, kind, number, pointer = struct.unpack_from(order + 'HHII', table, pos)
            if not (tag or kind or number or pointer):
                continue
            if kind not in _SIZES:
                raise ValueError('Unsupported TIFF type')
            length = _SIZES[kind] * number
            value_offset = base + pointer if length > 4 else offset + 2 + pos + 8
            value = self.read(value_offset, length)
            entries.append(_Entry(tag, kind, number, value_offset, value))
        following = struct.unpack_from(order + 'I', table, count * 12)[0] if next_link else 0
        return entries, following

    def chain(self, offset, order, base=0):
        for _ in range(24):
            entries, following = self.ifd(offset, order, base)
            yield entries
            if not following:
                return
            offset = base + following
        raise ValueError('IFD chain limit')

    def jpeg(self, offset, length):
        end = offset + length
        if length < 4 or end > self.size or self.read(offset, 2) != b'\xff\xd8':
            raise ValueError('Invalid preview JPEG extent')
        pos = offset + 2
        for _ in range(1024):
            if pos + 4 > end:
                break
            header = self.read(pos, 4)
            if header[0] != 255 or header[1] in {0, 1, *range(0xd0, 0xda), 255}:
                break
            size = int.from_bytes(header[2:], 'big')
            if size < 2 or pos + size + 2 > end:
                break
            self.read(pos + 4, size - 2)
            if header[1] == 0xda:
                return  # SOS ends the required metadata; never inspect scan pixels.
            pos += size + 2
        raise ValueError('Incomplete preview JPEG headers')


def _entry(entries, tag, kind, count=None):
    matches = [entry for entry in entries if entry.tag == tag]
    if len(matches) != 1 or matches[0].kind != kind or (count is not None and matches[0].count != count):
        raise ValueError('Missing or invalid camera metadata entry')
    return matches[0]


def _pointer(entries, tag, order):
    value = int.from_bytes(_entry(entries, tag, 4, 1).data, 'little' if order == '<' else 'big')
    if not value:
        raise ValueError('Null camera metadata pointer')
    return value


def _identity(entries, tag):
    return _entry(entries, tag, 2).data.split(b'\0', 1)[0].decode('ascii').strip()


def verified_camera_ranges(filename, exif, ranges, source_size):
    """Require the repaired SRW/KDC metadata subtrees in decoded packet ranges.

    Other camera profiles return True without inspecting ranges. The filename is
    intentionally not used to exempt renamed files; normal family/warning gates
    remain the caller's responsibility. No source hashes or sample names qualify
    coverage here.
    """
    make, model, kind = ((exif or {}).get(key) for key in ('Make', 'Model', 'FileType'))
    samsung = make == 'SAMSUNG' and kind == 'SRW' and isinstance(model, str) and model in _SRW
    kodak = make == 'EASTMAN KODAK COMPANY' and kind == 'KDC' and isinstance(model, str) and model in _KDC
    if not samsung and not kodak:
        return True
    try:
        reader = _Ranges(ranges, source_size)
        header = reader.read(0, 8)
        order = {b'II': '<', b'MM': '>'}.get(header[:2])
        if not order or struct.unpack_from(order + 'H', header, 2)[0] != 42:
            return False
        root, _ = reader.ifd(struct.unpack_from(order + 'I', header, 4)[0], order)
        if (_identity(root, 0x010f), _identity(root, 0x0110)) != (make, model):
            return False
        if samsung or model in _KDC_LEGACY:
            exif_ifd, _ = reader.ifd(_pointer(root, 0x8769, order), order)
            note = _entry(exif_ifd, 0x927c, 7)
            if note.count <= 4:
                return False
            if samsung:
                maker, _ = reader.ifd(note.offset, '<', note.offset, next_link=False)
                if _entry(maker, 1, 7, 4).data != b'0100':
                    return False
                relative = _pointer(maker, 0x0035, '<')
                if relative != note.count:
                    return False  # only the verified external, MakerNote-relative layout
                for preview in reader.chain(note.offset + relative, '<', note.offset):
                    _entry(preview, 0x011a, 5, 1)
                    _entry(preview, 0x011b, 5, 1)
                    reader.jpeg(note.offset + _pointer(preview, 0x0201, '<'),
                                _pointer(preview, 0x0202, '<'))
                return True
            maker, _ = reader.ifd(note.offset, order, next_link=False)
            camera, _ = reader.ifd(_pointer(maker, 0xff00, order), order, next_link=False)
            _entry(camera, 0xf900, 3, 1)
            _entry(camera, 0xf901, 3, 1)
            _entry(camera, 0xfa04, 2, 32)
        for directory in reader.chain(_pointer(root, 0xfe00, order), order):
            _entry(directory, 0xfa00, 2, 32)
            _entry(directory, 0xfa0d, 1, 1)
            for tag in (0xfa25, 0xfa27, 0xfa28, 0xfa29, 0xfa2a):
                _entry(directory, tag, 4, 3)
        return True
    except (ValueError, TypeError, IndexError, struct.error):
        return False
