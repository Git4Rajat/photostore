/**
 * Client for the in-browser ExifTool (WebPerl/WASM) worker.
 *
 * Produces the authoritative EXIF verdict the backend trusts: because the worker
 * runs the SAME ExifTool version the backend pins, a successful parse lets the
 * backend store the metadata WITHOUT ever fetching the original from storage.
 *
 *  - Returns a {@link BrowserExifResult} on a clean run (even if the file has no
 *    EXIF, or the format is unsupported — the backend classifies the JSON).
 *  - Returns `null` when the tool could not load/start/parse. The caller must
 *    then fall back to the existing path (which leaves the server EXIF fallback
 *    in charge) — i.e. `null` means "no authoritative verdict", never "no EXIF".
 *
 * The bundle (~7 MB brotli) is a one-time cached asset; {@link prewarmBrowserExifTool}
 * kicks off boot at upload-start so it's warm by the time preparation needs it.
 */

const WORKER_URL = '/exiftool-wasm/worker.js';

/** The exact field the backend's `_apply_browser_exiftool_result` consumes. */
export interface BrowserExifResult {
  source: 'browser_exiftool';
  verifiedComplete: true;
  /** Raw `exiftool -j -G1 -n` object; the backend normalizes it (single source of truth). */
  exifToolJson: Record<string, unknown>;
}

export type BrowserExifOutcome =
  | { ok: true; result: BrowserExifResult }
  | { ok: false; reason: string; ms: number };

type Pending = (value: BrowserExifOutcome) => void;

let unavailable = false;
let seq = 0;
const RECYCLE_AFTER_PARSES = 25;
// Boot the replacement this many parses before the recycle. Booting is ~30MB of
// fetch plus unpacking; started late, every photo's EXIF step queues behind it.
const STANDBY_LEAD_PARSES = 12;
// Each worker is one single-threaded WASM Perl, ~0.3-0.6s per photo, so one worker caps an
// upload near 100-200 files/min however many lanes feed it (HARs 2026-11-26). Each slot is an
// independent worker with its own queue, recycle and standby. Memory: ~30MB of loaded files
// per instance plus working set, and a slot briefly holds two (current + standby).
const MAX_SLOTS = 4;
let slotCount = 1;

function failed(reason: string, started: number): BrowserExifOutcome {
  return { ok: false, reason, ms: Math.round(performance.now() - started) };
}

function capable(): boolean {
  return typeof Worker !== 'undefined' && typeof WebAssembly !== 'undefined';
}

class ExifSlot {
  private worker: Worker | null = null;
  // A replacement booted a few parses ahead of the recycle, so the swap is instant.
  private standby: Worker | null = null;
  private standbyReady = false;
  private parsesSinceBoot = 0;
  private queue: Promise<unknown> = Promise.resolve();
  private pending = new Map<number, { resolve: Pending; timer: ReturnType<typeof setTimeout>; started: number }>();
  /** Requests queued or running on this slot. */
  waiting = 0;

  private create(): Worker | null {
    if (unavailable || !capable()) return null;
    try {
      // Classic worker (served from public/) — it importScripts the Emscripten glue.
      const w = new Worker(WORKER_URL);
      w.onmessage = (event: MessageEvent) => {
        const data = (event.data || {}) as {
          type?: string; id?: number; ok?: boolean; json?: Record<string, unknown>; outcome?: string;
        };
        if (data.type === 'ready') { if (w === this.standby) this.standbyReady = true; return; }
        if (typeof data.id !== 'number') return;
        const entry = this.pending.get(data.id);
        if (!entry) return;
        this.pending.delete(data.id);
        clearTimeout(entry.timer);
        if (data.ok && data.json) {
          entry.resolve({
            ok: true,
            result: { source: 'browser_exiftool', verifiedComplete: true, exifToolJson: data.json },
          });
        } else {
          entry.resolve(failed(data.outcome || 'parse_error', entry.started));
          this.recycle();
        }
      };
      w.onerror = () => {
        // A standby that fails to boot is just dropped; the next parse boots on demand.
        if (w === this.standby) { this.standby = null; this.standbyReady = false; try { w.terminate(); } catch { /* ignore */ } return; }
        markUnavailable();
      };
      return w;
    } catch {
      markUnavailable();
      return null;
    }
  }

  private getWorker(): Worker | null {
    if (this.worker) return this.worker;
    // Promote the pre-booted replacement if there is one, else boot now.
    this.worker = this.standby ?? this.create();
    this.standby = null;
    this.standbyReady = false;
    if (this.worker) this.parsesSinceBoot = 0;
    return this.worker;
  }

  private ensureStandby(): void {
    if (this.standby || unavailable) return;
    this.standby = this.create();
    if (this.standby) { try { this.standby.postMessage({ type: 'prewarm' }); } catch { /* ignore */ } }
  }

  // A failed parse leaves the WASM heap wedged ("Out of memory!" on every later
  // parse in the same worker), so the next parse always boots a fresh worker.
  recycle(reason = 'worker_recycled'): void {
    try { this.worker?.terminate(); } catch { /* ignore */ }
    this.worker = null;
    this.pending.forEach(({ resolve, timer, started }) => { clearTimeout(timer); resolve(failed(reason, started)); });
    this.pending.clear();
  }

  shutdown(): void {
    try { this.standby?.terminate(); } catch { /* ignore */ }
    this.standby = null;
    this.standbyReady = false;
    this.recycle('tool_unavailable');
  }

  prewarm(): void {
    const w = this.getWorker();
    if (w) { try { w.postMessage({ type: 'prewarm' }); } catch { /* ignore */ } }
  }

  /** Requests on one slot run one at a time, so a recycle never fails a neighbouring file. */
  run(bytes: Uint8Array, name: string, timeoutMs: number): Promise<BrowserExifOutcome> {
    this.waiting++;
    const run = this.queue.then(async () => {
      const first = await this.parseOnce(bytes, name, timeoutMs);
      if (first.ok || first.reason !== 'parse_error') return first;
      this.recycle();
      return this.parseOnce(bytes, name, timeoutMs);
    });
    this.queue = run.catch(() => undefined);
    return run.finally(() => { this.waiting--; });
  }

  private parseOnce(bytes: Uint8Array, name: string, timeoutMs: number): Promise<BrowserExifOutcome> {
    const started = performance.now();
    // Only swap once the replacement reports ready; until then the current worker
    // keeps serving, so no file waits on a boot (a few parses past 25 is harmless).
    if (this.parsesSinceBoot >= RECYCLE_AFTER_PARSES && this.standbyReady) this.recycle();
    const w = this.getWorker();
    if (!w) return Promise.resolve(failed(unavailable ? 'tool_unavailable' : 'unsupported_runtime', started));
    this.parsesSinceBoot++;
    if (this.parsesSinceBoot >= RECYCLE_AFTER_PARSES - STANDBY_LEAD_PARSES) this.ensureStandby();
    const id = ++seq;
    return new Promise<BrowserExifOutcome>((resolve) => {
      const timer = setTimeout(() => {
        if (!this.pending.has(id)) return;
        this.pending.delete(id);
        resolve(failed('timeout', started));
        this.recycle();
      }, timeoutMs);
      this.pending.set(id, { resolve, timer, started });
      try {
        w.postMessage({ id, type: 'parse', bytes, name });
      } catch {
        this.pending.delete(id);
        clearTimeout(timer);
        resolve(failed('post_failed', started));
      }
    });
  }
}

const slots: ExifSlot[] = [];

function markUnavailable(): void {
  unavailable = true;
  slots.forEach(slot => slot.shutdown());
}

// An idle slot first; else grow toward slotCount; else the one with the shortest queue.
function pickSlot(): ExifSlot {
  const idle = slots.find(slot => slot.waiting === 0);
  if (idle) return idle;
  if (slots.length < slotCount) {
    const slot = new ExifSlot();
    slots.push(slot);
    return slot;
  }
  return slots.reduce((best, slot) => (slot.waiting < best.waiting ? slot : best));
}

/** How many ExifTool workers may run at once (1 to MAX_SLOTS); slots are created as load needs them. */
export function setBrowserExifWorkers(count: number): void {
  slotCount = Math.max(1, Math.min(MAX_SLOTS, Math.floor(count) || 1));
}

/** Start boot + lib load now (call at upload-start) so the first parse is warm. */
export function prewarmBrowserExifTool(): void {
  pickSlot().prewarm();
}

/** Is the in-browser ExifTool usable in this context at all? */
export function browserExifToolAvailable(): boolean {
  return !unavailable && capable();
}

/**
 * Extract EXIF in-browser. Never throws: a failed run reports its reason so the
 * caller can record it and fall back to the server EXIF path.
 *
 * A `parse_error` is retried once on a fresh worker, and each worker is recycled
 * every RECYCLE_AFTER_PARSES parses because each parse leaves a little WASM
 * call-stack behind.
 */
export function extractBrowserExif(
  bytes: Uint8Array,
  name = '',
  timeoutMs = 20000,
): Promise<BrowserExifOutcome> {
  return pickSlot().run(bytes, name, timeoutMs);
}
