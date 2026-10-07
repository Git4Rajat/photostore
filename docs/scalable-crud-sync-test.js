/*
 * Destructive Photostore scalable CRUD sync test for the browser console.
 * Pasting this file only installs the controller and prints a read-only plan.
 *
 * Run suites:
 *   await scalableCrudSync.runPhotos('DELETE PHOTOS')
 *   await scalableCrudSync.runPeople('DELETE PEOPLE')
 *
 * Resume at a stage without replaying earlier stages:
 *   await scalableCrudSync.runPhotosFrom(4, 'DELETE PHOTOS')
 *
 * Watch an already-accepted photo job without submitting another delete:
 *   await scalableCrudSync.recoverPhotoJob('<jobId>', <beforeCount>, <accepted>)
 *
 * Copy results:
 *   scalableCrudSync.copyReport()
 */
(() => {
  'use strict';

  const options = {
    photoStages: [1, 100, 2000, 10000, 50000],
    peopleStages: [1, 50, 500],
    interStageDelayMs: 30000,
    pollIntervalMs: 5000,
    jobTimeoutMs: 2 * 60 * 60 * 1000,
    convergenceTimeoutMs: 45 * 60 * 1000,
    requestTimeoutMs: 45 * 60 * 1000,
    emitUiSignal: true,
    manualToken: '',
  };

  const config = window.__APP_CONFIG__ || {};
  const base = (value) => (value || '').replace(/\/$/, '') || location.origin;
  const api = base(config.apiBaseUrl);
  const extras = base(config.extrasApiBaseUrl || config.apiBaseUrl);
  const tools = base(config.toolsApiBaseUrl || config.apiBaseUrl);
  const token = options.manualToken || localStorage.getItem('photostore.passwordAuthToken') || '';
  const session = Math.random().toString(36).slice(2, 8);
  const report = {
    startedAt: new Date().toISOString(),
    session,
    options: { ...options, manualToken: options.manualToken ? '(set)' : '' },
    bases: { api, extras, tools },
    plans: {},
    photoStages: [],
    photoRecoveries: [],
    peopleStages: [],
    errors: [],
  };
  let requestNumber = 0;
  let running = false;

  const now = () => new Date().toISOString();
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const elapsed = (started) => Math.round(performance.now() - started);
  const note = (message) => console.log(`%c${message}`, 'color:#087f5b;font-weight:600');
  const warn = (message) => console.warn(`[crud-sync] ${message}`);

  async function request(method, url, body, timeoutMs = options.requestTimeoutMs) {
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
    try { json = text ? JSON.parse(text) : null; } catch { /* preserve non-JSON response */ }
    const result = {
      status: response.status,
      ms: elapsed(started),
      requestId,
      responseBytes: new Blob([text]).size,
      json,
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
    const response = await request('GET', `${api}/api/photos?${query}`);
    return response.json || {};
  }

  async function peoplePage(offset = 0, limit = 1) {
    const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    const response = await request('GET', `${extras}/api/persons/page?${query}`);
    return response.json || {};
  }

  const currentPhotoCount = async () => Number((await photoPage()).total || 0);
  const currentPeopleCount = async () => {
    const data = await peoplePage();
    return data.available === false ? null : Number(data.libraryTotal ?? data.total ?? 0);
  };

  async function collectPhotoIds(count) {
    const ids = [];
    while (ids.length < count) {
      const data = await photoPage(ids.length, Math.min(5000, count - ids.length));
      const page = Array.isArray(data.filenames) ? data.filenames : [];
      ids.push(...page);
      if (!data.hasMore || page.length === 0) break;
    }
    return ids.slice(0, count);
  }

  async function collectPersonIds(count) {
    const ids = [];
    while (ids.length < count) {
      const data = await peoplePage(ids.length, Math.min(500, count - ids.length));
      if (data.available === false) throw new Error(`People index unavailable (${data.reason || 'building'})`);
      const page = Array.isArray(data.rows) ? data.rows : [];
      ids.push(...page.map((row) => row.personId).filter(Boolean));
      if (!data.hasMore || page.length === 0) break;
    }
    return ids.slice(0, count);
  }

  async function waitForPhotoJob(jobId) {
    const started = performance.now();
    let polls = 0;
    let transientErrors = 0;
    let last = null;
    let lastError = null;
    while (elapsed(started) < options.jobTimeoutMs) {
      polls += 1;
      try {
        const response = await request('GET', `${tools}/api/jobs/status`, undefined, 60000);
        last = ((response.json && response.json.jobs) || []).find((job) => job.jobId === jobId) || null;
        if (last && ['done', 'failed'].includes(String(last.status).toLowerCase())) {
          return { status: last.status, waitMs: elapsed(started), polls, transientErrors, job: last };
        }
      } catch (error) {
        transientErrors += 1;
        lastError = String(error);
        warn(`Job status read failed (${transientErrors}); polling will continue`);
      }
      if (polls % 6 === 0) note(`Job is ${last ? last.status : 'not visible yet'} (${Math.round(elapsed(started) / 1000)}s)`);
      await sleep(options.pollIntervalMs);
    }
    return { status: 'timeout', waitMs: elapsed(started), polls, transientErrors, lastError, job: last };
  }

  async function waitForCount(kind, expectedMaximum) {
    const started = performance.now();
    const observations = [];
    const read = kind === 'photos' ? currentPhotoCount : currentPeopleCount;
    let polls = 0;
    let transientErrors = 0;
    let lastCount;
    while (elapsed(started) < options.convergenceTimeoutMs) {
      polls += 1;
      try {
        const count = await read();
        if (count !== lastCount) {
          observations.push({ atMs: elapsed(started), count });
          lastCount = count;
        }
        if (count !== null && count <= expectedMaximum) {
          return { converged: true, count, waitMs: elapsed(started), polls, transientErrors, observations };
        }
        if (polls % 6 === 0) note(`${kind} index count is ${count}; waiting for <= ${expectedMaximum}`);
      } catch (error) {
        transientErrors += 1;
        observations.push({ atMs: elapsed(started), error: String(error) });
        warn(`${kind} count read failed (${transientErrors}); polling will continue`);
      }
      await sleep(options.pollIntervalMs);
    }
    return { converged: false, count: lastCount ?? null, waitMs: elapsed(started), polls, transientErrors, observations };
  }

  function emitUiChange(operation, domains, itemCount, jobId) {
    if (!options.emitUiSignal) return null;
    const change = {
      id: `crud-sync-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
      at: Date.now(), operation, domains, itemCount, ...(jobId ? { jobId } : {}),
    };
    try {
      const channel = new BroadcastChannel('photostore.library-changes.v1');
      channel.postMessage(change);
      setTimeout(() => channel.close(), 1000);
    } catch (error) { warn(`UI signal failed: ${String(error)}`); }
    try { localStorage.setItem('photostore.library-change.latest', JSON.stringify(change)); } catch { /* ignore */ }
    return change;
  }

  async function runPhotoStage(count, stage) {
    const row = { stage: stage + 1, requested: count, startedAt: now() };
    report.photoStages.push(row);
    row.beforeCount = await currentPhotoCount();
    if (row.beforeCount < count) throw new Error(`Only ${row.beforeCount} photos remain; ${count} required`);
    const selectionStarted = performance.now();
    const ids = await collectPhotoIds(count);
    row.selectionMs = elapsed(selectionStarted);
    row.selected = ids.length;
    row.sampleIds = [ids[0], ids[Math.floor(ids.length / 2)], ids[ids.length - 1]].filter(Boolean);
    const payload = { filenames: ids };
    row.requestBytes = new Blob([JSON.stringify(payload)]).size;
    const mutation = await request('POST', `${api}/api/photos/delete`, payload);
    row.http = { status: mutation.status, ms: mutation.ms, requestId: mutation.requestId, responseBytes: mutation.responseBytes };
    row.accepted = Number(mutation.json?.accepted || mutation.json?.deleted?.length || 0);
    row.jobId = mutation.json?.jobId;
    row.mode = row.jobId ? 'queued' : 'inline';
    // Mirror the real UI's optimistic count as soon as the backend accepts
    // the mutation; index convergence is measured separately below.
    row.uiSignal = emitUiChange('photos-deleted', ['photos', 'people', 'albums', 'explore', 'trash'], row.accepted || count, row.jobId);
    if (row.jobId) {
      row.job = await waitForPhotoJob(row.jobId);
      if (row.job.status !== 'done') throw new Error(`Photo job ended with ${row.job.status}`);
    }
    row.convergence = await waitForCount('photos', row.beforeCount - (row.accepted || count));
    row.outcome = row.convergence.converged ? 'passed' : 'index-timeout';
    row.finishedAt = now();
    if (!row.convergence.converged) throw new Error('Photo index convergence timed out');
  }

  async function runPeopleStage(count, stage) {
    const row = { stage: stage + 1, requested: count, startedAt: now() };
    report.peopleStages.push(row);
    row.beforeCount = await currentPeopleCount();
    if (row.beforeCount === null) throw new Error('People index unavailable');
    if (row.beforeCount < count) throw new Error(`Only ${row.beforeCount} people remain; ${count} required`);
    const selectionStarted = performance.now();
    const ids = await collectPersonIds(count);
    row.selectionMs = elapsed(selectionStarted);
    row.selected = ids.length;
    row.sampleIds = [ids[0], ids[Math.floor(ids.length / 2)], ids[ids.length - 1]].filter(Boolean);
    const payload = { personIds: ids };
    row.requestBytes = new Blob([JSON.stringify(payload)]).size;
    const mutation = await request('POST', `${extras}/api/persons/delete`, payload);
    row.http = { status: mutation.status, ms: mutation.ms, requestId: mutation.requestId, responseBytes: mutation.responseBytes };
    row.deleted = mutation.json?.deletedPersonIds?.length || 0;
    row.errors = mutation.json?.errors?.length || 0;
    row.facesUpdated = mutation.json?.facesUpdated;
    row.metadataRebuild = mutation.json?.metadataRebuild;
    row.uiSignal = emitUiChange('people-deleted', ['people'], row.deleted);
    row.convergence = await waitForCount('people', row.beforeCount - row.deleted);
    row.outcome = row.convergence.converged && row.errors === 0 ? 'passed' : 'partial';
    row.finishedAt = now();
    if (!row.convergence.converged) throw new Error('People index convergence timed out');
  }

  async function runSuite(kind, confirmation, startStage = 1) {
    const expected = kind === 'photos' ? 'DELETE PHOTOS' : 'DELETE PEOPLE';
    if (confirmation !== expected) throw new Error(`Pass the exact phrase '${expected}'`);
    if (running) throw new Error('Another CRUD sync operation is running in this tab');
    const stages = kind === 'photos' ? options.photoStages : options.peopleStages;
    const start = Number(startStage) - 1;
    if (!Number.isInteger(start) || start < 0 || start >= stages.length) throw new Error(`Stage must be 1-${stages.length}`);
    const runner = kind === 'photos' ? runPhotoStage : runPeopleStage;
    running = true;
    try {
      for (let index = start; index < stages.length; index += 1) {
        note(`${kind} stage ${index + 1}/${stages.length}: ${stages[index].toLocaleString()}`);
        try {
          await runner(stages[index], index);
        } catch (error) {
          const row = (kind === 'photos' ? report.photoStages : report.peopleStages).at(-1);
          if (row) { row.outcome = 'failed'; row.error = String(error); row.errorDetails = error?.details; row.finishedAt = now(); }
          report.errors.push({ suite: kind, stage: stages[index], error: String(error), details: error?.details });
          throw error;
        }
        if (index < stages.length - 1 && options.interStageDelayMs > 0) await sleep(options.interStageDelayMs);
      }
      return report;
    } finally {
      running = false;
      report.finishedAt = now();
      printReport();
    }
  }

  async function recoverPhotoJob(jobId, beforeCount, accepted) {
    if (!jobId || !Number.isFinite(Number(beforeCount)) || !Number.isFinite(Number(accepted))) {
      throw new Error('Pass jobId, beforeCount, and accepted');
    }
    if (running) throw new Error('Another CRUD sync operation is running in this tab');
    running = true;
    const row = { jobId: String(jobId), beforeCount: Number(beforeCount), accepted: Number(accepted), startedAt: now() };
    report.photoRecoveries.push(row);
    try {
      note('Watching the existing job; no delete request will be sent');
      const expectedMaximum = row.beforeCount - row.accepted;
      const currentCount = await currentPhotoCount();
      if (currentCount <= expectedMaximum) {
        row.job = { status: 'inferred-done', waitMs: 0, polls: 0, note: 'Count already converged; job may have aged out of status.' };
        row.uiSignal = emitUiChange('photos-deleted', ['photos', 'people', 'albums', 'explore', 'trash'], row.accepted, row.jobId);
        row.convergence = { converged: true, count: currentCount, waitMs: 0, polls: 1, transientErrors: 0,
          observations: [{ atMs: 0, count: currentCount }] };
        row.outcome = 'passed';
        row.finishedAt = now();
        return row;
      }
      row.job = await waitForPhotoJob(row.jobId);
      if (row.job.status !== 'done') throw new Error(`Photo job ended with ${row.job.status}`);
      row.uiSignal = emitUiChange('photos-deleted', ['photos', 'people', 'albums', 'explore', 'trash'], row.accepted, row.jobId);
      row.convergence = await waitForCount('photos', expectedMaximum);
      row.outcome = row.convergence.converged ? 'passed' : 'index-timeout';
      row.finishedAt = now();
      return row;
    } catch (error) {
      row.outcome = 'failed'; row.error = String(error); row.errorDetails = error?.details; row.finishedAt = now();
      report.errors.push({ suite: 'photo-recovery', jobId: row.jobId, error: row.error, details: row.errorDetails });
      throw error;
    } finally {
      running = false;
      report.finishedAt = now();
      printReport();
    }
  }

  async function plan() {
    const [photos, people] = await Promise.all([currentPhotoCount(), currentPeopleCount()]);
    report.plans = {
      checkedAt: now(),
      photos: { available: photos, requested: options.photoStages.reduce((sum, n) => sum + n, 0), stages: options.photoStages },
      people: { available: people, requested: options.peopleStages.reduce((sum, n) => sum + n, 0), stages: options.peopleStages },
    };
    console.table([
      { suite: 'photos', available: photos, totalToDelete: report.plans.photos.requested, stages: options.photoStages.join(' -> ') },
      { suite: 'people', available: people, totalToDelete: report.plans.people.requested, stages: options.peopleStages.join(' -> ') },
    ]);
    if (!token) warn('No password token detected; set manualToken before pasting when using Entra login');
    return report.plans;
  }

  function printReport() {
    window.__scalableCrudSync = report;
    const photoRows = report.photoStages.map((row) => ({
      stage: row.requested, mode: row.mode, requestMs: row.http?.ms, jobWaitMs: row.job?.waitMs,
      indexWaitMs: row.convergence?.waitMs, before: row.beforeCount, after: row.convergence?.count, outcome: row.outcome,
    }));
    console.log('%c==== SCALABLE CRUD SYNC RESULT ====', 'font-weight:bold;font-size:14px');
    if (photoRows.length) console.table(photoRows);
    if (report.peopleStages.length) console.table(report.peopleStages);
    if (report.photoRecoveries.length) console.table(report.photoRecoveries);
    if (report.errors.length) console.table(report.errors);
  }

  function copyReport() {
    const json = JSON.stringify(report, null, 2);
    if (typeof copy === 'function') copy(json);
    else void navigator.clipboard.writeText(json);
    return report;
  }

  window.__scalableCrudSync = report;
  window.scalableCrudSync = {
    options,
    plan,
    runPhotos: (confirmation) => runSuite('photos', confirmation),
    runPeople: (confirmation) => runSuite('people', confirmation),
    runPhotosFrom: (stage, confirmation) => runSuite('photos', confirmation, stage),
    runPeopleFrom: (stage, confirmation) => runSuite('people', confirmation, stage),
    recoverPhotoJob,
    report: () => { printReport(); return report; },
    copyReport,
  };

  note('Scalable CRUD sync test installed. No data has been changed.');
  void plan().catch((error) => console.error('[crud-sync] Plan failed:', error));
})();
