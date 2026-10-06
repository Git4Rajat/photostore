"""Make the backend package importable from tests (backend/ is the root)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# No background flush timer in unit tests (they flush explicitly or through the reader).
os.environ.setdefault('DIRTY_FLUSH_DELAY_SECONDS', '0')

# app.py registers only a subset of blueprints per-process, keyed off the
# APP_ROLE env var (tools/upload/admin/extras each get their own slim
# container app in production; a bare/unset role -- what a bare `pytest` run
# has, same as this suite -- gets the slim 'backend' set: auth+photos+
# albums+system, see app.py's registration block). Tests that dispatch a
# real request through app.app.test_client()/url_map (rather than calling a
# route function directly) need the OTHER groups' routes registered too, or
# they 404 regardless of the business logic under test -- worse, a test
# asserting `status_code == 404` for the "wrong" reason (route doesn't exist
# at all) would silently keep passing after the split for a reason that has
# nothing to do with what it's meant to verify. Defensively register every
# blueprint not already present so this one process's app instance always
# exposes the full surface for tests, regardless of which role the
# APP_ROLE-conditional block picked at import time. This does NOT undermine
# per-role registration coverage -- test_app_role_blueprint_registration.py
# deliberately reimports app.py in an isolated subprocess per role, so it
# never sees this session-wide registration.
import app as _app_module  # noqa: E402
import storage_utils  # noqa: E402
from routes.auth import auth_bp as _auth_bp  # noqa: E402
from routes.upload import upload_bp as _upload_bp  # noqa: E402
from routes.photos import photos_bp as _photos_bp  # noqa: E402
from routes.people import people_bp as _people_bp  # noqa: E402
from routes.albums import albums_bp as _albums_bp  # noqa: E402
from routes.public import public_bp as _public_bp  # noqa: E402
from routes.library import library_bp as _library_bp  # noqa: E402
from routes.tools import tools_bp as _tools_bp  # noqa: E402
from routes.admin import admin_bp as _admin_bp  # noqa: E402
from routes.system import system_bp as _system_bp  # noqa: E402

for _bp in (_auth_bp, _upload_bp, _photos_bp, _people_bp, _albums_bp, _public_bp, _library_bp, _tools_bp, _admin_bp, _system_bp):
    if _bp.name not in _app_module.app.blueprints:
        _app_module.app.register_blueprint(_bp)


@pytest.fixture(autouse=True)
def _reset_search_index_dirty_marking_state():
    """storage_utils keeps a few small in-process dicts (dirty-filename
    write buffer, manifest-already-dirty flags, rebuild cooldown timestamps
    -- see _mark_search_index_dirty_filenames/_manifest_already_marked_dirty/
    _index_rebuild_in_cooldown) that persist across calls by design, so a
    sustained upload doesn't re-write the same "still dirty" state over and
    over. That persistence is exactly what makes it a test-isolation hazard:
    many tests reuse generic ids like 'u1' with a fresh, throwaway fake table
    each time, but these dicts are keyed by that same id and never tied to
    any particular table instance -- a test that marks a filename dirty
    without ever reading/clearing it (there's no requirement that it does)
    leaves it sitting in the buffer, where a *later*, unrelated test using
    the same id would flush it straight into ITS table. Confirmed live: adding
    the dirty-filename buffer made test_clear_removes_only_named_filenames
    flaky depending on what ran before it, purely from this leakage. Reset
    before every test so each one starts clean regardless of run order."""
    storage_utils._DIRTY_FILENAME_BUFFER.clear()
    storage_utils._INDEX_MANIFEST_DIRTY_FLAGS.clear()
    storage_utils._INDEX_REBUILD_LAST_COMPLETED_AT.clear()
    yield
    storage_utils._DIRTY_FILENAME_BUFFER.clear()
    storage_utils._INDEX_MANIFEST_DIRTY_FLAGS.clear()
    storage_utils._INDEX_REBUILD_LAST_COMPLETED_AT.clear()


@pytest.fixture(autouse=True)
def _tests_run_as_an_index_build_role(monkeypatch):
    """Index builds are restricted to the worker/ipworker roles in production
    (storage_utils.index_build_allowed). The bare test process has no APP_ROLE, so
    pretend to be a build role by default; tests/test_index_build_guard.py flips
    it off to pin the serving-process behaviour."""
    monkeypatch.setattr(storage_utils, '_ROLE_MAY_BUILD_INDEXES', True)


@pytest.fixture(autouse=True)
def _sequential_table_scans_by_default(monkeypatch):
    """Table fakes in most tests only understand the plain partition filter; the parallel
    RowKey-range scan is covered by tests/test_table_scan.py (explicit workers=)."""
    import table_scan
    monkeypatch.setattr(table_scan, 'PARALLELISM', 1)


@pytest.fixture(autouse=True)
def _no_manifest_cache_in_tests(monkeypatch):
    """Tests publish new manifests and expect the next read to see them."""
    import search_db
    monkeypatch.setattr(search_db, 'MANIFEST_TTL_SECONDS', 0)
