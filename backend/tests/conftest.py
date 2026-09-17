"""Make the backend package importable from tests (backend/ is the root)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
