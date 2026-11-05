"""Boundary smoke tests for routes the rest of the suite never dispatches.

Two guarantees per route, with storage unconfigured and the network blocked:
  * an unauthenticated caller is rejected (or the route is deliberately public)
  * an authenticated caller sending an empty request gets a handled response,
    never an unhandled 500
They do not verify business behaviour; they keep wiring, guards and input
validation from silently regressing on the long tail of endpoints.
"""
import re
import socket

import pytest

import app as backend

# (method, rule) for every route whose view function no other test executes.
UNCOVERED_ROUTES = [
    ('POST', '/admin/ipwork/enqueue'),
    ('POST', '/admin/people/rebuild-photo-people-index'),
    ('POST', '/admin/people/recluster'),
    ('POST', '/admin/people/recluster/restore'),
    ('POST', '/admin/people/repair-stale-memberships'),
    ('POST', '/admin/people/suppress-suspicious-faces'),
    ('POST', '/admin/people/unblock-low-confidence-faces'),
    ('POST', '/admin/people/verify-face-index'),
    ('GET', '/albums'),
    ('POST', '/albums'),
    ('POST', '/albums/<album_id>/delete'),
    ('POST', '/albums/<album_id>/photos/remove'),
    ('POST', '/albums/<album_id>/purge'),
    ('POST', '/albums/<album_id>/restore'),
    ('GET', '/albums/trash'),
    ('POST', '/api/admin/ipwork/enqueue'),
    ('POST', '/api/admin/people/rebuild-photo-people-index'),
    ('POST', '/api/admin/people/recluster'),
    ('POST', '/api/admin/people/recluster/restore'),
    ('POST', '/api/admin/people/repair-stale-memberships'),
    ('POST', '/api/admin/people/suppress-suspicious-faces'),
    ('POST', '/api/admin/people/unblock-low-confidence-faces'),
    ('POST', '/api/admin/people/verify-face-index'),
    ('GET', '/api/albums'),
    ('POST', '/api/albums'),
    ('POST', '/api/albums/<album_id>/delete'),
    ('POST', '/api/albums/<album_id>/photos/remove'),
    ('POST', '/api/albums/<album_id>/purge'),
    ('POST', '/api/albums/<album_id>/restore'),
    ('GET', '/api/albums/trash'),
    ('POST', '/api/auth/change-password'),
    ('GET', '/api/auth/config'),
    ('POST', '/api/auth/exchange'),
    ('POST', '/api/auth/forgot'),
    ('POST', '/api/auth/login'),
    ('POST', '/api/auth/reset'),
    ('GET', '/api/faces/crop/<face_id>'),
    ('POST', '/api/faces/delete'),
    ('GET', '/api/geocode/reverse'),
    ('POST', '/api/library/clean/request'),
    ('GET', '/api/library/clean/status'),
    ('GET', '/api/library/cleanup-info'),
    ('GET', '/api/library/cleanup-info/'),
    ('POST', '/api/people/assign-unclustered'),
    ('GET', '/api/people/diagnostic'),
    ('POST', '/api/people/recluster'),
    ('GET', '/api/performance/throughput'),
    ('GET', '/api/performance/throughput/'),
    ('POST', '/api/persons/<person_id>/find-faces'),
    ('POST', '/api/persons/<person_id>/merge'),
    ('POST', '/api/persons/<person_id>/not-face'),
    ('POST', '/api/persons/<person_id>/separate'),
    ('POST', '/api/persons/<person_id>/suggested-faces/accept'),
    ('POST', '/api/persons/<person_id>/suggested-faces/decline'),
    ('POST', '/api/persons/cluster'),
    ('GET', '/api/persons/merges'),
    ('GET', '/api/photos/cover/<path:filename>'),
    ('GET', '/api/photos/image/<path:filename>'),
    ('GET', '/api/photos/lookup/<path:filename>'),
    ('GET', '/api/photos/lookup/<path:filename>/'),
    ('POST', '/api/photos/metadata'),
    ('POST', '/api/photos/metadata/'),
    ('GET', '/api/photos/preview/<path:filename>'),
    ('GET', '/api/photos/processing-status'),
    ('GET', '/api/photos/processing-status/'),
    ('GET', '/api/photos/raw-full-preview/<path:filename>'),
    ('GET', '/api/photos/timeline'),
    ('GET', '/api/photos/timeline/'),
    ('POST', '/api/photos/trash/purge'),
    ('POST', '/api/photos/trash/restore'),
    ('GET', '/api/search/suggest'),
    ('GET', '/api/search/suggest/'),
    ('GET', '/api/suggestions'),
    ('GET', '/api/suggestions/'),
    ('POST', '/api/tools/cluster/flush'),
    ('POST', '/api/tools/indexes/maintenance/<scope>'),
    ('POST', '/api/tools/ipwork/release'),
    ('GET', '/api/tools/workbench/actions'),
    ('POST', '/api/tools/workbench/actions'),
    ('GET', '/api/tools/workbench/actions/<action_id>'),
    ('POST', '/api/upload/cancel'),
    ('POST', '/api/upload/cancel/'),
    ('POST', '/api/upload/processing/claim'),
    ('POST', '/api/upload/processing/claim/'),
    ('POST', '/api/upload/processing/heartbeat'),
    ('POST', '/api/upload/processing/heartbeat/'),
    ('GET', '/api/upload/processing/pending'),
    ('GET', '/api/upload/processing/pending/'),
    ('POST', '/api/upload/processing/release'),
    ('POST', '/api/upload/processing/release/'),
    ('GET', '/api/upload/processing/status'),
    ('GET', '/api/upload/processing/status/'),
    ('POST', '/api/uploads/corrupted/<path:filename>/clear'),
    ('POST', '/auth/change-password'),
    ('GET', '/auth/config'),
    ('POST', '/auth/exchange'),
    ('POST', '/auth/forgot'),
    ('POST', '/auth/login'),
    ('POST', '/auth/reset'),
    ('GET', '/geocode/reverse'),
    ('GET', '/health'),
    ('POST', '/people/assign-unclustered'),
    ('POST', '/people/recluster'),
    ('GET', '/performance/throughput'),
    ('GET', '/performance/throughput/'),
    ('POST', '/persons/<person_id>/not-face'),
    ('GET', '/photos/lookup/<path:filename>'),
    ('GET', '/photos/lookup/<path:filename>/'),
    ('POST', '/photos/metadata'),
    ('POST', '/photos/metadata/'),
    ('GET', '/photos/processing-status'),
    ('GET', '/photos/processing-status/'),
    ('GET', '/photos/timeline'),
    ('GET', '/photos/timeline/'),
    ('POST', '/photos/trash/purge'),
    ('POST', '/photos/trash/restore'),
    ('POST', '/public/albums/<token>/download'),
    ('GET', '/public/albums/<token>/download-check'),
    ('GET', '/public/photos/<token>/image/<path:filename>'),
    ('GET', '/public/photos/<token>/preview/<path:filename>'),
    ('GET', '/public/photos/<token>/thumbnail/<path:filename>'),
    ('GET', '/upload/benchmark/config'),
    ('POST', '/upload/benchmark/verify'),
    ('POST', '/upload/cancel'),
    ('POST', '/upload/cancel/'),
    ('POST', '/upload/processing/claim'),
    ('POST', '/upload/processing/claim/'),
    ('POST', '/upload/processing/heartbeat'),
    ('POST', '/upload/processing/heartbeat/'),
    ('GET', '/upload/processing/pending'),
    ('GET', '/upload/processing/pending/'),
    ('POST', '/upload/processing/release'),
    ('POST', '/upload/processing/release/'),
    ('GET', '/upload/processing/status'),
    ('GET', '/upload/processing/status/'),
    ('GET', '/upload/waves/live/<wave>/failures'),
    ('POST', '/upload/waves/live/<wave>/repair'),
    ('POST', '/uploads/corrupted/<path:filename>/clear'),
]

# Reachable without a session by design (login flows, shared links, probes).
PUBLIC_PREFIXES = ('/auth/', '/api/auth/', '/public/', '/api/public/', '/health', '/upload/benchmark/')

# Served to anonymous callers today. It returns only aggregate upload counters (no ids), but whether
# that is intended is an open product/security decision -- keep the test honest rather than hiding it.
ANONYMOUS_TODAY = ('/performance/throughput', '/api/performance/throughput')

HANDLED = {200, 201, 202, 204, 301, 302, 400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429, 503}


def _concrete(rule):
    return re.sub(r'<(?:[a-z]+:)?([a-z_]+)>', lambda m: 'x1.jpg' if m.group(1) == 'filename' else 'x1', rule)


def _is_public(rule):
    return rule.startswith(PUBLIC_PREFIXES)


@pytest.fixture
def client(monkeypatch):
    def no_network(*args, **kwargs):
        raise OSError('network disabled in route smoke tests')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    backend.app.config['PROPAGATE_EXCEPTIONS'] = False
    backend.app.config['TESTING'] = False
    return backend.app.test_client()


@pytest.mark.parametrize('method,rule', UNCOVERED_ROUTES)
def test_unauthenticated_request_is_rejected_not_crashed(client, monkeypatch, method, rule):
    monkeypatch.setattr(backend, 'AUTH_MODE', 'entra')
    response = client.open(_concrete(rule), method=method, json={}, follow_redirects=True)
    assert response.status_code in HANDLED, f'{method} {rule} -> {response.status_code}'
    if not _is_public(rule) and not rule.rstrip('/').startswith(ANONYMOUS_TODAY):
        assert response.status_code in {401, 403, 404, 405, 503}, f'{method} {rule} served an anonymous caller ({response.status_code})'


@pytest.mark.parametrize('method,rule', UNCOVERED_ROUTES)
def test_authenticated_empty_request_is_handled(client, monkeypatch, method, rule):
    monkeypatch.setattr(backend, '_require_user_id', lambda require_auth=False: ('u1', None))
    monkeypatch.setattr(backend, '_require_admin', lambda require_auth=True: ('u1', None))
    monkeypatch.setattr(backend, '_require_library_context', lambda require_auth=False: ('u1', 'u1', None))
    response = client.open(_concrete(rule), method=method, json={}, follow_redirects=True)
    assert response.status_code in HANDLED, f'{method} {rule} -> {response.status_code}'
