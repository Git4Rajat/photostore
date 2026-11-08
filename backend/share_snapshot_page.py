"""The static page a shared-album snapshot serves from the storage static website.

It is deliberately framework-free and tiny so it paints in well under a second with no container
awake. Meanwhile it pings the web app and the archive API so they wake in parallel; once both
answer it hands over to the real app -- automatically for albums bigger than the cached window,
and only on an explicit action (or "Open full album") otherwise. It never takes over while a
photo is open. A page-level CSP (meta) allows only its own inline script/style by hash.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
from typing import Dict
from urllib.parse import urlsplit

_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0c0c0e;color:#f4f4f6;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:rgba(12,12,14,.88);backdrop-filter:blur(10px)}
.brand{font-weight:700;letter-spacing:.2px}
.full{color:#4da2ff;font-weight:600;text-decoration:none;font-size:14px}
main{padding:8px 16px 40px;max-width:1400px;margin:0 auto}
h1{margin:12px 0 4px;font-size:24px}
.sub{margin:0 0 12px;color:#b8b8c2}
.status{display:flex;align-items:center;gap:10px;color:#b8b8c2;margin:8px 0 16px;min-height:22px}
.status[hidden]{display:none}
.spin{width:16px;height:16px;border-radius:50%;border:2px solid rgba(77,162,255,.25);border-top-color:#4da2ff;animation:r .8s linear infinite;flex:none}
@keyframes r{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation-duration:2.4s}}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:4px}
.tile{position:relative;aspect-ratio:1;padding:0;border:0;background:#1a1a1f;overflow:hidden;cursor:pointer;border-radius:4px}
.tile img{width:100%;height:100%;object-fit:cover;display:block}
.badge{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.6);border-radius:4px;padding:1px 6px;font-size:11px}
.lb{position:fixed;inset:0;z-index:10;background:#000;display:flex;align-items:center;justify-content:center;touch-action:pan-y}
.lb[hidden]{display:none}
.lb img{max-width:100vw;max-height:100vh;object-fit:contain;display:block}
.lb button{position:absolute;background:rgba(30,30,36,.75);color:#fff;border:0;border-radius:50%;width:44px;height:44px;font-size:22px;cursor:pointer}
.lb .x{top:12px;right:12px}.lb .p{left:12px;top:50%;margin-top:-22px}.lb .n{right:12px;top:50%;margin-top:-22px}
.lb .cap{position:absolute;left:0;right:0;bottom:0;padding:12px 16px;background:linear-gradient(transparent,rgba(0,0,0,.7));text-align:center;font-size:13px}
.lb .cap a{color:#4da2ff;font-weight:600;text-decoration:none}
.lb .dl{position:absolute;top:12px;right:68px;width:auto;height:44px;border-radius:22px;padding:0 16px;font-size:14px;display:flex;align-items:center;color:#fff;text-decoration:none;background:rgba(30,30,36,.75)}
.lb .dl[hidden]{display:none}
.msg{padding:48px 0;text-align:center;color:#b8b8c2}
"""

_JS = """
(function () {
  var C = __CONFIG__;
  var $ = function (id) { return document.getElementById(id); };
  var manifest = null, shown = [], current = -1, ready = false, taken = false;

  function fullUrl(i) {
    var u = C.spa + '/public/album/' + encodeURIComponent(C.token);
    return i >= 0 ? u + '?photo=' + i : u;
  }
  function originalUrl(name) {
    return C.archive + '/public/photos/' + encodeURIComponent(C.token) + '/image/' +
      name.split('/').map(encodeURIComponent).join('/');
  }
  function takeOver(i) {
    if (taken) return;
    taken = true;
    location.replace(fullUrl(i));
  }
  function maybeTakeOver() {
    if (ready && manifest && !taken && current < 0 && manifest.total > manifest.cached) takeOver(-1);
  }

  function ping(url) {
    var tries = 0;
    return new Promise(function (resolve) {
      (function attempt() {
        tries += 1;
        var ctl = window.AbortController ? new AbortController() : null;
        var timer = setTimeout(function () { if (ctl) ctl.abort(); }, 90000);
        fetch(url, { mode: 'no-cors', cache: 'no-store', signal: ctl ? ctl.signal : undefined })
          .then(function () { clearTimeout(timer); resolve(); })
          .catch(function () {
            clearTimeout(timer);
            if (tries < 8) setTimeout(attempt, 1500); else resolve();
          });
      }());
    });
  }

  function tileRotation(p) {
    var rem = (((p.rot || 0) - (p.trot || 0)) % 360 + 360) % 360;
    return rem ? 'rotate(' + rem + 'deg) scale(' + (rem % 180 === 0 ? 1 : 0.74) + ')' : '';
  }

  function render() {
    $('title').textContent = manifest.name || 'Shared album';
    $('sub').textContent = manifest.total + ' photo' + (manifest.total === 1 ? '' : 's') + ' \\u00b7 Read-only';
    var more = manifest.total > manifest.cached;
    $('statusText').textContent = more ? 'Loading all ' + manifest.total + ' photos\\u2026' : '';
    if (!more) $('status').hidden = true;
    var grid = $('grid');
    if (!manifest.photos.length && !more) {
      var e = document.createElement('div');
      e.className = 'msg';
      e.textContent = 'This album has no photos yet.';
      grid.appendChild(e);
    }
    manifest.photos.forEach(function (p, i) {
      if (!p.t) return;
      var b = document.createElement('button');
      b.className = 'tile';
      b.type = 'button';
      b.setAttribute('aria-label', 'Open photo ' + (i + 1));
      var img = document.createElement('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = '';
      img.onerror = function () { b.remove(); };
      img.src = p.t;
      var tr = tileRotation(p);
      if (tr) img.style.transform = tr;
      b.appendChild(img);
      if (p.video) {
        var badge = document.createElement('span');
        badge.className = 'badge';
        badge.textContent = 'Video';
        b.appendChild(badge);
      }
      b.addEventListener('click', function () { openLb(i); });
      grid.appendChild(b);
      shown.push(i);
    });
  }

  function openLb(i) {
    var p = manifest.photos[i];
    if (!p || !p.t) return;
    current = i;
    var lb = $('lb'), img = $('lbImg'), cap = $('lbCap');
    lb.hidden = false;
    var rot = (((p.rot || 0) % 360) + 360) % 360;
    img.style.transform = rot ? 'rotate(' + rot + 'deg)' : '';
    img.style.maxWidth = rot % 180 ? '100vh' : '100vw';
    img.style.maxHeight = rot % 180 ? '100vw' : '100vh';
    img.onerror = function () { if (p.t && img.getAttribute('src') !== p.t) img.src = p.t; };
    img.src = p.p || p.t;
    cap.textContent = '';
    if (p.video || !p.p) {
      cap.appendChild(document.createTextNode(p.video ? 'Videos play in the full album. ' : 'Full-size photo opens in the full album. '));
      var a = document.createElement('a');
      a.href = fullUrl(i);
      a.textContent = 'Open full album';
      a.addEventListener('click', function (e) { e.preventDefault(); takeOver(i); });
      cap.appendChild(a);
    } else {
      cap.textContent = (i + 1) + ' / ' + manifest.total;
    }
    [i - 1, i + 1].forEach(function (j) {
      var q = manifest.photos[j];
      if (q && q.p) (new Image()).src = q.p;
    });
    var dl = $('lbDl');
    dl.href = originalUrl(p.n);
    dl.hidden = false;
    $('lbClose').focus();
  }
  function step(d) {
    var at = shown.indexOf(current);
    var next = shown[at + d];
    if (next !== undefined) openLb(next);
  }
  function closeLb() {
    $('lb').hidden = true;
    current = -1;
    maybeTakeOver();
  }
  $('lbClose').addEventListener('click', closeLb);
  $('lbPrev').addEventListener('click', function () { step(-1); });
  $('lbNext').addEventListener('click', function () { step(1); });
  document.addEventListener('keydown', function (e) {
    if (current < 0) return;
    if (e.key === 'Escape') closeLb();
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowRight') step(1);
  });
  var sx = null;
  $('lb').addEventListener('touchstart', function (e) { sx = e.touches[0].clientX; }, { passive: true });
  $('lb').addEventListener('touchend', function (e) {
    if (sx === null) return;
    var dx = e.changedTouches[0].clientX - sx;
    sx = null;
    if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1);
  });
  $('dlAll').href = C.archive + '/public/albums/' + encodeURIComponent(C.token) + '/download';
  $('full').addEventListener('click', function (e) { e.preventDefault(); takeOver(current); });

  function legacy() {
  Promise.all([ping(C.spa + '/health'), ping(C.archive + '/health')]).then(function () {
    ready = true;
    maybeTakeOver();
  });
  fetch('manifest.json', { cache: 'no-cache' })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (m) {
      var exp = m.expiresAt ? Date.parse(m.expiresAt) : NaN;
      if (!isNaN(exp) && Date.now() > exp) {
        $('status').hidden = true;
        $('title').textContent = 'Shared album';
        $('sub').textContent = '';
        var d = document.createElement('div');
        d.className = 'msg';
        d.textContent = 'This shared album link has expired.';
        $('grid').appendChild(d);
        return;
      }
      if (m.building) { waitForBuild(m); return; }
      manifest = m;
      render();
      maybeTakeOver();
    })
    .catch(function () { takeOver(-1); });
  }

  // A just-shared album has a page but no photos yet: show what we know and poll until they are cached.
  // The real app takes over if it wakes first (total > cached), so the visitor never waits on the build.
  function waitForBuild(m) {
    $('title').textContent = m.name || 'Shared album';
    $('sub').textContent = m.total + ' photo' + (m.total === 1 ? '' : 's') + ' \\u00b7 Read-only';
    $('statusText').textContent = 'Preparing photos\\u2026';
    manifest = m;
    maybeTakeOver();
    var started = Date.now();
    var timer = setInterval(function () {
      if (taken) { clearInterval(timer); return; }
      if (Date.now() - started > 120000) { clearInterval(timer); takeOver(-1); return; }
      fetch('manifest.json', { cache: 'no-cache' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (next) {
          if (!next || next.building || taken) return;
          clearInterval(timer);
          manifest = next;
          render();
          maybeTakeOver();
        })
        .catch(function () {});
    }, 3000);
  }

  // Preferred view: the real shared-album UI, loaded from this static site (see ensure_app in
  // share_snapshot.py). If it is not published yet, or fails to load, the plain view above is used.
  function rich(a) {
    var base = new URL('./', location.href).href;
    window.__SNAPSHOT__ = { token: C.token, spa: C.spa, archive: C.archive, base: base };
    window.__APP_CONFIG__ = { apiBaseUrl: C.archive, uploadBaseUrl: C.archive, archiveApiBaseUrl: C.archive, spaBaseUrl: C.spa };
    var root = document.createElement('div');
    root.id = 'root';
    document.body.appendChild(root);
    (a.css || []).forEach(function (f) {
      var l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = '/app/' + a.version + '/' + f;
      document.head.appendChild(l);
    });
    var s = document.createElement('script');
    s.type = 'module';
    s.src = '/app/' + a.version + '/' + a.entry;
    s.onload = function () {
      document.querySelector('header').hidden = true;
      document.querySelector('main').hidden = true;
      var lb = document.getElementById('lb');
      if (lb) lb.remove();
    };
    s.onerror = function () { root.remove(); legacy(); };
    document.head.appendChild(s);
  }
  fetch('/app/current.json', { cache: 'no-cache' })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(rich)
    .catch(legacy);
}());
"""


def _origin(url: str) -> str:
    parts = urlsplit(url or '')
    return f'{parts.scheme}://{parts.netloc}' if parts.scheme and parts.netloc else ''


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode('ascii') + "'"


def _script_json(value: object) -> str:
    return json.dumps(value).replace('</', '<\\/')


def render(meta: Dict[str, str], *, token: str, spa_url: str, archive_url: str, image: str,
           image_is_fallback: bool = False) -> str:
    title = html.escape(meta.get('title') or 'Shared album')
    description = html.escape(meta.get('description') or '')
    image_attr = html.escape(image or '', quote=True)
    spa, archive = _origin(spa_url), _origin(archive_url)
    script = _JS.replace('__CONFIG__', _script_json({'token': token, 'spa': spa, 'archive': archive}))
    csp = '; '.join([
        "default-src 'none'",
        f"script-src 'self' {_hash(script)}",
        f"style-src 'self' {_hash(_CSS)}",
        "img-src 'self' data: blob: " + ' '.join(o for o in (spa, archive) if o),
        "media-src 'self' blob: " + ' '.join(o for o in (spa, archive) if o),
        "font-src 'self' data:",
        "connect-src 'self' " + ' '.join(o for o in (spa, archive) if o),
        "base-uri 'none'",
        "form-action 'none'",
    ])
    dims = ('<meta property="og:image:width" content="1200" /><meta property="og:image:height" content="630" />'
            if image_is_fallback else '')
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=True)}" />'
        '<meta name="referrer" content="no-referrer" /><meta name="robots" content="noindex" />'
        f'<title>{title}</title><meta name="description" content="{description}" />'
        '<meta property="og:type" content="website" /><meta property="og:site_name" content="Keepsake" />'
        f'<meta property="og:title" content="{title}" /><meta property="og:description" content="{description}" />'
        f'<meta property="og:image" content="{image_attr}" />{dims}'
        '<meta name="twitter:card" content="summary_large_image" />'
        f'<meta name="twitter:title" content="{title}" /><meta name="twitter:description" content="{description}" />'
        f'<meta name="twitter:image" content="{image_attr}" />'
        f'<style>{_CSS}</style></head><body>'
        '<header><span class="brand">Keepsake</span><span><a id="dlAll" class="full" href="#" target="_blank" rel="noopener">Download all</a> &nbsp; <a id="full" class="full" href="#">Open full album</a></span></header>'
        f'<main><h1 id="title">{title}</h1><p id="sub" class="sub">{description}</p>'
        '<div id="status" class="status"><span class="spin" aria-hidden="true"></span><span id="statusText">Opening shared album…</span></div>'
        '<div id="grid" class="grid"></div>'
        f'<noscript><p class="msg">Turn on JavaScript to view this album, or <a href="{html.escape(spa + "/public/album/" + token, quote=True)}">open the full album</a>.</p></noscript></main>'
        '<div id="lb" class="lb" role="dialog" aria-modal="true" aria-label="Photo viewer" hidden>'
        '<img id="lbImg" alt="" /><a id="lbDl" class="dl" href="#" target="_blank" rel="noopener" hidden>Download</a><button id="lbClose" class="x" type="button" aria-label="Close">×</button>'
        '<button id="lbPrev" class="p" type="button" aria-label="Previous">‹</button>'
        '<button id="lbNext" class="n" type="button" aria-label="Next">›</button>'
        '<div id="lbCap" class="cap"></div></div>'
        f'<script>{script}</script></body></html>'
    )
