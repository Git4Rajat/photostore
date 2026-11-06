"""Worker-only summary publication adapters; never discover source dirty rows.

Search maintenance owns its bounded dirty capture and successful delta drain.
It calls app._refresh_library_summaries afterwards, even without compaction.
All authoritative contributions and the sequence live in the search SQLite
transaction. These small hydrated blobs are derived outputs, not a sidecar
source with an independent remote identity.
"""
from __future__ import annotations

from datetime import datetime, timezone

from summary_incremental import DATABASE_FORMAT


RAW_FORMAT = 'raw-stream-summary-v1'


def publish_indexed_payloads(user_id, explore, timeline, *, store_explore, store_timeline,
                             load_explore, load_timeline, skip_unchanged=False):
    """Verify the existing best-effort stores instead of acknowledging failure.

    This is called under the maintenance catalog serializer. No new remote
    manifest/source or publication aliases are introduced. A partial upload
    leaves retryable derived outputs; their matching source tokens certify the
    committed SQLite snapshot, not an atomic transaction across two blobs.
    """
    def publish(payload, load, store, label):
        if skip_unchanged:
            previous = load(user_id)
            # Timestamp-only churn is not new derived data. Require the exact
            # snapshot identity AND content so missing/corrupt blobs get repaired.
            if (isinstance(previous, dict) and payload.get('sourceVersion')
                    and {k: v for k, v in previous.items() if k != 'updatedAt'}
                    == {k: v for k, v in payload.items() if k != 'updatedAt'}):
                return
        store(user_id, payload)
        if load(user_id) != payload:
            raise RuntimeError(label + ' summary publication was not confirmed')

    publish(explore, load_explore, store_explore, 'Explore')
    publish(timeline, load_timeline, store_timeline, 'Timeline')


def finalized_payloads(db, user_id, *, accumulator_factory, allow_full_adapter=False):
    """Select before hydrating, just like ExploreAccumulator.finalize.

    Fake/legacy full-builder adapters with only iter_smart_rows remain valid.
    Real SearchDatabase objects never fall back on a missing aggregate schema,
    and incremental callers disable this adapter entirely.
    """
    snapshot_method = getattr(db, 'summary_snapshot', None)
    accumulator = accumulator_factory()
    if callable(snapshot_method):
        snapshot = snapshot_method()
        groups = snapshot['groups']
        accumulator.place_groups = {str(i): item for i, item in enumerate(groups['places'])}
        accumulator.thing_groups = {str(i): item for i, item in enumerate(groups['things'])}
        timeline = snapshot['timeline']
        source = snapshot['sourceVersion']
    elif allow_full_adapter:
        for row in db.iter_smart_rows():
            accumulator.add(row)
        timeline = db.timeline_summary()
        source = 'full-builder-adapter:' + datetime.now(timezone.utc).isoformat()
    else:
        raise RuntimeError('incremental summaries require an indexed search summary snapshot')
    summary = accumulator.finalize(user_id)
    updated = datetime.now(timezone.utc).isoformat()
    explore = {'sourceVersion': source, 'updatedAt': updated, 'summaryMode': DATABASE_FORMAT,
               'places': summary.get('places', []), 'things': summary.get('things', [])}
    # Both derived artifacts identify the SAME SQLite read snapshot. Rollover
    # changes the token even if the committed sequence did not change.
    return explore, dict(timeline, sourceVersion=source, summaryMode=DATABASE_FORMAT)


class RawExploreSink:
    """Preserve the original full raw-stream algorithm, with an explicit mode."""

    def __init__(self, user_id, source_version, accumulator, store):
        self.user_id, self.source_version = user_id, source_version
        self.accumulator, self.store = accumulator, store

    def add(self, row):
        self.accumulator.add(row)

    def abort(self):
        pass

    def finish(self):
        summary = self.accumulator.finalize(self.user_id)
        self.store(self.user_id, dict(summary, sourceVersion=self.source_version,
                                     updatedAt=datetime.now(timezone.utc).isoformat(), summaryMode=RAW_FORMAT))


class RawTimelineSink:
    def __init__(self, user_id, source_version, accumulator, store):
        self.user_id, self.source_version = user_id, source_version
        self.accumulator, self.store = accumulator, store

    def add(self, row):
        self.accumulator.add(row)

    def abort(self):
        pass

    def finish(self):
        self.store(self.user_id, dict(self.accumulator.summary(), sourceVersion=self.source_version,
                                     summaryMode=RAW_FORMAT))