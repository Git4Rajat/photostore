"""app.py's configure_storage(...) call must match storage_utils' real signature.

Production incident 2026-10-24: a new keyword argument was added to app.py's
call site without adding the matching parameter to configure_storage() (which
takes an explicit keyword-only parameter list, not **kwargs). The call raised
TypeError on every single request/worker startup, and app.py's bare
`except Exception: log and continue` around _init_storage_clients() silently
swallowed it -- storage_utils._CTX was never populated, in production, for
the life of the deployment, with no crash to signal it. This static check
doesn't need to import or run app.py (which has heavy module-level side
effects): it just verifies every keyword actually used in the real call site
is one configure_storage() actually accepts.
"""
import ast
import inspect
from pathlib import Path

import storage_utils

APP_PY = Path(__file__).resolve().parents[1] / 'app.py'


def _configure_storage_call_keywords():
    tree = ast.parse(APP_PY.read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'configure_storage'):
            return {kw.arg for kw in node.keywords if kw.arg is not None}
    raise AssertionError('No configure_storage(...) call found in app.py')


def test_app_configure_storage_call_matches_the_real_signature():
    used = _configure_storage_call_keywords()
    accepted = set(inspect.signature(storage_utils.configure_storage).parameters)
    unknown = used - accepted
    assert not unknown, f'app.py passes keyword(s) configure_storage() does not accept: {unknown}'


def test_configure_storage_requires_no_new_mandatory_parameter_app_py_omits():
    """A newly-added required parameter would break the same call the same
    way -- fail loud here instead of at runtime in production."""
    used = _configure_storage_call_keywords()
    required = {
        name for name, param in inspect.signature(storage_utils.configure_storage).parameters.items()
        if param.default is inspect.Parameter.empty
    }
    missing = required - used
    assert not missing, f'app.py omits required configure_storage() parameter(s): {missing}'
