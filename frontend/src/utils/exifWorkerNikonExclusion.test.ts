import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {createContext, runInContext} from 'node:vm';
import {describe, expect, test} from 'vitest';

const WORKER_SOURCE = readFileSync(resolve(__dirname, '../../public/exiftool-wasm/worker.js'), 'utf8');

async function runWorker(record: Record<string, unknown>) {
    const posted: Array<{ok: boolean; json?: Record<string, unknown>}> = [];
    const indexBytes = new Uint8Array([2, 0, 0, 0, 0x5b, 0x5d]);
    const context: Record<string, unknown> = {
        self: {location: {pathname: '/exiftool-wasm/worker.js'}, postMessage: (message: unknown) => posted.push(message as never)},
        importScripts: () => {},
        fetch: async (url: string) => ({
            arrayBuffer: async () => (url.endsWith('lib.pack') ? indexBytes : new ArrayBuffer(0)),
        }),
        TextDecoder, DataView, Uint8Array, JSON, Promise, console,
        FS: {mkdir: () => {}, writeFile: () => {}, unlink: () => {}},
        ENV: {},
    };
    createContext(context);
    runInContext(WORKER_SOURCE, context);
    const Module = context.Module as {onRuntimeInitialized: () => void; print: (text: string) => void; callMain: (args: string[]) => void};
    Module.callMain = () => Module.print(JSON.stringify([record]));
    Module.onRuntimeInitialized();
    const self = context.self as {__boot: Promise<void>; onmessage: (event: {data: unknown}) => Promise<void>};
    await self.__boot;
    await self.onmessage({data: {id: 1, type: 'parse', bytes: new Uint8Array([0]), name: 'DSC_0001.NEF'}});
    return posted[0];
}

describe('browser ExifTool worker Nikon exclusion', () => {
    test('drops the Nikon maker-note parameters that disagree with native ExifTool', async () => {
        const reply = await runWorker({
            SourceFile: '/in.nef',
            'File:FileType': 'NEF',
            'EXIF:Make': 'NIKON CORPORATION',
            'Nikon:PitchAngle': 26204.17792,
            'Nikon:RollAngle': 41210.13432,
            'NikonCustom:LensControlRing': 228,
            'Nikon:HDR': 'Off',
        });
        expect(reply.ok).toBe(true);
        expect(reply.json).toEqual({SourceFile: '/in.nef', 'File:FileType': 'NEF', 'EXIF:Make': 'NIKON CORPORATION'});
    });

    test('keeps other Nikon tags and every non-Nikon tag', async () => {
        const reply = await runWorker({
            SourceFile: '/in.nef',
            'Nikon:LensType': 'AF-S 24-70mm',
            'Nikon:ISO': 400,
            'Composite:FOV': '71.4205807446502 15.228773642553',
        });
        expect(reply.json).toEqual({
            SourceFile: '/in.nef',
            'Nikon:LensType': 'AF-S 24-70mm',
            'Nikon:ISO': 400,
            'Composite:FOV': '71.4205807446502 15.228773642553',
        });
    });
});
