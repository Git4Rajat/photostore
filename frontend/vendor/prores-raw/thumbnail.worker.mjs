import createDecoder from './prores-raw.mjs';

// One job per worker. WORKERFS reads bounded slices, never a whole video copy.
self.onmessage = async ({data}) => {
    let readBytes = 0, readCalls = 0;
    const read = FileReaderSync.prototype.readAsArrayBuffer;
    FileReaderSync.prototype.readAsArrayBuffer = function (blob) {
        if (readBytes + blob.size > 64 * 1024 * 1024 || readCalls >= 2048) throw new Error('Video resource guard');
        readBytes += blob.size;
        readCalls++;
        return read.call(this, blob);
    };
    try {
        const module = await createDecoder({locateFile: name => new URL(name, import.meta.url).href,
            print: () => {}, printErr: () => {}});
        module.FS.mkdir('/input');
        module.FS.mount(module.FS.filesystems.WORKERFS, {files: [new File([data.file], 'source.mov')]}, '/input');
        const code = module.ccall('make_prores_thumbnail', 'number', ['string', 'string', 'number'],
            ['/input/source.mov', '/thumbnail.jpg', 1]);
        if (code !== 0) throw new Error([-3, -5].includes(code) ? 'Video resource guard' : 'ProRes RAW decode unsupported');
        const bytes = new Uint8Array(module.FS.readFile('/thumbnail.jpg'));
        if (!bytes.length || bytes.length > 512000) throw new Error('Invalid video derivative');
        self.postMessage({status: 'generated', bytes, readBytes, readCalls}, [bytes.buffer]);
    } catch (error) {
        self.postMessage({status: 'failed', error: error instanceof Error ? error.message : 'ProRes RAW decode failed'});
    } finally {
        FileReaderSync.prototype.readAsArrayBuffer = read;
    }
};
