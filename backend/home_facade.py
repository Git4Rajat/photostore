"""The always-on front door: a data-free page for the storage static website root.

It shows the app's chrome and a skeleton grid within a fraction of a second -- before any container is
awake -- while it wakes the web app and every API the app talks to (read from the web app's own env.js),
then hands over to the real app. It holds no account or library data, so it is safe on a public site; the
real app (and its login) stays on its own origin, so nobody is logged out. Optional ``?to=/path`` keeps a
deep link. A page-level CSP (meta) allows only its own inline script/style by hash plus the web origin's env.js.
"""
from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import urlsplit

_CSS = """
:root{color-scheme:light dark;--bg:#f7f4ef;--fg:#1a1a1a;--mut:#6a6a6a;--tile:#e6e2da;--hi:#f1eee8;--acc:#1e6ae1}
@media (prefers-color-scheme:dark){:root{--bg:#0c0c0e;--fg:#f4f4f6;--mut:#a0a0aa;--tile:#1b1b20;--hi:#25252b;--acc:#4da2ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:20px;padding:12px 16px}
.brand{font-weight:700;font-size:20px;letter-spacing:-.01em}
nav{display:flex;gap:16px;color:var(--mut);font-size:14px;overflow:hidden;white-space:nowrap}
main{padding:8px 16px 40px;max-width:1400px;margin:0 auto}
h1{margin:12px 0 4px;font-size:24px}
.sub{display:flex;align-items:center;gap:10px;margin:0 0 16px;color:var(--mut)}
.spin{width:16px;height:16px;border-radius:50%;border:2px solid rgba(30,106,225,.2);border-top-color:var(--acc);animation:r .8s linear infinite;flex:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:6px}
.t{aspect-ratio:1;border-radius:6px;background:linear-gradient(100deg,var(--tile) 30%,var(--hi) 50%,var(--tile) 70%);background-size:200% 100%;animation:s 1.4s ease-in-out infinite}
@keyframes r{to{transform:rotate(360deg)}}
@keyframes s{to{background-position:-200% 0}}
@media (prefers-reduced-motion:reduce){.spin,.t{animation:none}}
"""

_JS = """
(function () {
  var SPA = __SPA__;
  var to = '/';
  try {
    var p = new URLSearchParams(location.search).get('to');
    if (p && p.charAt(0) === '/' && p.charAt(1) !== '/' && p.indexOf('\\\\') < 0) to = p;
  } catch (e) {}
  var done = false;
  function go() { if (done) return; done = true; location.replace(SPA + to); }
  function ping(url) {
    return new Promise(function (resolve) {
      var tries = 0;
      (function attempt() {
        tries += 1;
        fetch(url.replace(/\\/$/, '') + '/health', { mode: 'no-cors', cache: 'no-store' })
          .then(resolve, function () { if (tries < 6) setTimeout(attempt, 1500); else resolve(); });
      }());
    });
  }
  var web = ping(SPA);
  // Wake every backend the app uses, not just the web server; their addresses live in its env.js.
  var s = document.createElement('script');
  s.src = SPA + '/env.js';
  s.onload = function () {
    var c = window.__APP_CONFIG__ || {};
    var seen = {};
    [c.apiBaseUrl, c.archiveApiBaseUrl, c.uploadBaseUrl, c.indexerApiBaseUrl, c.recoveryApiBaseUrl].forEach(function (u) {
      if (u && /^https:\\/\\//.test(u) && !seen[u]) { seen[u] = 1; ping(u); }
    });
  };
  document.head.appendChild(s);
  // Hand over as soon as the web app answers; never wait on the (slower) APIs, the app covers that itself.
  web.then(go);
  setTimeout(go, 20000);
}());
"""


def _origin(url: str) -> str:
    parts = urlsplit(url or '')
    return f'{parts.scheme}://{parts.netloc}' if parts.scheme == 'https' and parts.netloc else ''


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode('ascii') + "'"


def render(spa_url: str) -> str:
    spa = _origin(spa_url)
    script = _JS.replace('__SPA__', json.dumps(spa).replace('</', '<\\/'))
    csp = '; '.join([
        "default-src 'none'",
        f"script-src {_hash(script)} {spa}",
        f"style-src {_hash(_CSS)}",
        "connect-src https:",
        "base-uri 'none'",
        "form-action 'none'",
    ])
    tiles = '<div class="t"></div>' * 48
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
        '<meta name="robots" content="noindex" /><meta name="theme-color" content="#1e6ae1" />'
        '<title>Keepsake</title>'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}" />'
        f'<style>{_CSS}</style></head><body>'
        '<header><span class="brand">Keepsake</span><nav aria-hidden="true"><span>Ask</span><span>Gallery</span><span>Albums</span>'
        '<span>People</span><span>Explore</span><span>Tools</span></nav></header>'
        '<main><h1>Gallery</h1><p class="sub" role="status"><span class="spin" aria-hidden="true"></span>Opening your library…</p>'
        f'<div class="grid" aria-hidden="true">{tiles}</div>'
        f'<noscript><p>Turn on JavaScript, or <a href="{spa}/">open Keepsake</a>.</p></noscript></main>'
        f'<script>{script}</script></body></html>'
    )
