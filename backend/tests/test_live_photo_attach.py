"""Live Photo clips: attached to their photo at upload and by the Tools action, hidden from the catalog,
purged with their photo."""
import json

import pytest

import app
import live_photos as lp
import search_db
import storage_utils
import video_metadata
from fakes import FakeTable

SAME_MOMENT = '2025-08-10 15:08:29+02:00'


@pytest.fixture
def library(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)

    def update(user_id, filename, updates, *, derive_updates=None, initial_entity=None):
        try:
            row = dict(table.get_entity(user_id, filename))
        except Exception:
            return None
        if str(row.get('processing_state') or '') == 'deleted':
            return None
        row.update(derive_updates(row) if derive_updates else updates)
        table.upsert_entity(row)
        return row
    monkeypatch.setattr(app, '_update_metadata_entity_fields', update)

    def add(name, **fields):
        table.upsert_entity({'PartitionKey': 'lib', 'RowKey': name, 'uploadDate': '2026-11-10T19:00:00+00:00',
                             'anonymousImageId': 'blob-' + name, **fields})
    return table, add


def clip_fields(content_id='', captured=SAME_MOMENT):
    return {lp.CLIP_INFO: json.dumps({'contentId': content_id, 'capturedAt': captured})}


def still_fields(content_id='', taken='2025:08:10 15:08:29', offset='+02:00'):
    exif = {'DateTimeOriginal': taken, 'OffsetTimeOriginal': offset}
    if content_id:
        exif['ContentIdentifier'] = content_id
    return {'exifData': json.dumps(exif)}


def row(table, name):
    return table.get_entity('lib', name)


def test_attaching_links_both_halves_once(library):
    table, add = library
    add('IMG_1.HEIC', **still_fields())
    add('IMG_1.MOV', **clip_fields())
    add('IMG_10.HEIC', **still_fields())            # shares the prefix, not the name
    assert app.attach_live_photos('lib', 'IMG_1') == [('IMG_1.MOV', 'IMG_1.HEIC')]
    assert row(table, 'IMG_1.MOV')[lp.CLIP_OF] == 'IMG_1.HEIC'
    assert row(table, 'IMG_1.HEIC')[lp.PHOTO_CLIP] == 'IMG_1.MOV'
    assert not row(table, 'IMG_10.HEIC').get(lp.PHOTO_CLIP)
    assert app.attach_live_photos('lib', 'IMG_1') == []


def test_a_photo_claimed_meanwhile_keeps_its_clip(library, monkeypatch):
    table, add = library
    add('IMG_2.HEIC', **still_fields(), **{lp.PHOTO_CLIP: 'IMG_2 (1).MOV'})
    add('IMG_2.MOV', **clip_fields())
    monkeypatch.setattr(lp, 'choose_pairs', lambda rows: [('IMG_2.MOV', 'IMG_2.HEIC')])
    assert app.attach_live_photos('lib', 'IMG_2') == []
    assert row(table, 'IMG_2.HEIC')[lp.PHOTO_CLIP] == 'IMG_2 (1).MOV'
    assert not row(table, 'IMG_2.MOV').get(lp.CLIP_OF)


def test_upload_hook_looks_up_only_live_photo_halves(library, monkeypatch):
    table, add = library
    add('plain.jpg', **still_fields())
    add('movie.mov', **{lp.CLIP_INFO: lp.NOT_A_CLIP})
    monkeypatch.setattr(app, '_live_photo_rows', lambda *a: pytest.fail('looked up a non-Live-Photo upload'))
    assert app.attach_live_photo_after_upload('lib', 'plain.jpg') == []
    assert app.attach_live_photo_after_upload('lib', 'movie.mov') == []


def test_upload_hook_pairs_whichever_half_arrives_second(library):
    table, add = library
    add('IMG_3.HEIC', **still_fields('ID-3'))
    assert app.attach_live_photo_after_upload('lib', 'IMG_3.HEIC') == []          # the clip is not here yet
    add('IMG_3.MOV', **clip_fields('ID-3'))
    assert app.attach_live_photo_after_upload('lib', 'IMG_3.MOV') == [('IMG_3.MOV', 'IMG_3.HEIC')]


def test_purging_a_photo_also_purges_its_clip(library):
    table, add = library
    add('IMG_4.HEIC', **{lp.PHOTO_CLIP: 'IMG_4.MOV'})
    add('IMG_4.MOV', **{lp.CLIP_OF: 'IMG_4.HEIC'})
    add('IMG_5.HEIC')
    assert app._attached_live_clips('lib', ['IMG_4.HEIC', 'IMG_5.HEIC']) == ['IMG_4.MOV']
    assert app._attached_live_clips('lib', ['IMG_4.HEIC', 'IMG_4.MOV']) == []


def test_gallery_and_search_leave_an_attached_clip_out_and_badge_its_photo():
    clip_row = {'PartitionKey': 'lib', 'RowKey': 'IMG_6.MOV', 'uploadDate': 'x', lp.CLIP_OF: 'IMG_6.HEIC'}
    photo_row = {'PartitionKey': 'lib', 'RowKey': 'IMG_6.HEIC', 'uploadDate': 'x', lp.PHOTO_CLIP: 'IMG_6.MOV'}
    assert storage_utils._sort_index_row(clip_row) is None
    assert search_db.row_record(clip_row) is None
    assert storage_utils._sort_index_row(photo_row)['gallery']['liveClip'] == 'IMG_6.MOV'
    assert 'liveClip' not in storage_utils._sort_index_row({**photo_row, lp.PHOTO_CLIP: ''})['gallery']
    assert storage_utils.metadata_index_kinds({lp.CLIP_OF: 'IMG_6.HEIC'}) >= {'sort', 'lexical'}


def test_tools_action_detects_clips_attaches_them_and_can_undo(library, monkeypatch):
    table, add = library
    add('IMG_7.HEIC', **still_fields())
    add('IMG_7.MOV')
    add('trip.mov')
    live_tags = {'CreateDate': SAME_MOMENT, 'MediaInfo.General.0.extra.com_apple_quicktime_livephoto_auto': '1'}
    monkeypatch.setattr(video_metadata, 'browser_video_tags',
                        lambda r: live_tags if r['RowKey'] == 'IMG_7.MOV' else {'CreateDate': SAME_MOMENT})
    monkeypatch.setattr(app, '_require_user_id', lambda: ('lib', None))
    client = app.app.test_client()
    body = client.post('/api/tools/live-photos/attach', json={}).get_json()
    assert body == {'checked': 2, 'clips': 1, 'attached': 1, 'newlyAttached': 1, 'complete': True,
                    'lastChecked': 'trip.mov'}
    assert row(table, 'IMG_7.MOV')[lp.CLIP_OF] == 'IMG_7.HEIC'
    assert row(table, 'trip.mov')[lp.CLIP_INFO] == lp.NOT_A_CLIP
    again = client.post('/api/tools/live-photos/attach', json={}).get_json()
    assert (again['clips'], again['attached'], again['newlyAttached']) == (1, 1, 0)
    undone = client.post('/api/tools/live-photos/attach', json={'undo': True}).get_json()
    assert undone['undone'] == 2 and undone['complete']
    assert not row(table, 'IMG_7.MOV')[lp.CLIP_OF] and not row(table, 'IMG_7.HEIC')[lp.PHOTO_CLIP]
