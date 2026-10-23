/* Source-executing reproduction, not a browser or production-scale test.
 * Run: node docs/audits/index_ready_idle_reproduction.cjs
 * Loads the real TS module with mocked transport and deterministic timers.
 * The `disabled`/`enabled` scenarios capture timer-driven wakeups with no
 * visibility signal (unchanged from before the foreground-demand gate).
 * The `backgrounded`/`returns` scenarios exercise that gate: a document
 * mock with a real visibilitychange event, matching the code's actual
 * addEventListener/removeEventListener/dispatchEvent usage.
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '../..');
const frontendRequire = createRequire(path.join(root, 'frontend/package.json'));
const ts = frontendRequire('typescript');
const source = fs.readFileSync(path.join(root, 'frontend/src/services/indexReadyPush.ts'), 'utf8');
const code = ts.transpileModule(source, {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
}}).outputText;

function makeDocument(initial = 'visible') {
    let visibilityState = initial;
    const listeners = new Set();
    return {
        get visibilityState() { return visibilityState; },
        addEventListener: (type, fn) => { if (type === 'visibilitychange') listeners.add(fn); },
        removeEventListener: (type, fn) => { if (type === 'visibilitychange') listeners.delete(fn); },
        hide() { visibilityState = 'hidden'; listeners.forEach(fn => fn()); },
        show() { visibilityState = 'visible'; listeners.forEach(fn => fn()); },
    };
}

function harness(available, {withDocument = false} = {}) {
    let now = 0, serial = 0, posts = 0, refreshes = 0;
    const tasks = new Map();
    const schedule = (fn, ms, interval = 0) => {
        const id = ++serial;
        tasks.set(id, {fn, at: now + ms, interval});
        return id;
    };
    const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
    class FakeSocket {
        constructor() { schedule(() => this.onopen?.(), 0); }
        close() { this.onclose?.(); }
    }
    const exports = {};
    const dependencies = {
        './apiClient': {postTools: async () => { posts++; return {available, url: available ? 'wss://fake' : undefined}; }},
        './passwordAuthClient': {getActiveLibraryFromToken: () => 'library'},
        './jobNotifications': {isLikelyAuthenticated: () => true},
        './pendingEdits': {pendingEditScope: () => 'account/library'},
    };
    const doc = withDocument ? makeDocument() : undefined;
    // Date.now() must track the simulated clock, not real wall-clock time --
    // the visibility gate's hiddenSince/isGated math (indexReadyPush.ts)
    // reads Date.now(), and this script simulates minutes in milliseconds
    // of real execution.
    vm.runInNewContext(code, {
        exports, navigator: {onLine: true}, WebSocket: FakeSocket, document: doc, Date: {now: () => now},
        require: key => { assert.ok(key in dependencies, `unexpected dependency ${key}`); return dependencies[key]; },
        setTimeout: (fn, ms) => schedule(fn, ms),
        setInterval: (fn, ms) => schedule(fn, ms, ms),
        clearTimeout: id => tasks.delete(id), clearInterval: id => tasks.delete(id),
    }, {filename: 'indexReadyPush.ts'});
    const advance = async end => {
        while (true) {
            const next = [...tasks].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
            if (!next) break;
            const [id, task] = next; now = task.at;
            if (task.interval) tasks.set(id, {...task, at: now + task.interval});
            else tasks.delete(id);
            task.fn(); await flush();
        }
        now = end; await flush();
    };
    const ready = () => { refreshes++; };
    return {exports, doc, advance, flush, ready, posts: () => posts, refreshes: () => refreshes};
}

async function run(available, duration) {
    const h = harness(available);
    const stop = h.exports.startIndexReadyPush(h.ready);
    await h.flush(); await h.advance(duration);
    const observed = {available, observedMinutes: duration / 60000, negotiateRequests: h.posts(), refreshCallbacks: h.refreshes()};
    stop(); const before = h.posts(); await h.advance(duration + 600000);
    assert.equal(h.posts(), before, 'stop failed to cancel network work');
    return observed;
}

async function runVisibility(scenario) {
    const h = harness(true, {withDocument: true});
    const stop = h.exports.startIndexReadyPush(h.ready);
    await h.flush();
    if (scenario === 'backgrounded') {
        h.doc.hide();
        await h.advance(30 * 60000); // stays hidden for 30 simulated minutes
        const observed = {scenario, negotiateRequestsOverThirtyMinutes: h.posts()};
        stop();
        return observed;
    }
    // 'returns': hidden past the gate threshold, then comes back.
    h.doc.hide();
    await h.advance(6 * 60000); // past the 5-minute gate
    const beforeReturn = h.posts();
    h.doc.show();
    await h.flush();
    const observed = {scenario, negotiateRequestsWhileHidden: beforeReturn,
        negotiateRequestsImmediatelyOnReturn: h.posts() - beforeReturn};
    stop();
    return observed;
}

(async () => {
    const disabled = await run(false, 360000);
    const enabled = await run(true, 600000);
    assert.equal(disabled.negotiateRequests, 7);
    assert.equal(enabled.negotiateRequests, 3);
    assert.equal(enabled.refreshCallbacks, 3);

    const backgrounded = await runVisibility('backgrounded');
    const returns = await runVisibility('returns');
    // Un-gated, 30 simulated minutes would negotiate roughly once/minute
    // (~30 calls, matching `disabled`'s ~1/minute rate above); gated, it's
    // bounded by the 30s poll floor only up to the 5-minute threshold.
    assert.ok(backgrounded.negotiateRequestsOverThirtyMinutes <= 10,
        `expected the foreground-demand gate to suppress most of 30 minutes of background negotiation, got ${backgrounded.negotiateRequestsOverThirtyMinutes}`);
    assert.equal(returns.negotiateRequestsImmediatelyOnReturn, 1, 'return to foreground must trigger an immediate catch-up negotiate');

    console.log(JSON.stringify({
        disabled, enabled, backgrounded, returns,
        note: 'disabled/enabled reproduce timer-driven requests with no visibility signal (unchanged); '
            + 'backgrounded/returns exercise the foreground-demand gate added for step 7.',
    }, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
