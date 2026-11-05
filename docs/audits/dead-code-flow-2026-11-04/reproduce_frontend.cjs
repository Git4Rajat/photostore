/* Offline audit probes: asserts current defects, not desired behavior.
 * Transpiles unmodified production modules and supplies dependency fakes.
 * Run: node docs/audits/dead-code-flow-2026-11-04/reproduce_frontend.cjs
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../..');
const ts = require(path.join(root, 'frontend/node_modules/typescript'));

function load(relative, modules, globals = {}) {
    const source = fs.readFileSync(path.join(root, relative), 'utf8');
    const compiled = ts.transpileModule(source, {
        compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true},
    }).outputText;
    const exports = {};
    vm.runInNewContext(compiled, {exports, Map, Set, Promise, ...globals, require: name => {
        assert.ok(name in modules, `Missing fake module ${name}`);
        return modules[name];
    }}, {filename: relative});
    return exports;
}

async function run() {
    const results = {};
    const timers = [], calls = [];
    const indexes = load('frontend/src/services/indexBuilding.ts', {
        react: {useSyncExternalStore: (_subscribe, snapshot) => snapshot},
        './apiClient': {
            get: async url => { calls.push({client: 'core', url}); return {ready: true, building: false}; },
            getTools: async url => { calls.push({client: 'indexer', url}); return {ready: true, building: false}; },
        },
    }, {setTimeout: callback => {timers.push(callback); return timers.length;}, clearTimeout: () => {}});
    indexes.reportIndexBuilding('session', true);
    timers.shift()();
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(calls[0].client, 'core');
    results.index_status_poller_uses_core_client = calls;
    const snapshot = indexes.useIndexStatus();
    assert.notEqual(snapshot(), snapshot());
    results.unused_status_hook_has_unstable_snapshot = {sameReference: snapshot() === snapshot()};

    const http = load('frontend/src/services/httpClient.ts', {
        axios: {isAxiosError: e => !!e?.isAxiosError, CanceledError: Error},
        './sessionScope': {sessionScope: () => 'same-account-and-library'},
        './authClient': {getAccessToken: async () => null, isAuthEnabled: () => false},
        './backendStatus': {reportBackendReachable: () => {}, reportBackendUnreachable: () => {}},
        './apiError': {newRequestId: () => 'audit', classifyApiError: e => e},
        './sessionExpiry': {reportUnauthorized: () => {}},
        './perf': {perfEnabled: () => false, perfNow: () => 0},
    }, {setTimeout: callback => {callback(); return 1;}});

    let attempts = 0, liked = false;
    const client = {defaults: {}, post: async () => {
        attempts++;
        liked = !liked; // server applied the non-idempotent toggle
        if (attempts === 1) throw Object.assign(new Error('Response lost after server commit'), {isAxiosError: true, code: 'ERR_NETWORK'});
        return {data: {liked}, status: 200};
    }};
    const response = await http.requestJson(client, 'post', '/photos/photo.jpg/like', {});
    assert.equal(attempts, 2);
    assert.equal(response.liked, false);
    results.lost_mutation_response_replays_toggle = {attempts, finalLiked: response.liked};

    let gets = 0, resolveGet;
    const getClient = {defaults: {}, get: async (_url, config) => {
        gets++;
        await new Promise(resolve => {resolveGet = resolve;});
        return {data: {page: config.params.page}, status: 200};
    }};
    const first = http.requestJson(getClient, 'get', '/example', undefined, {params: {page: 1}});
    const second = http.requestJson(getClient, 'get', '/example', undefined, {params: {page: 2}});
    resolveGet();
    const answers = await Promise.all([first, second]);
    assert.equal(gets, 1);
    assert.equal(answers[1].page, 1);
    results.latent_get_dedup_ignores_config_params = {networkCalls: gets, returnedPages: answers.map(a => a.page)};
    console.log(JSON.stringify(results, null, 2));
}

run().catch(error => {console.error(error); process.exitCode = 1;});
