import pytest

import exif_utils
import upload_metadata as transport
from tests.test_upload_metadata import _candidate_fixture


PROFILES = [
    ('srw', 'SAMSUNG', 'EX1', 'SRW', '[minor] Overlapping MakerNotes values'),
    ('srw', 'SAMSUNG', 'NX mini', 'SRW', '[minor] Overlapping MakerNotes values'),
    ('x3f', 'SIGMA', 'SIGMA DP2 Merrill', 'X3F', 'Bad PrintIM size'),
    ('x3f', 'SIGMA', 'SIGMA SD15', 'X3F', 'Invalid EXIF text encoding for UserComment'),
    ('dng', 'Arashi Vision', 'Insta360 One2', 'DNG', 'Invalid EXIF text encoding for UserComment'),
    ('fff', 'Hasselblad', 'Hasselblad CFV-50c', 'FFF', 'Bad IPTC data tag (marker 0x0) [x2]'),
    ('fff', 'Hasselblad', 'Hasselblad H5D-40', 'FFF', '[minor] Error reading value for IFD0 entry 26, ID 0xc519 HasselbladXML'),
    ('fff', 'Hasselblad', 'Hasselblad H6D-100cMS', 'FFF', '[minor] Error reading value for IFD0 entry 27, ID 0xc519 HasselbladXML'),
    ('fff', 'Hasselblad', 'Hasselblad H4D-40', 'FFF', '[minor] Suspicious MakerNotes offset for tag 0x001a'),
    ('iiq', 'Phase One A/S', 'H 25', 'IIQ', 'ICC_Profile pointer references previous ICC_Profile directory'),
    ('iiq', 'Phase One', 'iXM-RS150F', 'IIQ', '[minor] Unrecognized PhaseOne format size 8 [x3]'),
    ('tif', 'Canon', 'EOS D2000C', 'TIFF', '[minor] Empty IPTC-NAA data'),
    ('dcr', 'Kodak', 'DCS Pro 14N', 'DCR', '[Minor] Ignoring KodakIFD DiffTileGains24t852822 with excessive count'),
    ('dcr', 'Kodak', 'DCS Pro SLR/n', 'DCR', '[minor] Empty IPTC-NAA data'),
    ('kdc', 'EASTMAN KODAK COMPANY', 'KODAK EasyShare Z981 Digital Camera', 'KDC', 'Suspicious SubIFD offset for tag 0xfd04'),
    ('kdc', 'EASTMAN KODAK COMPANY', 'KODAK P712 ZOOM DIGITAL CAMERA', 'KDC', 'Suspicious SubIFD offset for tag 0xfd04'),
]


def metadata(profile):
    _, make, model, kind, warning = profile
    return {'ExifToolVersion': '13.55', 'Make': make, 'Model': model, 'FileType': kind,
            'Warning': warning, 'DNGVersion': '1 1 0 0'}


@pytest.mark.parametrize('profile', PROFILES)
def test_verified_source_warning_preserves_metadata_and_checks_all_diagnostics(monkeypatch, profile):
    import metadata_tiff_coverage
    monkeypatch.setattr(metadata_tiff_coverage, 'verified_camera_ranges', lambda *_: True)
    exif = metadata(profile)
    storage, row = _candidate_fixture()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: exif.copy())
    calls = []
    monkeypatch.setattr(exif_utils, '_exiftool_has_only_warnings', lambda path, warnings: calls.append(warnings) or True)
    outcomes = []
    assert transport.extract_candidate(storage, 'photo.' + profile[0], row, on_outcome=outcomes.append) == exif
    assert outcomes == ['reused']
    assert calls == [transport._verified_source_warning_profile('photo.' + profile[0], exif)]
    assert len(calls[0]) == (2 if profile[2] in {'Hasselblad H4D-40', 'DCS Pro 14N', 'KODAK P712 ZOOM DIGITAL CAMERA'} else 1)
    monkeypatch.setattr(exif_utils, '_exiftool_has_only_warnings', lambda *_: False)
    assert transport.extract_candidate(storage, 'photo.' + profile[0], row) is None


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('changes', [
    {'ExifToolVersion': '13.56'}, {'Make': 'Unknown'}, {'Model': 'Future Camera'},
    {'FileType': 'JPEG'}, {'Warning': 'Corrupt metadata'}, {'Error': 'Partial data'},
    {'EXIF.Truncated': 'true'},
])
def test_warning_qualification_is_not_a_blanket_exemption(profile, changes):
    assert not transport._verified_source_warning_profile('photo.' + profile[0], {**metadata(profile), **changes})


def test_warning_dng_version_and_filename_are_required():
    exif = metadata(PROFILES[4])
    assert not transport._verified_source_warning_profile('photo.dng', {**exif, 'DNGVersion': '1 7 0 0'})
    assert not transport._verified_source_warning_profile('photo.raw', exif)


def test_warning_qualification_cannot_bypass_missing_camera_ranges(monkeypatch):
    import metadata_tiff_coverage
    storage, row = _candidate_fixture()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: metadata(PROFILES[1]))
    monkeypatch.setattr(exif_utils, '_exiftool_has_only_warnings', lambda *_: pytest.fail('Coverage must qualify first'))
    monkeypatch.setattr(metadata_tiff_coverage, 'verified_camera_ranges', lambda *_: False)
    outcomes = []
    assert transport.extract_candidate(storage, 'photo.srw', row, on_outcome=outcomes.append) is None
    assert outcomes == ['incomplete_camera_metadata']


@pytest.mark.parametrize('category', sorted(exif_utils.ExifExtractionError.CATEGORIES))
def test_packet_parser_failures_have_bounded_outcomes_not_no_exif(monkeypatch, category):
    storage, row = _candidate_fixture()
    def fail(_):
        raise exif_utils.ExifExtractionError(category)
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', fail)
    outcomes, diagnostics = [], []
    assert transport.extract_candidate(storage, 'photo.dng', row,
        on_outcome=outcomes.append, on_diagnostic=diagnostics.append) is None
    assert outcomes == ['exiftool_' + category]
    assert diagnostics == [{'version': 'unknown', 'category': category}]
