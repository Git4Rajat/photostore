import json
import time
from typing import Dict, List, Mapping

import jwt
import requests
from jwt.algorithms import RSAAlgorithm

_JWKS_CACHE: Dict[str, Dict[str, object]] = {}


def _auth_configured(tenant_id: str, client_id: str) -> bool:
    return bool(tenant_id and client_id)


def _jwks_url(tenant_id: str) -> str:
    return f'https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys'


def _issuer(tenant_id: str) -> str:
    return f'https://login.microsoftonline.com/{tenant_id}/v2.0'


def _accepted_issuers(tenant_id: str) -> List[str]:
    return [
        _issuer(tenant_id),
        f'https://sts.windows.net/{tenant_id}/',
    ]


def _accepted_audiences(api_audience: str, client_id: str) -> List[str]:
    values = [
        api_audience,
        client_id,
        f'api://{client_id}' if client_id else '',
    ]
    accepted = []
    for value in values:
        if value and value not in accepted:
            accepted.append(value)
    return accepted


def _get_jwks_keys(tenant_id: str) -> List[Dict]:
    now = time.time()
    cache_key = _jwks_url(tenant_id)
    cache_entry = _JWKS_CACHE.get(cache_key, {'fetched_at': 0.0, 'keys': []})
    cached = cache_entry.get('keys', [])
    fetched_at = float(cache_entry.get('fetched_at', 0.0) or 0.0)
    if cached and (now - fetched_at) < 900:
        return cached

    response = requests.get(cache_key, timeout=8)
    response.raise_for_status()
    keys = response.json().get('keys', [])
    if not isinstance(keys, list):
        keys = []

    _JWKS_CACHE[cache_key] = {
        'fetched_at': now,
        'keys': keys,
    }
    return keys


def validate_bearer_token(token: str, tenant_id: str, client_id: str, api_audience: str) -> Dict:
    if not _auth_configured(tenant_id, client_id):
        raise RuntimeError('Entra auth is not configured on the API.')

    unverified = jwt.get_unverified_header(token)
    token_kid = unverified.get('kid')
    if not token_kid:
        raise RuntimeError('Missing kid in token header.')

    keys = _get_jwks_keys(tenant_id)
    matching = next((key for key in keys if key.get('kid') == token_kid), None)
    if not matching:
        raise RuntimeError('No matching signing key found for token.')

    public_key = RSAAlgorithm.from_jwk(json.dumps(matching))
    audiences = _accepted_audiences(api_audience, client_id)
    if not audiences:
        raise RuntimeError('No accepted audiences configured for token validation.')

    payload = jwt.decode(
        token,
        public_key,
        algorithms=['RS256'],
        audience=audiences,
        issuer=_accepted_issuers(tenant_id),
        options={'require': ['exp', 'iat']},
    )
    return payload


def get_request_user_id(
    headers: Mapping[str, str],
    auth_required: bool,
    tenant_id: str,
    client_id: str,
    api_audience: str,
    trust_user_header: bool = False,
) -> str:
    auth_header = str(headers.get('Authorization', '') or '')
    if auth_header.lower().startswith('bearer '):
        token = auth_header.split(' ', 1)[1].strip()
        payload = validate_bearer_token(token, tenant_id, client_id, api_audience)
        user_id = str(payload.get('oid') or payload.get('sub') or payload.get('preferred_username') or '').strip()
        if user_id:
            return user_id
        raise RuntimeError('Token does not contain a usable user identifier claim.')

    if auth_required:
        raise RuntimeError('Authorization token is required.')

    # No validated token. Only fall back to the unauthenticated X-User-ID header when the
    # operator has explicitly opted in (local dev). Otherwise refuse to invent an identity,
    # so a spoofed header can never impersonate another user.
    if not trust_user_header:
        raise RuntimeError('Authorization token is required.')

    return str(headers.get('X-User-ID', 'anonymous'))
