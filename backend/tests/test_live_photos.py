import json

import live_photos as lp
from catalog_metadata import is_live_photo_clip


def clip(name, content_id='', captured='', modified='', **extra):
    info = {'contentId': content_id, 'capturedAt': captured}
    return {'RowKey': name, lp.CLIP_INFO: json.dumps(info), 'clientLastModified': modified, **extra}


def photo(name, content_id='', taken='', offset='', modified='', **extra):
    exif = {}
    if content_id:
        exif['ContentIdentifier'] = content_id
    if taken:
        exif['DateTimeOriginal'] = taken
    if offset:
        exif['OffsetTimeOriginal'] = offset
    return {'RowKey': name, 'exifData': json.dumps(exif), 'clientLastModified': modified, **extra}


def test_names_drop_export_copy_numbers_and_upload_collision_suffixes():
    assert lp.base_stem('IMG_1234 (2).MOV') == 'IMG_1234'
    assert lp.base_stem('IMG_1234-1a2b3c4d.HEIC') == 'IMG_1234'
    assert lp.base_stem('FullSizeRender (13).mov') == 'FullSizeRender'
    assert lp.exact_stem('IMG_1234 (2)-1a2b3c4d.MOV') == 'IMG_1234 (2)'


def test_only_videos_with_apple_live_photo_tags_are_clips():
    tags = {'CreateDate': '2025-08-10 15:08:29+02:00',
            'MediaInfo.General.0.extra.com_apple_quicktime_livephoto_auto': '1',
            'MediaInfo.General.0.extra.com_apple_quicktime_content_identifier': 'ABC'}
    assert lp.clip_info_from_tags(tags) == {'contentId': 'ABC', 'capturedAt': '2025-08-10 15:08:29+02:00'}
    assert lp.clip_info_from_tags({'CreateDate': '2025-08-10 15:08:29+02:00',
                                   'MediaInfo.General.0.extra.com_apple_quicktime_content_identifier': 'ABC'}) is None
    assert lp.clip_info({lp.CLIP_INFO: lp.NOT_A_CLIP}) is None


def test_apple_content_identifier_decides_when_both_halves_have_it():
    rows = [clip('IMG_1.MOV', 'ID-2'), photo('IMG_1.HEIC', 'ID-1'), photo('IMG_1 (1).HEIC', 'ID-2')]
    assert lp.choose_pairs(rows) == [('IMG_1.MOV', 'IMG_1 (1).HEIC')]


def test_a_different_content_identifier_is_never_paired_even_at_the_same_second():
    rows = [clip('IMG_1.MOV', 'ID-2', '2025-08-10 15:08:29+02:00'),
            photo('IMG_1.HEIC', 'ID-1', '2025:08:10 15:08:29', '+02:00')]
    assert lp.choose_pairs(rows) == []


def test_capture_time_pairs_renumbered_copies_across_time_zones():
    rows = [clip('IMG_7 (2).MOV', captured='2025-08-10 13:08:29 UTC'),
            photo('IMG_7.HEIC', taken='2024:01:01 09:00:00', offset='+01:00'),
            photo('IMG_7 (1).HEIC', taken='2025:08:10 15:08:30', offset='+02:00')]
    assert lp.choose_pairs(rows) == [('IMG_7 (2).MOV', 'IMG_7 (1).HEIC')]


def test_capture_time_without_offsets_compares_wall_clocks():
    rows = [clip('IMG_8.MOV', captured='2025-08-10 15:08:29+02:00'), photo('IMG_8.HEIC', taken='2025:08:10 15:08:28')]
    assert lp.choose_pairs(rows) == [('IMG_8.MOV', 'IMG_8.HEIC')]


def test_unprocessed_photos_pair_by_exact_name_and_modified_time_only():
    rows = [clip('IMG_9.MOV', modified='2025-01-01T10:00:00+00:00'),
            photo('IMG_9.HEIC', modified='2025-01-01T10:00:01+00:00'),
            photo('IMG_9 (1).HEIC', modified='2025-01-01T10:00:00+00:00')]
    assert lp.choose_pairs(rows) == [('IMG_9.MOV', 'IMG_9.HEIC')]
    assert lp.choose_pairs([rows[0], photo('IMG_9.HEIC', modified='2025-01-02T10:00:00+00:00')]) == []


def test_each_photo_takes_one_clip_and_attached_halves_stay_put():
    same = '2025-08-10 15:08:29+02:00'
    rows = [clip('IMG_3.MOV', captured=same), clip('IMG_3 (1).MOV', captured=same),
            photo('IMG_3.HEIC', taken='2025:08:10 15:08:29', offset='+02:00')]
    assert lp.choose_pairs(rows) == [('IMG_3.MOV', 'IMG_3.HEIC')]
    rows[0][lp.CLIP_OF] = 'IMG_3.HEIC'
    rows[2][lp.PHOTO_CLIP] = 'IMG_3.MOV'
    assert lp.choose_pairs(rows) == []


def test_deleted_rows_and_ordinary_videos_take_no_part():
    rows = [clip('IMG_4.MOV', 'ID'), photo('IMG_4.HEIC', 'ID', processing_state='deleted'),
            {'RowKey': 'IMG_5.MOV', lp.CLIP_INFO: lp.NOT_A_CLIP}, photo('IMG_5.HEIC')]
    assert lp.choose_pairs(rows) == []


def test_only_an_attached_clip_counts_as_one():
    assert is_live_photo_clip({'RowKey': 'IMG_1.MOV', lp.CLIP_OF: 'IMG_1.HEIC'})
    assert not is_live_photo_clip({'RowKey': 'IMG_1.HEIC', lp.PHOTO_CLIP: 'IMG_1.MOV'})
    assert not is_live_photo_clip({'RowKey': 'IMG_1.MOV', lp.CLIP_OF: ''})


def test_a_short_video_with_apples_identifier_is_a_clip_even_without_live_photo_tags():
    base = {'CreateDate': '2021-11-04 21:13:50+05:30',
            'MediaInfo.General.0.extra.com_apple_quicktime_content_identifier': 'ABC'}
    assert lp.clip_info_from_tags({**base, 'Duration': '2.5'}) == {'contentId': 'ABC', 'capturedAt': base['CreateDate']}
    assert lp.clip_info_from_tags({**base, 'Duration': '12.0'}) is None             # an ordinary video
    assert lp.clip_info_from_tags({**base}) is None                                 # length unknown
    assert lp.clip_info_from_tags({'Duration': '2.5', 'CreateDate': base['CreateDate']}) is None


def test_a_clip_recorded_seconds_before_its_still_is_still_its_clip():
    rows = [clip('IMG_3020.MOV', captured='2023-06-28 21:43:12+02:00'),
            photo('IMG_3020.HEIC', taken='2023:06:28 21:43:16', offset='+02:00')]
    assert lp.choose_pairs(rows) == [('IMG_3020.MOV', 'IMG_3020.HEIC')]
    rows[1] = photo('IMG_3020.HEIC', taken='2023:06:28 21:43:19', offset='+02:00')
    assert lp.choose_pairs(rows) == []


def test_a_png_is_never_a_live_photos_still():
    rows = [clip('IMG_1.MOV', modified='2025-01-01T10:00:00+00:00'), photo('IMG_1.PNG', modified='2025-01-01T10:00:00+00:00')]
    assert lp.choose_pairs(rows) == []


def test_a_browser_read_photo_without_apples_identifier_is_not_a_clips_still():
    browser = json.dumps({'client_exif': {'source': 'browser_exiftool'}})
    rows = [clip('IMG_5.MOV', 'ID-5', '2025-08-10 15:08:29+02:00'),
            photo('IMG_5.HEIC', taken='2025:08:10 15:08:29', offset='+02:00', processing_metadata=browser)]
    assert lp.choose_pairs(rows) == []
    rows[1]['processing_metadata'] = '{}'           # server EXIF never carries the identifier: time still decides
    assert lp.choose_pairs(rows) == [('IMG_5.MOV', 'IMG_5.HEIC')]
