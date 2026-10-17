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

let worker: Worker | null = null;
let unavailable = false;
let seq = 0;
const pending = new Map<number, { resolve: Pending; timer: ReturnType<typeof setTimeout>; started: number }>();

function failed(reason: string, started: number): BrowserExifOutcome {
  return { ok: false, reason, ms: Math.round(performance.now() - started) };
}

function capable(): boolean {
  return typeof Worker !== 'undefined' && typeof WebAssembly !== 'undefined';
}

function getWorker(): Worker | null {
  if (worker) return worker;
  if (unavailable || !capable()) return null;
  try {
    // Classic worker (served from public/) — it importScripts the Emscripten glue.
    const w = new Worker(WORKER_URL);
    w.onmessage = (event: MessageEvent) => {
      const data = (event.data || {}) as {
        id?: number; ok?: boolean; json?: Record<string, unknown>; outcome?: string;
      };
      if (typeof data.id !== 'number') return;
      const entry = pending.get(data.id);
      if (!entry) return;
      pending.delete(data.id);
      clearTimeout(entry.timer);
      if (data.ok && data.json) {
        entry.resolve({
          ok: true,
          result: { source: 'browser_exiftool', verifiedComplete: true, exifToolJson: data.json },
        });
      } else {
        entry.resolve(failed(data.outcome || 'parse_error', entry.started));
        recycleWorker();
      }
    };
    w.onerror = () => { markUnavailable(); };
    worker = w;
    return w;
  } catch {
    markUnavailable();
    return null;
  }
}

function markUnavailable(): void {
  unavailable = true;
  recycleWorker('tool_unavailable');
}

// A failed parse leaves the WASM heap wedged ("Out of memory!" on every later
// parse in the same worker), so the next parse always boots a fresh worker.
function recycleWorker(reason = 'worker_recycled'): void {
  try { worker?.terminate(); } catch { /* ignore */ }
  worker = null;
  pending.forEach(({ resolve, timer, started }) => { clearTimeout(timer); resolve(failed(reason, started)); });
  pending.clear();
}

/** Start boot + lib load now (call at upload-start) so the first parse is warm. */
export function prewarmBrowserExifTool(): void {
  const w = getWorker();
  if (w) { try { w.postMessage({ type: 'prewarm' }); } catch { /* ignore */ } }
}

/** Is the in-browser ExifTool usable in this context at all? */
export function browserExifToolAvailable(): boolean {
  return !unavailable && capable();
}

/**
 * Extract EXIF in-browser. Never throws: a failed run reports its reason so the
 * caller can record it and fall back to the server EXIF path.
 */
export async function extractBrowserExif(
  bytes: Uint8Array,
  timeoutMs = 20000,
): Promise<BrowserExifOutcome> {
  const started = performance.now();
  const w = getWorker();
  if (!w) return failed(unavailable ? 'tool_unavailable' : 'unsupported_runtime', started);
  const id = ++seq;
  return new Promise<BrowserExifOutcome>((resolve) => {
    const timer = setTimeout(() => {
      if (!pending.has(id)) return;
      pending.delete(id);
      resolve(failed('timeout', started));
      recycleWorker();
    }, timeoutMs);
    pending.set(id, { resolve, timer, started });
    try {
      // Transfer the buffer to avoid a copy; caller must not reuse `bytes` after.
      w.postMessage({ id, type: 'parse', bytes }, [bytes.buffer]);
    } catch {
      pending.delete(id);
      clearTimeout(timer);
      resolve(failed('post_failed', started));
    }
  });
}
