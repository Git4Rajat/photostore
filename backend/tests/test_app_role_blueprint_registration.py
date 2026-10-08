"""Pins app.py's APP_ROLE-conditional blueprint registration: the tools and
upload service splits (2026-09-15) mean tools_bp/upload_bp routes must be
registered on their own dedicated APP_ROLE containers and NOT on the
default/backend role, or the "split" would just be a redundant duplicate
rather than an actual narrower service. Re-imports app.py in a fresh
subprocess per role since blueprint registration only runs once, at module
import time.
"""
from __future__ import annotations

import os
import subprocess
import sys

_SCRIPT = "import app\nprint('\\n'.join(sorted({r.rule for r in app.app.url_map.iter_rules()})))\n"


def _rule_paths_for_role(role: str | None) -> set[str]:
    env = dict(os.environ)
    if role:
        env['APP_ROLE'] = role
    else:
        env.pop('APP_ROLE', None)
    result = subprocess.run(
        [sys.executable, '-c', _SCRIPT],
        cwd=__file__.rsplit('/tests/', 1)[0],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return {line for line in result.stdout.splitlines() if line.strip()}


def test_backend_role_does_not_serve_tools_routes():
    paths = _rule_paths_for_role(None)
    assert '/api/tools/workbench/actions' not in paths
    assert '/api/photos' in paths  # sanity: other groups still registered


def test_backend_role_does_not_serve_upload_routes():
    paths = _rule_paths_for_role(None)
    assert '/api/upload/init' not in paths
    assert '/api/photos' in paths  # sanity: other groups still registered


def test_backend_role_does_not_serve_admin_routes():
    paths = _rule_paths_for_role(None)
    assert '/api/admin/people/dedupe-faces' not in paths
    assert '/api/photos' in paths  # sanity: other groups still registered


def test_backend_role_does_not_serve_extras_routes():
    """2026-09-17: library/public moved to their own 'extras' role so backend can
    shrink to a 0.5vCPU/1Gi everyday-browsing tier. people_bp moved back to
    'backend' 2026-10-08 once get_person's request-path full-account scan was
    replaced with a bounded per-person fetch -- see app.py's 'extras' branch
    comment -- so backend DOES serve people_bp now; only library/public don't."""
    paths = _rule_paths_for_role(None)
    assert '/api/persons/page' in paths  # people_bp moved here 2026-10-08
    assert '/api/faces/delete' in paths
    assert '/api/library/mine' not in paths
    assert '/public/albums/<token>' not in paths
    assert '/api/photos' in paths  # sanity: other groups still registered
    assert '/health' in paths  # system_bp stays on backend -- see app.py's comment


def test_extras_role_does_not_serve_people_routes():
    """people_bp left 'extras' 2026-10-08 -- pin that it's actually gone, not
    just that backend gained a copy (blueprints could in principle be
    registered on both)."""
    paths = _rule_paths_for_role('extras')
    assert '/api/persons/page' not in paths
    assert '/api/faces/delete' not in paths
    assert '/api/library/mine' in paths  # sanity: extras still serves its own routes


def test_tools_role_serves_only_tools_routes():
    paths = _rule_paths_for_role('tools')
    non_static = {p for p in paths if not p.startswith('/static') and p != '/health'}   # /health is on every role (warm-up probe)
    assert non_static == {
        '/api/tools/workbench/actions',
        '/api/tools/workbench/actions/<action_id>',
        # The derived-index builder moved here from backend (2026-09-30): tools
        # is the 2vCPU/4Gi role that can safely scan a full metadata partition.
        '/api/tools/indexes/build',
        '/api/tools/indexes/status',
        # jobs_status moved here from backend (2026-10-01): polled
        # continuously by every session, indefinitely -- competed with
        # interactive gallery traffic for backend's thin GUNICORN_WORKERS=2/
        # THREADS=2 pool. See routes/tools.py's comment on jobs_status.
        '/api/jobs/status',
        '/jobs/status',
    }


def test_backend_role_does_not_serve_jobs_status():
    """jobs_status moved to tools (2026-10-01) -- see the tools-role test
    above. Backend must not keep serving it too, or the move wouldn't
    actually relieve backend's thread pool of this continuous poll."""
    paths = _rule_paths_for_role(None)
    assert '/api/jobs/status' not in paths
    assert '/jobs/status' not in paths
    assert '/health' in paths  # sanity: system_bp's other routes still registered


def test_upload_role_serves_only_upload_routes():
    paths = _rule_paths_for_role('upload')
    non_static = {p for p in paths if not p.startswith('/static')}
    assert non_static  # non-empty
    assert '/health' in non_static  # own health route -- frontend's warm-up probe hits this origin directly
    non_health = non_static - {'/health'}
    assert all(p.startswith(('/upload', '/api/upload', '/uploads', '/api/uploads')) for p in non_health)
    assert '/api/upload/init' in non_static


def test_admin_role_serves_only_admin_routes():
    paths = _rule_paths_for_role('admin')
    non_static = {p for p in paths if not p.startswith('/static') and p != '/health'}   # /health is on every role (warm-up probe)
    assert non_static  # non-empty
    assert all(p.startswith(('/admin', '/api/admin')) for p in non_static)
    assert '/api/admin/people/dedupe-faces' in non_static
    assert '/api/admin/jobs/status' in non_static


def test_extras_role_serves_only_extras_routes():
    """people_bp moved to 'backend' 2026-10-08 -- extras now carries only
    library/public (see app.py's 'extras' branch comment)."""
    paths = _rule_paths_for_role('extras')
    non_static = {p for p in paths if not p.startswith('/static') and p != '/health'}   # /health is on every role (warm-up probe)
    assert non_static  # non-empty
    extras_prefixes = ('/api/library', '/public', '/api/public')
    assert all(p.startswith(extras_prefixes) for p in non_static)
    assert '/api/library/mine' in non_static
    assert '/public/albums/<token>' in non_static
