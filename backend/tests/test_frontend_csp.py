"""Exercise the real runtime CSP assignments without writing nginx files."""
from pathlib import Path
import os
import subprocess
import pytest


@pytest.mark.parametrize('api_url', ['', 'https://production-api.example'])
def test_runtime_csp_keeps_webpubsub_origin_with_api_override(api_url):
    source = (Path(__file__).resolve().parents[2] / 'frontend/docker-entrypoint.sh').read_text()
    assignments = source[source.index('CONNECT_SRC='):source.index('\nIMG_SRC=')]
    result = subprocess.run(['bash', '-c', assignments + '\nprintf "%s" "$CONNECT_SRC"'],
                            env={**os.environ, 'API_BASE_URL': api_url}, capture_output=True, text=True, check=True)
    origins = result.stdout.split()
    assert 'wss://*.webpubsub.azure.com' in origins
    assert "'self'" in origins
    assert 'https://*.blob.core.windows.net' in origins
    if api_url:
        assert api_url in origins


def test_blob_host_selection_survives_set_u_when_storage_account_is_unset():
    # The entrypoint runs under `set -eu`; an unset STORAGE_ACCOUNT_NAME must fall
    # back to the wildcard instead of aborting container start.
    source = (Path(__file__).resolve().parents[2] / 'frontend/docker-entrypoint.sh').read_text()
    snippet = source[source.index('if [ -n "${STORAGE_ACCOUNT_NAME'):source.index('UNPKG_SRC=')]
    env = {k: v for k, v in os.environ.items() if k != 'STORAGE_ACCOUNT_NAME'}
    unset = subprocess.run(['bash', '-c', 'set -eu\n' + snippet + '\nprintf "%s" "$BLOB_HOST"'],
                           env=env, capture_output=True, text=True, check=True)
    assert unset.stdout == 'https://*.blob.core.windows.net'
    scoped = subprocess.run(['bash', '-c', 'set -eu\n' + snippet + '\nprintf "%s" "$BLOB_HOST"'],
                            env={**env, 'STORAGE_ACCOUNT_NAME': 'acct1'}, capture_output=True, text=True, check=True)
    assert scoped.stdout == 'https://acct1.blob.core.windows.net'
