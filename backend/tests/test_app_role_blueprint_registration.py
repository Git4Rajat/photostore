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
    """2026-09-17: people/library/public moved to their own 'extras' role so
    backend can shrink to a 0.5vCPU/1Gi everyday-browsing tier -- see
    backend-cpu-optimization-2026-09 memory."""
    paths = _rule_paths_for_role(None)
    assert '/api/persons' not in paths
    assert '/api/faces' not in paths
    assert '/api/library/mine' not in paths
    assert '/public/albums/<token>' not in paths
    assert '/api/photos' in paths  # sanity: other groups still registered
    assert '/health' in paths  # system_bp stays on backend -- see app.py's comment


def test_tools_role_serves_only_tools_routes():
    paths = _rule_paths_for_role('tools')
    non_static = {p for p in paths if not p.startswith('/static')}
    assert non_static == {'/api/tools/workbench/actions', '/api/tools/workbench/actions/<action_id>'}


def test_upload_role_serves_only_upload_routes():
    paths = _rule_paths_for_role('upload')
    non_static = {p for p in paths if not p.startswith('/static')}
    assert non_static  # non-empty
    assert all(p.startswith(('/upload', '/api/upload', '/uploads', '/api/uploads')) for p in non_static)
    assert '/api/upload/init' in non_static


def test_admin_role_serves_only_admin_routes():
    paths = _rule_paths_for_role('admin')
    non_static = {p for p in paths if not p.startswith('/static')}
    assert non_static  # non-empty
    assert all(p.startswith(('/admin', '/api/admin')) for p in non_static)
    assert '/api/admin/people/dedupe-faces' in non_static
    assert '/api/admin/jobs/status' in non_static


def test_extras_role_serves_only_extras_routes():
    paths = _rule_paths_for_role('extras')
    non_static = {p for p in paths if not p.startswith('/static')}
    assert non_static  # non-empty
    extras_prefixes = ('/api/persons', '/api/faces', '/api/people', '/persons', '/people', '/api/library', '/public', '/api/public')
    assert all(p.startswith(extras_prefixes) for p in non_static)
    assert '/api/persons' in non_static
    assert '/api/library/mine' in non_static
    assert '/public/albums/<token>' in non_static
