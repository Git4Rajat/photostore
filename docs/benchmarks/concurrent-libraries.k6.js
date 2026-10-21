// API-only distributed load test. No media bytes, uploads, login storm, or AI.
// See scale-100k-users-20261021.md before running against a provisioned test environment.
import http from 'k6/http';
import { check, sleep } from 'k6';
import { SharedArray } from 'k6/data';
import exec from 'k6/execution';
import { Rate, Trend } from 'k6/metrics';

const base = (__ENV.BASE_URL || '').replace(/\/$/, '');
const target = Number(__ENV.VUS || 100);
const period = Number(__ENV.PERIOD_SECONDS || 10);
if (!base.startsWith('https://') || !__ENV.USERS_FILE) throw new Error('Set HTTPS BASE_URL and USERS_FILE');
if (!Number.isInteger(target) || target < 1 || !Number.isFinite(period) || period <= 0) throw new Error('Invalid VUS or PERIOD_SECONDS');
const users = new SharedArray('library sessions', () => {
  const entries = JSON.parse(open(__ENV.USERS_FILE));
  const libraries = new Set();
  for (const entry of entries) {
    if (!entry.libraryId || !entry.token || entry.photoCount !== 1000000 || libraries.has(entry.libraryId)) {
      throw new Error('Require unique libraryId, token, photoCount=1000000 for every identity');
    }
    libraries.add(entry.libraryId);
  }
  return entries;
});
if (users.length < target) throw new Error('Need at least one distinct million-photo library per VU');
const ready = new Rate('library_ready');
const galleryTime = new Trend('gallery_ready_ms', true);
const searchTime = new Trend('search_ready_ms', true);

export const options = {
  scenarios: { libraries: {
    executor: 'ramping-vus', startVUs: 0,
    stages: [
      { duration: '2m', target: Math.max(1, Math.ceil(target * .01)) },
      { duration: '3m', target: Math.max(1, Math.ceil(target * .1)) },
      { duration: '5m', target: Math.max(1, Math.ceil(target * .5)) },
      { duration: '5m', target },
      { duration: __ENV.HOLD || '30m', target },
      { duration: '2m', target: 0 },
    ], gracefulRampDown: '30s',
  } },
  thresholds: {
    http_req_failed: ['rate<0.001'],
    library_ready: ['rate>0.999'],
    gallery_ready_ms: ['p(95)<500', 'p(99)<1500'],
    search_ready_ms: ['p(95)<2000', 'p(99)<5000'],
    checks: ['rate>0.999'],
  },
};

let initialized = false;
export default function () {
  const user = users[exec.vu.idInTest - 1];
  if (!user) throw new Error('Distributed VU ID exceeds identity file; expand file for allocated ID range');
  if (!initialized) sleep(Math.random() * period);
  const began = Date.now();
  const isSearch = initialized && Math.random() < .1;
  const sort = Math.random() < .05 ? 'name' : 'capture';
  // Vary offsets to expose deep pagination; filenames/tokens never enter metric tags.
  const offset = Math.random() < .8 ? 0 : Math.floor(Math.random() * 999976);
  const query = ['beach', 'dog', 'city', 'mountain', 'travel'][Math.floor(Math.random() * 5)];
  const route = isSearch
    ? `/api/photos/search?q=${query}&limit=24`
    : `/api/photos?sort=${sort}&offset=${offset}&limit=24`;
  const response = http.get(base + route, {
    headers: { Authorization: `Bearer ${user.token}` },
    tags: { name: isSearch ? 'search' : 'gallery' }, timeout: '30s',
  });
  let body;
  try { body = response.json(); } catch (_) { body = null; }
  const available = response.status === 200 && body && Array.isArray(body.photos)
    && !body.indexBuilding && !body.indexPartial && !body.searchIndexBuilding
    && (isSearch || (body.total === 1000000 && body.photos.length > 0));
  ready.add(Boolean(available));
  check(response, { 'ready million-photo library response': () => Boolean(available) });
  if (available) (isSearch ? searchTime : galleryTime).add(response.timings.duration);
  initialized = true;
  // Start-to-start pacing approaches 1 request/period; slow responses lower
  // achieved RPS. Report that drop; this closed-loop test cannot certify fixed RPS.
  sleep(Math.max(0, period - (Date.now() - began) / 1000));
}
