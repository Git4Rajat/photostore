import './style.css';
import { getRuntimeConfig } from '../config/appConfig';
import { getToken, getEmailFromToken, hasValidSession } from '../services/passwordAuthClient';
import workerUrl from './sensor-worker.js?worker&url';
import { runCloudBenchmark } from './cloud-client';
import { summarize } from './results';

const element = id => document.getElementById(id);
const buttons = ['pilot', 'full', 'stop', 'download'].map(element);
const config = getRuntimeConfig();
const uploadBase = (config.uploadBaseUrl || config.apiBaseUrl || '').replace(/\/$/, '');
const EXPECTED_FILES = 1808;
const EXPECTED_BYTES = 53_250_868_221;
const report = { schemaVersion: 1, decoderPackage: '@colorhythm/libraw-wasm@1.1.1', runs: [], limitations: [
  'Upload/finalization and JPEG-generation success are not OCR, face, tag, or colour accuracy.',
  'This is the persistent-isolation finalization path, not normal AI-queuing finalization.',
  'Benchmark files do not replace normal library dedup-index entries.',
  'Retries count attempted body bytes; HTTP/TLS overhead and interrupted bytes are not measured.',
  'Serial arm order and network variability can affect wall-time comparisons.',
] };
let files = [];
let target;
let running = false;
let stopRequested = false;
let pilotPassed = false;
let pilotInventory = '';
let recent = [];
let progressTimer;

const describe = error => String(error?.message || error).replace(/https?:\/\/[^\s)]+/g, '[url]').slice(0, 300);
const status = text => { element('status').textContent = text; };
const inventoryKey = () => JSON.stringify(files.map(file => [file.name, file.size]));
const corpusMatches = () => files.length === EXPECTED_FILES && files.reduce((sum, file) => sum + file.size, 0) === EXPECTED_BYTES;
const controls = () => {
  buttons[0].disabled = running || !target || !files.length;
  buttons[1].disabled = running || !target || !pilotPassed || pilotInventory !== inventoryKey() || !corpusMatches();
  buttons[2].disabled = !running || stopRequested;
  buttons[3].disabled = !report.runs.length;
  element('photos').disabled = running;
};

function renderSummary() {
  element('summary').replaceChildren();
  for (const result of report.runs) {
    const summary = summarize(result);
    const row = document.createElement('tr');
    for (const value of [`${result.scope}: ${result.arm}`, summary.finalized, summary.failed, result.arm === 'derivatives' ? summary.generated : '-', summary.verified, `${(summary.wallMs / 1000).toFixed(1)} s`]) {
      const cell = document.createElement('td'); cell.textContent = String(value); row.append(cell);
    }
    element('summary').append(row);
  }
}

async function authenticated(path, payload, base = uploadBase) {
  const token = getToken();
  if (!token || !hasValidSession()) throw new Error('Your app session has expired. Sign in to Keepsake again.');
  const response = await fetch(base + path, { method: payload ? 'POST' : 'GET', headers: {
    Authorization: `Bearer ${token}`, ...(payload ? { 'Content-Type': 'application/json' } : {}),
  }, ...(payload ? { body: JSON.stringify(payload) } : {}), signal: AbortSignal.timeout(180000) });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function checkpoint() {
  const database = await new Promise((resolve, reject) => {
    const request = indexedDB.open('photostore-upload-benchmark', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('reports');
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
  try {
    await new Promise((resolve, reject) => {
      const transaction = database.transaction('reports', 'readwrite');
      transaction.objectStore('reports').put(report, report.startedAt || 'latest');
      transaction.oncomplete = resolve; transaction.onerror = () => reject(transaction.error);
    });
  } finally { database.close(); }
}

async function verify(result) {
  const finalized = result.records.filter(record => record.status === 'finalized');
  for (let offset = 0; offset < finalized.length; offset += 20) {
    const batch = finalized.slice(offset, offset + 20);
    const response = await authenticated('/upload/benchmark/verify', { files: batch.map(record => ({
      filename: record.filename, sha256: record.sha256, blobName: record.blobName, totalSize: record.originalBytes,
      benchmark: { runId: result.runId, arm: result.arm },
      expectedDerivatives: record.sensor?.status === 'generated' ? { preview: record.sensor.preview.bytes, thumbnail: record.sensor.thumbnail.bytes } : {},
    })) });
    batch.forEach((record, index) => { record.verification = response.results?.[index] || { verified: false }; });
    status(`Verifying ${result.scope} ${result.arm}: ${Math.min(offset + 20, finalized.length)} / ${finalized.length}`);
  }
}

async function showSamples(result) {
  const generated = result.records.filter(record => record.verification?.verified && record.sensor?.status === 'generated').slice(0, 4);
  if (!generated.length) return;
  const media = await authenticated('/api/photos/media-token', undefined, (config.apiBaseUrl || '').replace(/\/$/, ''));
  const thumbnail = media.thumbnail || media.thumbnails || media;
  if (!thumbnail?.baseUrl || !thumbnail.sas) return;
  element('samples').replaceChildren();
  for (const record of generated) {
    const figure = document.createElement('figure');
    const image = document.createElement('img');
    image.alt = record.sourceFilename;
    image.src = `${thumbnail.baseUrl.replace(/\/$/, '')}/preview/${encodeURIComponent(record.blobName)}.jpg?${thumbnail.sas.replace(/^\?/, '')}`;
    const caption = document.createElement('figcaption'); caption.textContent = record.sourceFilename;
    figure.append(image, caption); element('samples').append(figure);
  }
}

function pilotFiles() {
  if (files.length <= 12) return files;
  const sample = [];
  for (const extension of ['cr2', 'cr3', 'nef', 'dng', 'ari', 'ori']) {
    const matching = files.filter(file => file.name.toLowerCase().endsWith('.' + extension)).sort((a, b) => a.size - b.size);
    const file = matching[Math.floor(matching.length / 2)];
    if (file) sample.push(file);
  }
  return sample.length ? sample : files.slice(0, 6);
}

async function run(scope) {
  if (running || !target) return;
  const selected = scope === 'pilot' ? pilotFiles() : files;
  if (!selected.length || (scope === 'full' && (!pilotPassed || !corpusMatches() || pilotInventory !== inventoryKey()))) return;
  running = true; stopRequested = false; controls();
  report.startedAt ||= new Date().toISOString();
  report.target = { libraryId: target.libraryId, storageAccount: target.storageAccount };
  const runId = `run-${Date.now().toString(36)}-${crypto.randomUUID().slice(0, 8)}`;
  const current = [];
  try {
    for (const arm of ['originals', 'derivatives']) {
      if (stopRequested) break;
      let finished = 0;
      const began = performance.now();
      element('progress').max = selected.length; element('progress').value = 0;
      progressTimer = setInterval(() => status(`${scope}: ${arm}, ${finished} / ${selected.length}, ${((performance.now() - began) / 1000).toFixed(0)} s`), 1000);
      const result = await runCloudBenchmark({
        token: getToken(), arm, runId, prefix: `rawbench-${runId}-${arm}-`, expectedFiles: selected.length,
        uploadBase, storageAccount: target.storageAccount, workerUrl,
        shouldStop: () => stopRequested, requestStop: () => { stopRequested = true; },
      }, selected, event => {
        if (event.event !== 'finished') return;
        finished += 1; element('progress').value = finished;
        recent = [...recent, `${arm} | ${event.sourceFilename} | ${event.status}${event.error ? ' | ' + event.error : ''}`].slice(-12);
        element('events').textContent = recent.join('\n');
      });
      clearInterval(progressTimer);
      Object.assign(result, { runId, scope, startedAt: new Date(Date.now() - result.wallMs).toISOString() });
      report.runs.push(result); current.push(result); renderSummary(); controls();
      await checkpoint();
      await verify(result);
      result.summary = summarize(result); renderSummary(); await checkpoint();
    }
    const expectedRejection = record => /\.(ori|lri|sti)$/i.test(record.sourceFilename) && /Invalid filename/.test(record.error || '');
    const allValid = current.length === 2 && current.every(result => !result.stopped && result.records.every(record => record.verification?.verified || expectedRejection(record)));
    const developed = current.find(result => result.arm === 'derivatives')?.records.filter(record => record.sensor?.status === 'generated').length || 0;
    if (scope === 'pilot') { pilotPassed = allValid && developed >= Math.min(2, selected.length); pilotInventory = inventoryKey(); }
    status(stopRequested ? 'Stopped. Completed files are retained and isolated.' : scope === 'pilot' ? (pilotPassed ? 'Pilot passed: assets and persistent isolation verified.' : 'Pilot did not pass. Review results before the full run.') : allValid ? 'Full comparison complete. Assets and persistent isolation verified.' : 'Comparison finished with failures. Review results.');
    const derivatives = current.find(result => result.arm === 'derivatives');
    if (derivatives) await showSamples(derivatives).catch(() => {});
  } catch (error) { clearInterval(progressTimer); status(describe(error)); }
  finally { running = false; controls(); }
}

element('photos').addEventListener('change', () => {
  files = [...element('photos').files].sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : a.size - b.size);
  element('inventory').textContent = `${files.length.toLocaleString()} files / ${(files.reduce((sum, file) => sum + file.size, 0) / 1e9).toFixed(2)} GB${corpusMatches() ? ' / full corpus matched' : ''}`;
  controls();
});
element('pilot').addEventListener('click', () => { void run('pilot'); });
element('full').addEventListener('click', () => { void run('full'); });
element('stop').addEventListener('click', () => { stopRequested = true; controls(); });
element('download').addEventListener('click', () => {
  const content = { ...report, summaries: report.runs.map(summarize) };
  const url = URL.createObjectURL(new Blob([JSON.stringify(content, null, 2)], { type: 'application/json' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `raw-upload-benchmark-${Date.now()}.json`; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
window.addEventListener('beforeunload', event => { if (running) { event.preventDefault(); event.returnValue = ''; } });
element('account').textContent = getEmailFromToken();
authenticated('/upload/benchmark/config').then(value => {
  if (!value.enabled || value.processingIsolation !== 'persistent' || !value.directPreview) throw new Error('Deployment does not support the approved benchmark');
  target = value; element('isolation').textContent = 'Persistently isolated'; status('Ready for pilot'); controls();
}).catch(error => status(describe(error)));
