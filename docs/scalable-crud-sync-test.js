/*
 * Photostore scalable CRUD sync test (browser console).
 *
 * This is DESTRUCTIVE: it moves photos to Recently Deleted and permanently
 * deletes people clusters. It installs a test controller but does not delete
 * anything until an exact confirmation phrase is passed.
 *
 * HOW TO RUN
 *   1. Open Photostore, sign in, and keep the Gallery or People page visible.
 *   2. DevTools -> Console. Paste this whole file and press Enter.
 *   3. Review the plan printed by the script.
 *   4. Run one or both destructive suites:
 *        await scalableCrudSync.runPhotos('DELETE PHOTOS')
 *        await scalableCrudSync.runPeople('DELETE PEOPLE')
 *   5. Copy the result:
 *        scalableCrudSync.copyReport()
 *      If a run stops, resume without replaying completed stages:
 *        await scalableCrudSync.runPhotosFrom(4, 'DELETE PHOTOS')
 *        await scalableCrudSync.runPeopleFrom(2, 'DELETE PEOPLE')
 *
 * Photo stages delete 1, 100, 2,000, 10,000, then 50,000 currently-visible
 * photos. People stages delete 1, 50, then 500 currently-visible clusters.
 * Each stage uses a disjoint set because it waits for the relevant index count
 * to converge before selecting the next stage. Change OPTIONS to run a subset.
 *
 * Direct console fetches bypass the frontend store's optimistic update. With
 * emitUiSignal=true this script emits the same cross-view library-change event
 * after the backend mutation is complete, so the open app can reconcile.
 */
(() => {
  'use strict';

  const OPTIONS = {
    photoStages: [1, 100, 2000, 10000, 50000],
    peopleStages: [1, 50, 500],
    interStageDelayMs: 30000,
    pollIntervalMs: 5000,
    jobTimeoutMs: 45 * 60 * 1000,
    convergenceTimeoutMs: 20 * 60 * 1000,
    requestTimeoutMs: 45 * 60 * 1000,
    emitUiSignal: true,
    manualToken: '', // Required here only when Entra token auto-detection is unavailable.
  };

  const cfg = window.__APP_CONFIG__ || {};
  const origin = location.origin;
  const base = (value) => (value || '').replace(/\/$/, '') || origin;
  const API = base(cfg.apiBaseUrl);
  const EXTRAS = base(cfg.extrasApiBaseUrl || cfg.apiBaseUrl);
  const TOOLS = base(cfg.toolsApiBaseUrl || cfg.apiBaseUrl);
  const token = OPTIONS.manualToken || localStorage.getItem('photostore.passwordAuthToken') || '';
  const session = Math.random().toString(36).slice(2, 8);
  const report = {
    startedAt: new Date().toISOString(),
    session,
    options: { ...OPTIONS, manualToken: OPTIONS.manualToken ? '(set)' : '' },
    bases: { api: API, extras: EXTRAS, tools: TOOLS },
    plans: {},
    photoStages: [],
    peopleStages: [],
    errors: [],
  };
  let requestNumber = 0;
  let running = false;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const now = () => new Date().toISOString();
  const note = (message) => console.log(`%c${message}`, 'color:#087f5b;font-weight:600');
  const warn = (message) => console.warn(`[crud-sync] ${message}`);
  const elapsed = (started) => Math.round(performance.now() - started);

  async function request(method, url, body, timeoutMs = OPTIONS.requestTimeoutMs) {
    const requestId = `crud-sync-${session}-${++requestNumber}`;
    const headers = {
      'X-Request-ID': requestId,
      'X-Client-View': 'scalable-crud-sync-test',
      'X-Client-Session': session,
    };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const started = performance.now();
    let response;
    let text = '';
    try {
      response = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: 'no-store',
        signal: controller.signal,
      });
      text = await response.text();
    } catch (error) {
      const wrapped = new Error(`${method} ${url}: ${String(error)}`);
      wrapped.details = { requestId, status: 0, ms: elapsed(started), error: String(error) };
      throw wrapped;
    } finally {
      clearTimeout(timer);
    }
    let json = null;
    try { json = text ? JSON.parse(text) : null; } catch { /* keep raw response */ }
    const result = {
      ok: response.ok,
      status: response.status,
      ms: elapsed(started),
      requestId,
      json,
      responseBytes: new Blob([text]).size,
    };
    if (!response.ok) {
      const error = new Error(`${method} ${url} returned ${response.status}: ${(json && json.error) || text.slice(0, 300)}`);
      error.details = result;
      throw error;
    }
    return result;
  }

  async function photoPage(offset = 0, limit = 1) {
    const query = new URLSearchParams({ sort: 'name', idsOnly: '1', offset: String(offset), limit: String(limit) });
    return request('GET', `${API}/api/photos?${query}`).then((result) => ({ result, data: result.json || {} }));
  }

  async function peoplePage(offset = 0, limit = 1) {
    const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    return request('GET', `${EXTRAS}/api/persons/page?${query}`).then((result) => ({ result, data: result.json || {} }));
  }

  async function collectPhotoIds(count) {
    const ids = [];
    while (ids.length < count) {
      const wanted = Math.min(5000, count - ids.length);
      const { data } = await photoPage(ids.length, wanted);
      const page = Array.isArray(data.filenames) ? data.filenames : [];
      ids.push(...page);
      if (!data.hasMore || page.length === 0) break;
    }
    return ids.slice(0, count);
  }

  async function collectPersonIds(count) {
    const ids = [];
    while (ids.length < count) {
      const wanted = Math.min(500, count - ids.length);
      const { data } = await peoplePage(ids.length, wanted);
      if (data.available === false) throw new Error(`People index is unavailable (${data.reason || 'building'})`);
      const page = Array.isArray(data.rows) ? data.rows : [];
      ids.push(...page.map((row) => row.personId).filter(Boolean));
      if (!data.hasMore || page.length === 0) break;
    }
    return ids.slice(0, count);
  }

  async function currentPhotoCount() {
    const { data } = await photoPage(0, 1);
    return Number(data.total || 0);
  }

  async function currentPeopleCount() {
    const { data } = await peoplePage(0, 1);
    if (data.available === false) return null;
    return Number(data.libraryTotal ?? data.total ?? 0);
  }

  async function waitForPhotoJob(jobId) {
    const started = performance.now();
    let polls = 0;
    let last = null;
    while (elapsed(started) < OPTIONS.jobTimeoutMs) {
      polls += 1;
      const response = await request('GET', `${TOOLS}/api/jobs/status`, undefined, 60000);
      last = ((response.json && response.json.jobs) || []).find((job) => job.jobId === jobId) || null;
      if (last && ['done', 'failed'].includes(String(last.status).toLowerCase())) {
        return { status: last.status, waitMs: elapsed(started), polls, job: last };
      }
      if (polls % 6 === 0) note(`Job ${jobId} is ${last ? last.status : 'not visible yet'} (${Math.round(elapsed(started) / 1000)}s)`);
      await sleep(OPTIONS.pollIntervalMs);
    }
    return { status: 'timeout', waitMs: elapsed(started), polls, job: last };
  }

  async function waitForCount(kind, expectedMaximum) {
    const started = performance.now();
    const observations = [];
    const read = kind === 'photos' ? currentPhotoCount : currentPeopleCount;
    while (elapsed(started) < OPTIONS.convergenceTimeoutMs) {
      const count = await read();
      observations.push({ atMs: elapsed(started), count });
      if (count !== null && count <= expectedMaximum) {
        return { converged: true, count, waitMs: elapsed(started), polls: observations.length, observations };
      }
      if (observations.length % 6 === 0) note(`${kind} index count is ${count}; waiting for <= ${expectedMaximum}`);
      await sleep(OPTIONS.pollIntervalMs);
    }
    return {
      converged: false,
      count: observations.length ? observations[observations.length - 1].count : null,
      waitMs: elapsed(started),
      polls: observations.length,
      observations,
    };
  }

  function emitUiChange(operation, domains, itemCount, jobId) {
    if (!OPTIONS.emitUiSignal) return null;
    const change = {
      id: `crud-sync-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
      at: Date.now(),
      operation,
      domains,
      itemCount,
      ...(jobId ? { jobId } : {}),
    };
    try {
      const channel = new BroadcastChannel('photostore.library-changes.v1');
      channel.postMessage(change);
      setTimeout(() => channel.close(), 1000);
    } catch (error) {
      warn(`BroadcastChannel failed: ${String(error)}`);
    }
    try { localStorage.setItem('photostore.library-change.latest', JSON.stringify(change)); } catch { /* ignore */ }
    return change;
  }

  async function delayBeforeNext(kind, index, stages) {
    if (index >= stages.length - 1 || OPTIONS.interStageDelayMs <= 0) return;
    note(`${kind}: waiting ${Math.round(OPTIONS.interStageDelayMs / 1000)}s before the next stage`);
    await sleep(OPTIONS.interStageDelayMs);
  }

  async function runPhotoStage(count, index) {
    const row = { stage: index + 1, requested: count, startedAt: now() };
    report.photoStages.push(row);
    try {
      row.beforeCount = await currentPhotoCount();
      if (row.beforeCount < count) throw new Error(`Only ${row.beforeCount} active photos remain; stage needs ${count}`);
      note(`Photos ${index + 1}/${OPTIONS.photoStages.length}: selecting ${count.toLocaleString()} of ${row.beforeCount.toLocaleString()}`);
      const selectionStarted = performance.now();
      const ids = await collectPhotoIds(count);
      row.selectionMs = elapsed(selectionStarted);
      row.selected = ids.length;
      row.sampleIds = [ids[0], ids[Math.floor(ids.length / 2)], ids[ids.length - 1]].filter(Boolean);
      const payload = { filenames: ids };
      row.requestBytes = new Blob([JSON.stringify(payload)]).size;
      const mutation = await request('POST', `${API}/api/photos/delete`, payload);
      row.http = { status: mutation.status, ms: mutation.ms, requestId: mutation.requestId, responseBytes: mutation.responseBytes };
      row.accepted = Number((mutation.json && mutation.json.accepted) || (mutation.json && mutation.json.deleted && mutation.json.deleted.length) || 0);
      row.jobId = mutation.json && mutation.json.jobId;
      row.mode = row.jobId ? 'queued' : 'inline';
      if (row.jobId) {
        row.job = await waitForPhotoJob(row.jobId);
        if (row.job.status !== 'done') throw new Error(`Photo delete job ended with ${row.job.status}`);
      }
      row.uiSignal = emitUiChange('photos-deleted', ['photos', 'people', 'albums', 'explore', 'trash'], row.accepted || count, row.jobId);
      row.convergence = await waitForCount('photos', row.beforeCount - (row.accepted || count));
      row.finishedAt = now();
      row.outcome = row.convergence.converged ? 'passed' : 'index-timeout';
      console.table([summarizePhoto(row)]);
      if (!row.convergence.converged) throw new Error(`Photo index did not converge within ${OPTIONS.convergenceTimeoutMs}ms`);
    } catch (error) {
      row.finishedAt = now();
      row.outcome = 'failed';
      row.error = String(error);
      row.errorDetails = error && error.details;
      report.errors.push({ suite: 'photos', stage: count, error: row.error, details: row.errorDetails });
      throw error;
    }
  }

  async function runPeopleStage(count, index) {
    const row = { stage: index + 1, requested: count, startedAt: now() };
    report.peopleStages.push(row);
    try {
      row.beforeCount = await currentPeopleCount();
      if (row.beforeCount === null) throw new Error('People index is unavailable');
      if (row.beforeCount < count) throw new Error(`Only ${row.beforeCount} people remain; stage needs ${count}`);
      note(`People ${index + 1}/${OPTIONS.peopleStages.length}: selecting ${count.toLocaleString()} of ${row.beforeCount.toLocaleString()}`);
      const selectionStarted = performance.now();
      const ids = await collectPersonIds(count);
      row.selectionMs = elapsed(selectionStarted);
      row.selected = ids.length;
      row.sampleIds = [ids[0], ids[Math.floor(ids.length / 2)], ids[ids.length - 1]].filter(Boolean);
      const payload = { personIds: ids };
      row.requestBytes = new Blob([JSON.stringify(payload)]).size;
      const mutation = await request('POST', `${EXTRAS}/api/persons/delete`, payload);
      row.http = { status: mutation.status, ms: mutation.ms, requestId: mutation.requestId, responseBytes: mutation.responseBytes };
      row.deleted = ((mutation.json && mutation.json.deletedPersonIds) || []).length;
      row.errors = ((mutation.json && mutation.json.errors) || []).length;
      row.facesUpdated = mutation.json && mutation.json.facesUpdated;
      row.metadataRebuild = mutation.json && mutation.json.metadataRebuild;
      row.uiSignal = emitUiChange('people-deleted', ['people'], row.deleted);
      row.convergence = await waitForCount('people', row.beforeCount - row.deleted);
      row.finishedAt = now();
      row.outcome = row.convergence.converged && row.errors === 0 ? 'passed' : 'partial';
      console.table([summarizePeople(row)]);
      if (!row.convergence.converged) throw new Error(`People index did not converge within ${OPTIONS.convergenceTimeoutMs}ms`);
    } catch (error) {
      row.finishedAt = now();
      row.outcome = 'failed';
      row.error = String(error);
      row.errorDetails = error && error.details;
      report.errors.push({ suite: 'people', stage: count, error: row.error, details: row.errorDetails });
      throw error;
    }
  }

  function summarizePhoto(row) {
    return {
      stage: row.requested,
      mode: row.mode,
      requestMs: row.http && row.http.ms,
      jobWaitMs: row.job && row.job.waitMs,
      indexWaitMs: row.convergence && row.convergence.waitMs,
      before: row.beforeCount,
      after: row.convergence && row.convergence.count,
      outcome: row.outcome,
    };
  }

  function summarizePeople(row) {
    return {
      stage: row.requested,
      requestMs: row.http && row.http.ms,
      indexWaitMs: row.convergence && row.convergence.waitMs,
      deleted: row.deleted,
      facesUpdated: row.facesUpdated,
      before: row.beforeCount,
      after: row.convergence && row.convergence.count,
      outcome: row.outcome,
    };
  }

  async function plan() {
    note('Reading current library counts (no changes are being made)');
    const [photos, people] = await Promise.all([currentPhotoCount(), currentPeopleCount()]);
    report.plans = {
      checkedAt: now(),
      photos: { available: photos, requested: OPTIONS.photoStages.reduce((sum, value) => sum + value, 0), stages: OPTIONS.photoStages },
      people: { available: people, requested: OPTIONS.peopleStages.reduce((sum, value) => sum + value, 0), stages: OPTIONS.peopleStages },
    };
    console.table([
      { suite: 'photos', available: photos, totalToDelete: report.plans.photos.requested, stages: OPTIONS.photoStages.join(' -> ') },
      { suite: 'people', available: people, totalToDelete: report.plans.people.requested, stages: OPTIONS.peopleStages.join(' -> ') },
    ]);
    if (!token) warn('No password token was detected. Set OPTIONS.manualToken before pasting when using Entra authentication.');
    return report.plans;
  }

  async function runSuite(kind, confirmation, startStage = 1) {
    const expected = kind === 'photos' ? 'DELETE PHOTOS' : 'DELETE PEOPLE';
    if (confirmation !== expected) throw new Error(`Destructive test refused. Pass the exact phrase '${expected}'.`);
    if (running) throw new Error('A CRUD sync suite is already running in this tab.');
    const stages = kind === 'photos' ? OPTIONS.photoStages : OPTIONS.peopleStages;
    const startIndex = Number(startStage) - 1;
    if (!Number.isInteger(startIndex) || startIndex < 0 || startIndex >= stages.length) {
      throw new Error(`startStage must be between 1 and ${stages.length}`);
    }
    running = true;
    const runner = kind === 'photos' ? runPhotoStage : runPeopleStage;
    try {
      for (let index = startIndex; index < stages.length; index += 1) {
        await runner(stages[index], index);
        await delayBeforeNext(kind, index, stages);
      }
      note(`${kind} suite complete`);
      return report;
    } finally {
      running = false;
      report.finishedAt = now();
      window.__scalableCrudSync = report;
      printReport();
    }
  }

  function printReport() {
    console.log('%c==== SCALABLE CRUD SYNC RESULT ====', 'font-weight:bold;font-size:14px');
    if (report.photoStages.length) console.table(report.photoStages.map(summarizePhoto));
    if (report.peopleStages.length) console.table(report.peopleStages.map(summarizePeople));
    if (report.errors.length) { console.log('%cERRORS', 'color:red;font-weight:bold'); console.table(report.errors); }
    console.log('Full result: window.__scalableCrudSync');
  }

  function copyReport() {
    window.__scalableCrudSync = report;
    const json = JSON.stringify(report, null, 2);
    if (typeof copy === 'function') copy(json);
    else navigator.clipboard.writeText(json);
    note('Report copied to the clipboard');
    return report;
  }

  window.__scalableCrudSync = report;
  window.scalableCrudSync = {
    options: OPTIONS,
    plan,
    runPhotos: (confirmation) => runSuite('photos', confirmation),
    runPeople: (confirmation) => runSuite('people', confirmation),
    runPhotosFrom: (stage, confirmation) => runSuite('photos', confirmation, stage),
    runPeopleFrom: (stage, confirmation) => runSuite('people', confirmation, stage),
    report: () => { printReport(); return report; },
    copyReport,
  };

  console.log('%cScalable CRUD sync test installed. No data has been changed.', 'color:#087f5b;font-weight:bold');
  console.log("Run await scalableCrudSync.runPhotos('DELETE PHOTOS') or await scalableCrudSync.runPeople('DELETE PEOPLE') after reviewing the plan.");
  void plan().catch((error) => {
    report.errors.push({ suite: 'plan', error: String(error), details: error && error.details });
    console.error('[crud-sync] Could not read the plan:', error);
  });
})();
