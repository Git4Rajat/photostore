"""Attempt-local reasons for bounded merges that intentionally produce no index."""
from contextlib import contextmanager
from contextvars import ContextVar

import processing_diagnostics

_CURRENT = ContextVar('index_merge_diagnostics', default=None)


def was_deferred(kind):
    state = _CURRENT.get()
    return bool(state is not None and
                state['reasons'].get(kind, {}).get('outcome') == 'deferred')


@contextmanager
def capture(library, scope, revision):
    state = {'fields': dict(library_id=processing_diagnostics.file_id(library, ''),
                           scope=scope, intent_revision=revision,
                           operation_id=processing_diagnostics.operation_id(f'{library}:{scope}:{revision}')),
             'reasons': {}}
    token = _CURRENT.set(state)
    try:
        yield state['reasons']
    finally:
        _CURRENT.reset(token)


def record(kind, reason, *, error=None):
    """Return None without changing publisher contracts or logging source data.

    Genuine read failures remain distinct from expected readiness/capacity
    deferrals. Only an explicit deferral can suppress the generic merge error.
    """
    state = _CURRENT.get()
    outcome = 'failed' if error is not None else 'deferred'
    fields = dict(state['fields']) if state is not None else {}
    if state is not None:
        # A later deferral cannot hide an earlier error in this kind.
        previous = state['reasons'].get(kind)
        if previous is None or previous['outcome'] != 'failed':
            state['reasons'][kind] = dict(outcome=outcome, reason=reason)
    processing_diagnostics.emit('index_merge_readiness', kind=kind, reason=reason,
        outcome=outcome, error_type=type(error).__name__ if error is not None else None,
        **fields)
