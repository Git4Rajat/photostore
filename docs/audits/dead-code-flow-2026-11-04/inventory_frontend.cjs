/* Conservative module reachability, including lazy imports and worker URLs.
 * This is not symbol-level tree shaking and does not prove deletion is safe.
 */
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../../..');
const frontend = path.join(root, 'frontend');
const ts = require(path.join(frontend, 'node_modules/typescript'));
const walk = directory => fs.readdirSync(directory, {withFileTypes: true}).flatMap(entry =>
    entry.isDirectory() ? walk(path.join(directory, entry.name)) : [path.join(directory, entry.name)]);
const files = walk(path.join(frontend, 'src')).filter(file =>
    /\.(tsx?|js)$/.test(file) && !/\.(test|spec)\.|\.d\.ts$|\/test\//.test(file));
const members = new Set(files);
const edges = new Map();
const unresolved = [];
const relative = file => path.relative(root, file);
const lines = file => {
    const source = fs.readFileSync(file, 'utf8');
    return source.split('\n').length - (source.endsWith('\n') ? 1 : 0);
};
for (const file of files) {
    const source = fs.readFileSync(file, 'utf8');
    const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
    const references = [];
    function walkNode(node) {
        if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node))
            && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) references.push(node.moduleSpecifier.text);
        if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword
            && node.arguments[0] && ts.isStringLiteral(node.arguments[0])) references.push(node.arguments[0].text);
        if (ts.isNewExpression(node) && node.expression.getText(tree) === 'URL'
            && node.arguments?.[0] && ts.isStringLiteral(node.arguments[0])) references.push(node.arguments[0].text);
        ts.forEachChild(node, walkNode);
    }
    walkNode(tree);
    edges.set(file, references.filter(ref => ref.startsWith('.')).flatMap(ref => {
        const base = path.resolve(path.dirname(file), ref.split('?')[0]);
        const found = [base, ...['.ts', '.tsx', '.js', '/index.ts', '/index.tsx'].map(ext => base + ext)]
            .find(candidate => members.has(candidate));
        if (!found && !/\.(css|svg|json|wasm)$/.test(base)) unresolved.push({file: relative(file), reference: ref});
        return found ? [found] : [];
    }));
}
const entrypoints = ['index.html', 'mockups.html', 'benchmark.html'].flatMap(html =>
    [...fs.readFileSync(path.join(frontend, html), 'utf8').matchAll(/<script[^>]+src="([^"]+)"/g)]
        .map(match => path.resolve(frontend, match[1].replace(/^\//, ''))).filter(file => members.has(file)));
const reached = new Set();
function walkGraph(file) {
    if (reached.has(file)) return;
    reached.add(file);
    (edges.get(file) || []).forEach(walkGraph);
}
entrypoints.forEach(walkGraph);
const disconnected = files.filter(file => !reached.has(file));
console.log(JSON.stringify({
    method: 'Conservative source-module graph; includes type imports, lazy imports, worker URLs, and Vite query imports.',
    entrypoints: entrypoints.map(relative),
    sourceFiles: files.length,
    sourceLines: files.reduce((sum, file) => sum + lines(file), 0),
    unreachableFiles: disconnected.length,
    unreachableLines: disconnected.reduce((sum, file) => sum + lines(file), 0),
    unreachable: disconnected.map(file => ({file: relative(file), lines: lines(file)})).sort((a, b) => b.lines - a.lines),
    unresolved,
}, null, 2));
