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

type Pending = (value: BrowserExifResult | null) => void;

let worker: Worker | null = null;
let unavailable = false;
let seq = 0;
const pending = new Map<number, { resolve: Pending; timer: ReturnType<typeof setTimeout> }>();

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
      const data = (event.data || {}) as { id?: number; ok?: boolean; json?: Record<string, unknown> };
      if (typeof data.id !== 'number') return;
      const entry = pending.get(data.id);
      if (!entry) return;
      pending.delete(data.id);
      clearTimeout(entry.timer);
      entry.resolve(
        data.ok && data.json
          ? { source: 'browser_exiftool', verifiedComplete: true, exifToolJson: data.json }
          : null,
      );
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
  try { worker?.terminate(); } catch { /* ignore */ }
  worker = null;
  pending.forEach(({ resolve, timer }) => { clearTimeout(timer); resolve(null); });
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
 * Extract EXIF in-browser. Resolves to the authoritative result, or `null` if the
 * tool is unavailable/failed (→ caller falls back to the server EXIF path).
 */
export async function extractBrowserExif(
  bytes: Uint8Array,
  timeoutMs = 20000,
): Promise<BrowserExifResult | null> {
  const w = getWorker();
  if (!w) return null;
  const id = ++seq;
  return new Promise<BrowserExifResult | null>((resolve) => {
    const timer = setTimeout(() => {
      if (pending.delete(id)) resolve(null); // tool too slow → let the server handle it
    }, timeoutMs);
    pending.set(id, { resolve, timer });
    try {
      // Transfer the buffer to avoid a copy; caller must not reuse `bytes` after.
      w.postMessage({ id, type: 'parse', bytes }, [bytes.buffer]);
    } catch {
      pending.delete(id);
      clearTimeout(timer);
      resolve(null);
    }
  });
}
