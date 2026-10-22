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
