# Large video file-read failure — 2026-10-25

## Reproduction

File: `Free ProRes Log Stock Footage 21.MOV`, 2,381,066,480 bytes, from the
local ImageDataSet video corpus.

Headless Google Chrome could read an 8 MiB slice of the selected file, but
`file.arrayBuffer()` failed with `NotReadableError` and the exact reported
message: "The requested file could not be read, typically due to permission
problems that have occurred after a reference to a file was acquired."

The upload path called `readBlobArrayBuffer(file)` before WebCrypto SHA-256,
requiring the entire video in a single buffer before upload initialization.
This reproduces the error with a readable local file; reselection alone does
not address that allocation.

## Change

Files larger than 64 MiB now use incremental SHA-256 with `hash-wasm` 4.12.0,
reading 8 MiB slices. Smaller files retain the existing WebCrypto path. The
digest remains SHA-256 over the complete original byte stream, preserving the
existing deduplication and backend hash contract. The library loads on demand
for large files. Existing frontend CSP permits WebAssembly.

Hashing checks the upload abort signal between reads and before returning a
digest. A short read fails explicitly; genuine file permission/read errors
still propagate.

## Verification

- The exact local video hashed successfully in Chrome in 9,427 ms.
- Browser digest matched an independent Node SHA-256 file stream:
  `e65e820c115df899157041c5188ad53ffba54ff95d398da70cb87e9c80d1045c`.
- Six utility regressions cover chunk boundaries, whole-file-read avoidance,
  empty input, cancellation, genuine read errors, and incomplete reads.
- Two provider regressions verify large-file hash routing and Stop during
  hashing without upload initialization/finalization.
- Upload provider, wave client/synchronization/operations, and new hashing
  suites: **87 passed**. TypeScript checking passed.
- Reusable browser check: `frontend/scripts/smoke-large-file-hash.mjs`, taking
  the local file path as its argument and `PLAYWRIGHT_MODULE_PATH` if needed.

This validates local file reading and hashing, not a full 2.2 GB network upload.
No deployment was performed.
