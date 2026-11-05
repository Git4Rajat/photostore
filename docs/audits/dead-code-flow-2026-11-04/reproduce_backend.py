"""Offline audit probes, asserting current defects rather than desired behavior.

Runs selected, unmodified function bodies extracted from production source.
Dependencies are fakes; this does not import app.py or contact storage.
Run from the repository root with the project's Python environment.
"""
from __future__ import annotations

import ast
import copy
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parents[3]


def load(relative, names, namespace):
    tree = ast.parse((ROOT / relative).read_text())
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    assert {n.name for n in nodes} == set(names)
    namespace.update(dict(Optional=Optional, Dict=Dict, List=List, Any=Any))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), relative, 'exec'), namespace)
    return namespace


def run():
    results = {}
    flask = Flask('offline-flow-audit')
    logger = logging.getLogger('offline-flow-audit')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False

    # Manifest contains a delta, but the serving summary loses it.
    manifest = dict(sourceVersion='base-v1', rowCount=1, deltaSeq=3)
    summary_ns = load('backend/storage_utils.py', ['get_index_manifest_summary'], {
        '_index_manifest_loaders': lambda: {'sort': lambda _: manifest},
    })
    summary = summary_ns['get_index_manifest_summary']('shared-library', 'sort')
    assert 'delta_seq' not in summary
    results['sort_manifest_delta_lost'] = dict(manifestDelta=3, summaryDelta=summary.get('delta_seq'))

    common = SimpleNamespace(app=SimpleNamespace(logger=logger), jsonify=jsonify,
        request=request, _require_user_id=lambda: ('shared-library', None),
        _validate_media_filename=lambda name: name)
    common.get_index_manifest_summary = lambda *_: summary
    common.get_sort_index_blob_location = lambda _: ('catalog', 'sort.json')
    common._create_stable_read_sas_url = lambda c, b: ('https://example.invalid/' + b, 'later')
    ns = load('backend/routes/photos.py', ['photos_sort_index'], {
        'app': common, 'SORT_INDEX_CLIENT_MAX_ROWS': 200_000,
    })
    with flask.test_request_context('/api/photos/sort-index'):
        body = ns['photos_sort_index']().get_json()
    assert body['deltaSeq'] == 0 and body['deltaUrls'] == []
    results['sort_route_hides_deltas'] = body

    # Use the real compatibility shim: account and library are different IDs.
    actor = ['alice']
    auth_ns = load('backend/app.py', ['_require_user_id'], {
        '_require_library_context': lambda **_: (actor[0], 'shared-library', None),
    })
    common._require_user_id = auth_ns['_require_user_id']
    common.json = json
    row = dict(PartitionKey='shared-library', RowKey='photo.jpg', likedBy='[]', likes=0)
    common._get_metadata_entity = lambda *_: dict(row)
    def update(*args, **kwargs):
        row.update(args[2])
        return dict(row)
    common._update_metadata_entity_fields = update
    ns = load('backend/routes/photos.py', ['toggle_like_photo', 'set_photo_rating'], {'app': common})
    responses = []
    for account in ('alice', 'bob'):
        actor[0] = account
        with flask.test_request_context('/photos/photo.jpg/like', method='POST'):
            responses.append(dict(account=account, **ns['toggle_like_photo']('photo.jpg').get_json()))
    assert [r['likes'] for r in responses] == [1, 0]
    results['two_accounts_share_one_like_identity'] = responses

    # The real write helper returns None when its storage read fails.
    class UnavailableTable:
        def get_entity(self, **_):
            raise RuntimeError('injected storage outage')
    helper = load('backend/app.py', ['_update_metadata_entity_fields'], {
        'metadata_table_client': UnavailableTable(),
    })['_update_metadata_entity_fields']
    common._update_metadata_entity_fields = helper
    with flask.test_request_context('/photos/photo.jpg/rating', method='POST', json={'rating': 5}):
        response = ns['set_photo_rating']('photo.jpg')
        body = response.get_json()
    assert response.status_code == 200 and body['success'] and 'rating' not in row
    results['rating_reports_success_without_write'] = dict(response=body, storedRating=row.get('rating'))

    # A failed secondary trash write is swallowed; replay skips its repair.
    class Table:
        def __init__(self, rows):
            self.rows = copy.deepcopy(rows)
            self.fail_next_upsert = False
        def get_entity(self, partition_key, row_key):
            return copy.deepcopy(self.rows[(partition_key, row_key)])
        def upsert_entity(self, entity):
            if self.fail_next_upsert:
                self.fail_next_upsert = False
                raise RuntimeError('injected trash-index outage')
            self.rows[(entity['PartitionKey'], entity['RowKey'])] = copy.deepcopy(entity)
        def query_entities(self, _):
            return list(self.rows.values())
    metadata = Table({('shared-library', 'photo.jpg'): dict(
        PartitionKey='shared-library', RowKey='photo.jpg', processing_state='active', thumbnail_status='done')})
    trash = Table({('shared-library', '__init__'): dict(PartitionKey='shared-library', RowKey='__init__')})
    trash.fail_next_upsert = True
    delete_ns = load('backend/app.py', [
        '_mark_processing_deleted_for_file', '_trash_index_add', '_trash_index_entries',
    ], dict(metadata_table_client=metadata, trash_index_table_client=trash,
        BROWSER_PROCESSING_STATUS_FIELDS=[('thumbnail_status', 'thumbnail')],
        datetime=datetime, timezone=timezone, json=json,
        app=SimpleNamespace(logger=logger), _TRASH_INDEX_INIT_KEY='__init__',
        _escape_odata=lambda x: x))
    delete_ns['_mark_processing_deleted_for_file']('shared-library', 'photo.jpg')
    delete_ns['_mark_processing_deleted_for_file']('shared-library', 'photo.jpg')
    entries = delete_ns['_trash_index_entries']('shared-library')
    assert metadata.rows[('shared-library', 'photo.jpg')]['processing_state'] == 'deleted' and entries == []
    results['trash_secondary_write_not_repaired_by_replay'] = dict(metadataState='deleted', trashEntries=entries)

    # A snapshot read before another writer's edit is written back without CAS.
    metadata.rows[('shared-library', 'photo.jpg')].update(processing_state='active', rating=1)
    original_get = metadata.get_entity
    def concurrent_get(**kwargs):
        snapshot = original_get(**kwargs)
        metadata.rows[('shared-library', 'photo.jpg')]['rating'] = 5
        return snapshot
    metadata.get_entity = concurrent_get
    delete_ns['_mark_processing_deleted_for_file']('shared-library', 'photo.jpg')
    assert metadata.rows[('shared-library', 'photo.jpg')]['rating'] == 1
    results['delete_overwrites_concurrent_metadata_edit'] = dict(concurrentRating=5, finalRating=1)
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    run()
