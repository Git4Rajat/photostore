import { BlockBlobClient } from '@azure/storage-blob';


  const now = () => performance.now();
  const BLOCK_BYTES = 8 * 1024 * 1024;

  class Gate {
    constructor(capacity) { this.free = capacity; this.waiters = []; }
    async run(task) {
      if (this.free) this.free -= 1;
      else await new Promise(resolve => this.waiters.push(resolve));
      try { return await task(); }
      finally {
        const next = this.waiters.shift();
        if (next) next();
        else this.free += 1;
      }
    }
  }

  function batcher(endpoint, api, gate, debounceMs, maxWaitMs) {
    let pending = [];
    let debounce;
    let deadline;
    const flush = () => {
      clearTimeout(debounce);
      clearTimeout(deadline);
      debounce = deadline = undefined;
      const batch = pending.splice(0, 12);
      if (!batch.length) return;
      void gate.run(async () => {
        try {
          const response = await api(endpoint, { files: batch.map(item => item.payload) });
          batch.forEach((item, index) => {
            const result = response.results?.[index];
            if (!result || result.index !== index || result.error) item.reject(new Error(`${endpoint}: file ${index}: ${result?.error || 'missing result'}`));
            else item.resolve(result);
          });
        } catch (error) { batch.forEach(item => item.reject(error)); }
      });
    };
    return payload => new Promise((resolve, reject) => {
      pending.push({ payload, resolve, reject });
      if (pending.length >= 12) { flush(); return; }
      clearTimeout(debounce);
      debounce = setTimeout(flush, debounceMs);
      if (!deadline) deadline = setTimeout(flush, maxWaitMs);
    });
  }

  function decode(file, workerUrl) {
    return new Promise(resolve => {
      const started = now();
      const worker = new Worker(workerUrl, { type: 'module' });
      let done = false;
      const finish = result => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        worker.terminate();
        resolve({ ...result, workerWallMs: now() - started });
      };
      const timer = setTimeout(() => finish({ status: 'timeout', error: 'Sensor worker exceeded 60 seconds' }), 60000);
      worker.onmessage = event => finish(event.data);
      worker.onerror = () => finish({ status: 'failed', error: 'Sensor worker initialization failed' });
      worker.postMessage({ file, maxMegapixels: 200 });
    });
  }

  // No signed URLs, bearer tokens, or remote response bodies enter the records.
  const safeError = error => String(error?.message || error).replace(/https?:\/\/[^\s)]+/g, '[url]').slice(0, 300);

  export async function runCloudBenchmark(config, files, recordBenchmark) {
    if (files.length !== config.expectedFiles) throw new Error('File inventory differs from approved run');
    if (!/^rawbench-[a-z0-9-]+-$/.test(config.prefix)) throw new Error('Invalid isolated filename prefix');
    const began = now();
    const blockGate = new Gate(6);
    const sensorGate = new Gate(1);
    const initGate = new Gate(6);
    const finalGate = new Gate(14);
    const pendingGate = new Gate(24);
    let cursor = 0;
    let latestOriginalCommitMs = 0;
    let latestAssetCommitMs = 0;
    const records = [];
    const completions = [];
    const apiRequests = [];
    const api = async (endpoint, payload) => {
      const start = now();
      const body = JSON.stringify(payload);
      let status = 0;
      try {
        const response = await fetch(config.uploadBase + endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.token}` },
          body,
          signal: AbortSignal.timeout(180000),
        });
        status = response.status;
        if ([401, 403].includes(status)) config.requestStop?.();
        if (!response.ok) throw new Error(`${endpoint} returned HTTP ${status}`);
        return await response.json();
      } finally {
        apiRequests.push({ endpoint, files: payload.files.length, status, bodyBytes: new TextEncoder().encode(body).byteLength, wallMs: now() - start });
      }
    };
    const init = batcher('/upload/init-batch', api, initGate, 50, 250);
    const finalize = batcher('/upload/finalize-batch', api, finalGate, 200, 2500);

    const clientFor = signedUrl => {
      const url = new URL(signedUrl);
      if (url.protocol !== 'https:' || url.hostname !== config.storageAccount + '.blob.core.windows.net') throw new Error('SAS destination account mismatch');
      return new BlockBlobClient(signedUrl, undefined, { retryOptions: { maxTries: 1 } });
    };
    const storageCall = async (operation, bodyBytes, record) => {
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          record.storageRequestCount += 1;
          record.storagePayloadBytesAttempted += bodyBytes;
          await operation(AbortSignal.timeout(120000));
          return;
        } catch (error) {
          if (error.statusCode && ![408, 429, 500, 502, 503, 504].includes(error.statusCode)) throw new Error(`Storage HTTP ${error.statusCode}`);
          if (attempt === 2) throw new Error('Storage request failed after three attempts');
        }
        record.storageRetries += 1;
        await new Promise(resolve => setTimeout(resolve, 500 * 2 ** attempt));
      }
    };

    async function original(file, initialized, record) {
      const started = now();
      const client = clientFor(initialized.blobUrl);
      const ids = [];
      let blockCursor = 0;
      const count = Math.ceil(file.size / BLOCK_BYTES);
      for (let index = 0; index < count; index += 1) ids.push(btoa('block-' + String(index).padStart(8, '0')));
      await Promise.all(Array.from({ length: Math.min(2, count) }, async () => {
        while (blockCursor < count) {
          const index = blockCursor++;
          const block = file.slice(index * BLOCK_BYTES, (index + 1) * BLOCK_BYTES);
          await blockGate.run(() => storageCall(signal => client.stageBlock(ids[index], block, block.size, { abortSignal: signal }), block.size, record));
        }
      }));
      await blockGate.run(() => storageCall(signal => client.commitBlockList(ids, { abortSignal: signal, blobHTTPHeaders: { blobContentType: file.type || 'application/octet-stream' } }), 0, record));
      record.originalStorageMs = now() - started;
      record.originalCommittedAtMs = now() - began;
      latestOriginalCommitMs = Math.max(latestOriginalCommitMs, record.originalCommittedAtMs);
      latestAssetCommitMs = Math.max(latestAssetCommitMs, record.originalCommittedAtMs);
    }

    async function process(index, advance) {
      const file = files[index];
      const started = now();
      const extension = file.name.includes('.') ? file.name.split('.').pop().toLowerCase() : '';
      const filename = config.prefix + String(index).padStart(5, '0') + '.' + extension;
      const record = { index, sourceFilename: file.name, filename, arm: config.arm, originalBytes: file.size, startedAtMs: started - began, storageRequestCount: 0, storagePayloadBytesAttempted: 0, storageRetries: 0 };
      await recordBenchmark({ event: 'started', ...record });
      try {
        const hashStart = now();
        const hash = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
        const sha256 = [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, '0')).join('');
        record.hashMs = now() - hashStart;
        const initStart = now();
        const benchmark = { runId: config.runId, arm: config.arm };
        const initialized = await init({ filename, totalSize: file.size, sha256, directToBlob: true, includePreview: config.arm === 'derivatives', benchmark });
        record.initMs = now() - initStart;
        record.blobName = initialized.blobName;
        if (!initialized.blobName || !initialized.blobUrl || !initialized.processingIsolated) throw new Error('Upload initialization missing isolated blob identity');
        const payload = { filename, totalSize: file.size, sha256, contentType: file.type || 'application/octet-stream', uploadId: initialized.uploadId, blobName: initialized.blobName, clientAssetId: filename, clientLastModified: file.lastModified, benchmark };
        record.sha256 = sha256;
        const transfer = original(file, initialized, record);
        const derivatives = config.arm === 'derivatives' ? (async () => {
          const generated = await sensorGate.run(() => decode(file, config.workerUrl));
          const { previewBlob, thumbnailBlob, ...metadata } = generated;
          record.sensor = metadata;
          if (generated.status !== 'generated') return;
          if (previewBlob.size > 1500000 || Math.max(generated.preview.width, generated.preview.height) > 2048 || Math.max(generated.thumbnail.width, generated.thumbnail.height) > 120) throw new Error('Derivative size constraints violated');
          const previewStart = now();
          if (!initialized.previewBlobUrl) throw new Error('Direct preview upload URL unavailable');
          const previewClient = clientFor(initialized.previewBlobUrl);
          await blockGate.run(() => storageCall(signal => previewClient.upload(previewBlob, previewBlob.size, { abortSignal: signal, blobHTTPHeaders: { blobContentType: 'image/jpeg' } }), previewBlob.size, record));
          record.previewStorageMs = now() - previewStart;
          record.previewCommittedAtMs = now() - began;
          latestAssetCommitMs = Math.max(latestAssetCommitMs, record.previewCommittedAtMs);
          const thumbStart = now();
          if (!initialized.thumbnailBlobUrl) throw new Error('Thumbnail upload URL unavailable');
          const thumbnailClient = clientFor(initialized.thumbnailBlobUrl);
          await blockGate.run(() => storageCall(signal => thumbnailClient.upload(thumbnailBlob, thumbnailBlob.size, { abortSignal: signal, blobHTTPHeaders: { blobContentType: 'image/jpeg' } }), thumbnailBlob.size, record));
          record.thumbnailStorageMs = now() - thumbStart;
          record.thumbnailCommittedAtMs = now() - began;
          latestAssetCommitMs = Math.max(latestAssetCommitMs, record.thumbnailCommittedAtMs);
          payload.clientProcessing = { preview: { alreadyUploaded: true, hasData: true, contentType: 'image/jpeg', sourceKind: 'raw_converted_jpeg', sourceFormat: extension, rawParserVersion: generated.librawVersion, previewWidth: generated.preview.width, previewHeight: generated.preview.height, originalBytes: file.size, sourceBytes: previewBlob.size } };
          // Only claim thumbnail completion after a successful direct storage PUT.
          // Unsupported sensor files omit reports to avoid backend embedded-preview fallbacks.
          payload.clientProcessingReport = [{ step: 'thumbnail', status: 'done', durationMs: generated.workerWallMs, sourceKind: 'raw_converted_jpeg', sourceFormat: extension }];
        })() : Promise.resolve();
        const settled = await Promise.allSettled([transfer, derivatives]);
        const failure = settled.find(item => item.status === 'rejected');
        if (failure) throw failure.reason;
        advance();
        const finalStart = now();
        const result = await finalize(payload);
        record.finalizeMs = now() - finalStart;
        if (!result.complete || result.bytesReceived !== file.size || result.filename !== filename || result.processingIsolated !== true) throw new Error('Finalization did not verify the expected isolated original');
        record.status = 'finalized';
        record.duplicateCount = result.duplicates?.length || 0;
        record.finalizedAtMs = now() - began;
      } catch (error) { record.status = 'failed'; record.error = safeError(error); }
      advance();
      record.endToEndMs = now() - started;
      records.push(record);
      await recordBenchmark({ event: 'finished', ...record });
    }

    // Bound unfinished finalizations while keeping sensor work independent of file lanes.
    await Promise.all(Array.from({ length: 6 }, async () => {
      while (cursor < files.length && !config.shouldStop?.()) {
        const index = cursor++;
        let advance;
        const prepared = new Promise(resolve => { advance = resolve; });
        const task = pendingGate.run(() => process(index, advance));
        completions.push(task);
        task.catch(advance);
        await prepared;
      }
    }));
    await Promise.all(completions);
    return {
      arm: config.arm, prefix: config.prefix, files: files.length, wallMs: now() - began,
      attemptedFiles: records.length, stopped: records.length !== files.length,
      latestOriginalCommitMs, latestAssetCommitMs,
      environment: { userAgent: navigator.userAgent, hardwareConcurrency: navigator.hardwareConcurrency, origin: location.origin },
      settings: { blockBytes: BLOCK_BYTES, globalStorageConcurrency: 6, fileLanes: 6, sensorWorkers: 1, batchMaxFiles: 12, initDebounceMs: 50, finalizeDebounceMs: 200, directPreviewSupported: true, embeddedPreviews: false, clientDedupSkipped: true, processingIsolation: 'persistent' },
      records, apiRequests,
    };
  }


