/* Source-executing reproduction, not a browser or production-scale test.
 * Run: node docs/audits/index_ready_idle_reproduction.cjs
 * Loads the real TS module with mocked transport and deterministic timers.
 * Assertions capture CURRENT wakeup behavior, not successful idle policy.
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

async function run(available, duration) {
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
    vm.runInNewContext(code, {
        exports, navigator: {onLine: true}, WebSocket: FakeSocket,
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
    const stop = exports.startIndexReadyPush(() => refreshes++);
    await flush(); await advance(duration);
    const observed = {available, observedMinutes: duration / 60000, negotiateRequests: posts, refreshCallbacks: refreshes};
    stop(); const before = posts; await advance(duration + 600000);
    assert.equal(posts, before, 'stop failed to cancel network work');
    return observed;
}

(async () => {
    const disabled = await run(false, 360000);
    const enabled = await run(true, 600000);
    assert.equal(disabled.negotiateRequests, 7);
    assert.equal(enabled.negotiateRequests, 3);
    assert.equal(enabled.refreshCallbacks, 3);
    console.log(JSON.stringify({disabled, enabled, note: 'Reproduces current timer-driven requests without user activity; not target idle-policy compliance.'}, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
