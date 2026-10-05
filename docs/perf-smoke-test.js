/*
 * Photostore browser smoke + performance test.
 *
 * HOW TO RUN
 *   1. Open the app in the browser and sign in.
 *   2. DevTools -> Console. Paste this whole file, press Enter.
 *      (Chrome may ask you to type "allow pasting" first.)
 *   3. Wait for "DONE" (about 1-3 minutes on a 130k-photo library).
 *   4. Everything is also returned as `window.__smoke` (JSON). Run
 *        copy(JSON.stringify(window.__smoke, null, 1))
 *      to put the full result on the clipboard, and paste it to Claude.
 *
 * WHAT IT DOES
 *   READ-ONLY by default. It calls every list/read API the app uses, downloads the
 *   three client indexes the way the app does, loads real thumbnails with the media
 *   token, then runs concurrency and repeat-call probes. It never deletes, renames,
 *   uploads, labels, merges or creates anything unless you turn on the OPT-IN
 *   section below.
 *
 *   Each call sends X-Request-ID "smoke-N" so you can find it in Log Analytics
 *   (`PERF event=request ... rid=smoke-N`), next to the browser-side numbers.
 *
 * OPTIONS (edit before pasting if you want)
 */
(async () => {
  const OPTIONS = {
    thumbnails: 60,        // how many real thumbnails to download via the media token
    searchTerms: ['dog', 'beach', 'birthday 2022', 'zzzzqq'],  // last one should return nothing
    slowMs: 1500,          // flag anything slower than this
    concurrency: 8,        // parallel requests in the queueing probe
    // ---- OPT-IN, writes data. Leave false for a safe run. ----
    smartAlbum: false,     // POST /api/albums/autocreate (creates ONE album if a new group exists)
    manualToken: '',       // paste a bearer token here only if auto-detection fails (Entra login)
  };

  const cfg = window.__APP_CONFIG__ || {};
  const origin = location.origin;
  const base = (v) => (v || '').replace(/\/$/, '') || origin;
  const API = base(cfg.apiBaseUrl), EXTRAS = base(cfg.extrasApiBaseUrl || cfg.apiBaseUrl),
        TOOLS = base(cfg.toolsApiBaseUrl || cfg.apiBaseUrl);

  const token = OPTIONS.manualToken || localStorage.getItem('photostore.passwordAuthToken') || '';
  if (!token) console.warn('No password-auth token found in localStorage; if the app uses Microsoft login, set OPTIONS.manualToken.');
  const session = Math.random().toString(36).slice(2, 8);
  let counter = 0;
  const results = [];
  const note = (msg) => console.log('%c' + msg, 'color:#08f');

  const parseTiming = (h) => {
    const out = {};
    (h || '').split(',').forEach((p) => {
      const [n, ...rest] = p.trim().split(';');
      const d = rest.find((x) => x.trim().startsWith('dur='));
      if (n && d) out[n] = Number(d.trim().slice(4));
    });
    return out;
  };

  // One measured call. Never throws.
  async function call(name, method, url, body, extra = {}) {
    const rid = `smoke-${session}-${++counter}`;
    const headers = { 'X-Request-ID': rid, 'X-Client-View': 'smoke-test', 'X-Client-Session': session };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const t0 = performance.now();
    let res, text = '', error = '', attempts = 0;
    // Scale-to-zero apps answer the first request with a gateway error (no CORS headers, so the
    // browser reports "Failed to fetch"). Retry those like the app does, and report how many tries.
    for (; attempts < 4; attempts++) {
      error = ''; res = undefined; text = '';
      try {
        res = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, cache: 'no-store' });
        text = await res.text();
      } catch (e) { error = String(e); }
      const transient = !res || res.status === 502 || res.status === 503 || res.status === 504;
      if (!transient || attempts === 3) { attempts++; break; }
      await new Promise((r) => setTimeout(r, 1500 * (attempts + 1)));
    }
    const ms = performance.now() - t0;
    let json = null;
    try { json = text ? JSON.parse(text) : null; } catch (e) { /* not json */ }
    const timing = parseTiming(res && res.headers.get('server-timing'));
    const row = {
      name, method, path: url.replace(/^https?:\/\/[^/]+/, '').split('?')[0], status: res ? res.status : 0,
      ms: Math.round(ms), serverMs: timing.app, storageMs: timing.storage,
      kb: Math.round(text.length / 1024), rid, attempts,
      error: error || (res && !res.ok ? (json && json.error) || res.statusText : ''),
      ...extra,
    };
    results.push(row);
    return { ok: !!res && res.ok, json, row };
  }

  // The browser usually gunzips Content-Encoding:gzip blobs itself; only decompress when the
  // bytes still start with the gzip magic number (1f 8b).
  async function bufferToText(buf) {
    const u8 = new Uint8Array(buf);
    if (u8[0] === 0x1f && u8[1] === 0x8b) {
      return await new Response(new Blob([buf]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
    }
    return new TextDecoder().decode(buf);
  }

  async function blobJson(name, url) {
    const t0 = performance.now();
    let rows = null, kb = 0, parseMs = 0, error = '';
    try {
      const r = await fetch(url, { cache: 'no-store' });
      const buf = await r.arrayBuffer();
      kb = Math.round(buf.byteLength / 1024);
      const t1 = performance.now();
      const text = await bufferToText(buf);
      rows = (JSON.parse(text).rows || []).length;
      parseMs = Math.round(performance.now() - t1);
    } catch (e) { error = String(e); }
    results.push({ name, method: 'GET', path: '(blob) ' + url.replace(/^https?:\/\/([^/]+)\/([^/?]+).*/, '$1/$2'),
      status: error ? 0 : 200, ms: Math.round(performance.now() - t0), kb, rows, parseMs, error });
    return rows;
  }

  note('0/9 warm-up (scale-to-zero apps may take ~30 s to start; reported separately)');
  for (const [label, host] of [['backend', API], ['extras', EXTRAS], ['tools', TOOLS]]) {
    if (label !== 'backend' && host === API) continue;
    await call(`WARM-UP ${label} /health`, 'GET', `${host}/health`);
  }
  note('1/9 health and tokens');
  await call('health', 'GET', `${API}/health`);
  const mt = await call('media-token', 'GET', `${API}/api/photos/media-token`);
  const media = mt.json || {};

  note('2/9 client indexes (what the app downloads at session start)');
  const sortM = await call('sort-index manifest', 'GET', `${API}/api/photos/sort-index`);
  if (sortM.json && sortM.json.indexUrl) await blobJson('sort-index blob', sortM.json.indexUrl);
  const albM = await call('albums-index manifest', 'GET', `${API}/api/albums/index`);
  if (albM.json && albM.json.indexUrl) await blobJson('albums-index blob', albM.json.indexUrl);
  const pplM = await call('people-index manifest', 'GET', `${EXTRAS}/api/persons/index`);
  if (pplM.json && pplM.json.indexUrl) await blobJson('people-index blob', pplM.json.indexUrl);
  await call('index-status', 'GET', `${API}/api/photos/index-status`);
  await call('tools index status', 'GET', `${TOOLS}/api/tools/indexes/status`);

  note('3/9 gallery pages, filters, timeline, explore');
  const p1 = await call('photos page 1 (48)', 'GET', `${API}/api/photos?sort=capture&limit=48&offset=0&directMedia=1`);
  await call('photos page 2 (48)', 'GET', `${API}/api/photos?sort=capture&limit=48&offset=48&directMedia=1`);
  await call('photos page 100 (deep)', 'GET', `${API}/api/photos?sort=capture&limit=48&offset=4800&directMedia=1`);
  await call('photos sort=upload', 'GET', `${API}/api/photos?sort=upload&limit=48&offset=0&directMedia=1`);
  await call('photos sort=rating', 'GET', `${API}/api/photos?sort=rating&limit=48&offset=0&directMedia=1`);
  await call('photos filter rating>=3', 'GET', `${API}/api/photos/filter?minRating=3&limit=48&offset=0`);
  await call('timeline', 'GET', `${API}/api/photos/timeline`);
  await call('explore', 'GET', `${API}/api/explore`);
  await call('processing-status', 'GET', `${API}/api/photos/processing-status`);

  const photos = ((p1.json && p1.json.photos) || []);
  const names = photos.map((p) => p.filename).filter(Boolean);

  note('4/9 search');
  for (const q of OPTIONS.searchTerms) {
    await call(`search "${q}"`, 'GET', `${API}/api/photos/search?q=${encodeURIComponent(q)}&limit=24&offset=0`);
  }
  await call('search "dog" page 2', 'GET', `${API}/api/photos/search?q=dog&limit=24&offset=24`);

  note('5/9 photo detail and batch lookups');
  if (names.length) {
    await call('metadata (1 photo)', 'GET', `${API}/api/photos/${encodeURIComponent(names[0])}/metadata`);
    await call('lookup-batch (24)', 'POST', `${API}/api/photos/lookup-batch`, { filenames: names.slice(0, 24) });
    await call('access-batch thumbnails (24)', 'POST', `${API}/api/photos/access-batch`, { kind: 'thumbnail', filenames: names.slice(0, 24) });
    await call('access-batch preview (6)', 'POST', `${API}/api/photos/access-batch`, { kind: 'preview', filenames: names.slice(0, 6) });
  } else note('  (no photos returned; skipping per-photo calls)');

  note('6/9 albums');
  const al = await call('albums list', 'GET', `${API}/api/albums`);
  const albums = (al.json && al.json.albums) || [];
  if (albums[0]) await call('album detail (first)', 'GET', `${API}/api/albums/${encodeURIComponent(albums[0].albumId || albums[0].id)}`);
  await call('albums trash', 'GET', `${API}/api/albums/trash`);

  note('7/9 people');
  const roster = await call('people roster (names+covers)', 'GET', `${EXTRAS}/api/persons?namesOnly=1&covers=1&limit=100000`);
  const people = (roster.json && (roster.json.persons || roster.json.people)) || [];
  await call('people page (15)', 'GET', `${EXTRAS}/api/persons?limit=15&offset=0`);
  await call('people suggestions', 'GET', `${EXTRAS}/api/persons/suggestions`);
  await call('people merges', 'GET', `${EXTRAS}/api/persons/merges`);
  const first = people[0];
  if (first) {
    const pid = first.personId || first.id;
    await call('person detail (first)', 'GET', `${EXTRAS}/api/persons/${encodeURIComponent(pid)}`);
    const cover = first.coverFaceId;
    if (cover) await call('face crop (cover)', 'GET', `${EXTRAS}/api/faces/crop/${encodeURIComponent(cover)}`);
    // 12 person avatars at once, like the People grid
    const covers = people.map((p) => p.coverFaceId).filter(Boolean).slice(0, 12);
    const t0 = performance.now();
    await Promise.all(covers.map((c, i) => call(`face crop #${i + 1} (parallel)`, 'GET', `${EXTRAS}/api/faces/crop/${encodeURIComponent(c)}`)));
    results.push({ name: 'face crops: 12 in parallel (wall)', method: 'GET', path: '(group)', status: 200, ms: Math.round(performance.now() - t0) });
  }

  note('8/9 account, jobs, tools');
  await call('jobs status', 'GET', `${TOOLS}/api/jobs/status`);
  await call('workbench actions', 'GET', `${TOOLS}/api/tools/workbench/actions`);
  await call('trash list', 'GET', `${API}/api/photos/trash?limit=50`);
  await call('library mine', 'GET', `${EXTRAS}/api/library/mine`);
  await call('library members', 'GET', `${EXTRAS}/api/library/members`);
  await call('library cleanup-info', 'GET', `${EXTRAS}/api/library/cleanup-info`);
  if (OPTIONS.smartAlbum) {
    await call('SMART ALBUM autocreate (writes!)', 'POST', `${API}/api/albums/autocreate`, { rule: 'recent-upload' });
  }

  note('9/9 thumbnails, concurrency and repeat probes');
  // Thumbnails the way the gallery loads them: container token + sort-index blob name.
  let thumbStats = null;
  try {
    const sortRows = (sortM.json && sortM.json.indexUrl) ? await (async () => {
      const r = await fetch(sortM.json.indexUrl); const b = await r.arrayBuffer();
      const t = await bufferToText(b);
      return JSON.parse(t).rows || [];
    })() : [];
    const withThumb = sortRows.filter((r) => r.thumb).slice(0, OPTIONS.thumbnails);
    if (media.available && media.baseUrl && withThumb.length) {
      const enc = (s) => s.split('/').map(encodeURIComponent).join('/');
      const urls = withThumb.map((r) => `${media.baseUrl}/${enc(r.thumb)}?${media.sas}`);
      const timeOne = async (u) => { const t0 = performance.now(); const r = await fetch(u); const b = await r.arrayBuffer(); return { ms: performance.now() - t0, kb: b.byteLength / 1024, ok: r.ok }; };
      const cold0 = performance.now();
      const cold = [];
      for (let i = 0; i < urls.length; i += 6) cold.push(...await Promise.all(urls.slice(i, i + 6).map(timeOne)));
      const coldWall = performance.now() - cold0;
      const warm0 = performance.now();
      const warm = [];
      for (let i = 0; i < urls.length; i += 6) warm.push(...await Promise.all(urls.slice(i, i + 6).map(timeOne)));
      const warmWall = performance.now() - warm0;
      const avg = (a) => Math.round(a.reduce((s, x) => s + x.ms, 0) / Math.max(1, a.length));
      thumbStats = {
        count: urls.length, failed: cold.filter((x) => !x.ok).length,
        firstPassWallMs: Math.round(coldWall), firstPassAvgMs: avg(cold),
        secondPassWallMs: Math.round(warmWall), secondPassAvgMs: avg(warm),
        avgKb: Math.round(cold.reduce((s, x) => s + x.kb, 0) / Math.max(1, cold.length)),
        browserCacheWorking: warmWall < coldWall * 0.5,
      };
    } else thumbStats = { skipped: 'no media token or no thumbnails in the sort index' };
  } catch (e) { thumbStats = { error: String(e) }; }

  // Queueing probe: N identical requests at once. If these are much slower than a single call,
  // the server is queueing (too few worker threads, or something long is holding them).
  const single = results.find((r) => r.name === 'photos page 1 (48)');
  const t0 = performance.now();
  const burst = await Promise.all(Array.from({ length: OPTIONS.concurrency }, (_, i) =>
    call(`burst ${i + 1}/${OPTIONS.concurrency}: photos page`, 'GET', `${API}/api/photos?sort=capture&limit=48&offset=${i * 48}&directMedia=1`)));
  const burstWall = Math.round(performance.now() - t0);
  const burstMs = burst.map((b) => b.row.ms);

  // Repeat probe: same call 3x -> shows whether anything is cached.
  const rep = [];
  for (let i = 0; i < 3; i++) rep.push((await call(`repeat ${i + 1}: albums index manifest`, 'GET', `${API}/api/albums/index`)).row.ms);

  // ---------- report ----------
  const warm = results.filter((r) => r.name.startsWith('WARM-UP'));
  const measured = results.filter((r) => !r.name.startsWith('WARM-UP'));
  const bad = measured.filter((r) => r.status === 0 || r.status >= 400);
  const slow = measured.filter((r) => r.ms >= OPTIONS.slowMs).sort((a, b) => b.ms - a.ms);
  const summary = {
    when: new Date().toISOString(), session, apiBase: API, extrasBase: EXTRAS, toolsBase: TOOLS,
    calls: measured.length, failed: bad.length, slow: slow.length,
    coldStartMs: Object.fromEntries(warm.map((w) => [w.name, w.ms])),
    retriedCalls: measured.filter((r) => r.attempts > 1).map((r) => `${r.name} x${r.attempts}`),
    thumbnails: thumbStats,
    queueing: {
      singlePageMs: single && single.ms, burstOf: OPTIONS.concurrency, burstWallMs: burstWall,
      burstSlowestMs: Math.max(...burstMs), burstMedianMs: burstMs.sort((a, b) => a - b)[Math.floor(burstMs.length / 2)],
      verdict: Math.max(...burstMs) > 3 * ((single && single.ms) || 1) ? 'SERVER IS QUEUEING (parallel calls wait on each other)' : 'ok',
    },
    repeatAlbumsIndexMs: rep,
    resourceTimingHosts: (() => {
      const m = {};
      performance.getEntriesByType('resource').forEach((e) => { const h = new URL(e.name).host; (m[h] = m[h] || { n: 0, ms: 0 }); m[h].n++; m[h].ms += e.duration; });
      return Object.fromEntries(Object.entries(m).map(([h, v]) => [h, { requests: v.n, avgMs: Math.round(v.ms / v.n) }]));
    })(),
  };
  window.__smoke = { summary, results: measured, failed: bad, slowest: slow.slice(0, 15) };

  console.log('%c==== SMOKE TEST RESULT ====', 'font-weight:bold;font-size:14px');
  console.table(results.map(({ name, status, ms, serverMs, storageMs, kb, rows, parseMs, error }) =>
    ({ name, status, ms, serverMs, storageMs, kb, rows, parseMs, error })));
  console.log('%cFAILED / ERROR (' + bad.length + ')', 'color:red;font-weight:bold'); console.table(bad);
  console.log('%cSLOW >= ' + OPTIONS.slowMs + 'ms (' + slow.length + ')', 'color:orange;font-weight:bold');
  console.table(slow.map(({ name, ms, serverMs, storageMs, kb }) => ({ name, ms, serverMs, storageMs, kb })));
  console.log('Thumbnails:', thumbStats); console.log('Queueing probe:', summary.queueing);
  console.log('%cDONE. Run: copy(JSON.stringify(window.__smoke, null, 1))  and paste it to Claude.', 'color:green;font-weight:bold');
  if (window.photostorePerf) { console.log('App perf collector report:'); window.photostorePerf.report(); }
})();
