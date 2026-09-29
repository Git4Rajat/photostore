import { useEffect, useRef, useState } from 'react';
import { resolveApiUrl } from './apiClient';
import { getAccessToken, isAuthEnabled } from './authClient';

const DEFAULT_MAX_PROTECTED_IMAGE_REQUESTS = 4;

export const fetchProtectedBlobUrl = async (path: string): Promise<string> => {
  const url = resolveApiUrl(path);
  const headers: Record<string, string> = {};
  // Absolute URLs are signed storage URLs (SAS): the signature in the query
  // string IS the auth, and Azure rejects requests carrying both a SAS and an
  // Authorization header. Only backend-relative paths need the bearer token.
  const isSignedStorageUrl = /^https?:\/\//i.test(path);
  if (!isSignedStorageUrl && isAuthEnabled()) {
    const token = await getAccessToken();
    if (!token) {
      throw new Error('Authentication required for protected image fetch');
    }
    headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(url, {
    headers,
    mode: 'cors',
    credentials: 'omit',
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Failed to fetch protected image: ${response.status}`);
  }

  const blob = await response.blob();
  return URL.createObjectURL(blob);
};

// Same as fetchProtectedBlobUrl but streams the response so download progress
// can drive a UI indicator (e.g. the viewer's full-resolution loading ring),
// and accepts an AbortSignal so a navigation away can cancel the fetch.
export const fetchProtectedBlobUrlWithProgress = async (
  path: string,
  options: { signal?: AbortSignal; onProgress?: (loadedBytes: number, totalBytes: number) => void } = {},
): Promise<string> => {
  const url = resolveApiUrl(path);
  const headers: Record<string, string> = {};
  const isSignedStorageUrl = /^https?:\/\//i.test(path);
  if (!isSignedStorageUrl && isAuthEnabled()) {
    const token = await getAccessToken();
    if (!token) {
      throw new Error('Authentication required for protected image fetch');
    }
    headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(url, {
    headers,
    mode: 'cors',
    credentials: 'omit',
    signal: options.signal,
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Failed to fetch protected image: ${response.status}`);
  }

  const totalBytes = Number(response.headers.get('Content-Length') || 0);
  if (!response.body || !options.onProgress) {
    const blob = await response.blob();
    return URL.createObjectURL(blob);
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let loadedBytes = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) {
      chunks.push(value);
      loadedBytes += value.byteLength;
      options.onProgress(loadedBytes, totalBytes);
    }
  }
  const blob = new Blob(chunks as BlobPart[], { type: response.headers.get('Content-Type') || 'application/octet-stream' });
  return URL.createObjectURL(blob);
};

export const useProtectedBlobUrls = (paths: string[], maxConcurrent = DEFAULT_MAX_PROTECTED_IMAGE_REQUESTS) => {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const urlsRef = useRef<Record<string, string>>({});
  const createdObjectUrls = useRef<string[]>([]);

  useEffect(() => {
    urlsRef.current = urls;
  }, [urls]);

  useEffect(() => {
    if (paths.length === 0) {
      return undefined;
    }

    let active = true;
    const uniquePaths = Array.from(new Set(paths.filter((path): path is string => Boolean(path))));

    // Absolute (SAS/signed) URLs are already directly loadable by the browser
    // -- pass them straight through instead of round-tripping every one
    // through an authenticated fetch + blob conversion. That round trip is
    // only needed for backend-relative paths, which carry a bearer token an
    // <img src> can't send on its own. This also means covers still render
    // when isAuthEnabled() is false (no token to attach either way), instead
    // of the whole hook silently doing nothing.
    const directPaths = uniquePaths.filter((path) => /^https?:\/\//i.test(path));
    if (directPaths.length) {
      setUrls((prev) => {
        let changed = false;
        const next = { ...prev };
        for (const path of directPaths) {
          if (next[path] !== path) {
            next[path] = path;
            changed = true;
          }
        }
        if (!changed) {
          return prev;
        }
        urlsRef.current = next;
        return next;
      });
    }

    const scopedPaths = uniquePaths.filter((path) => !directPaths.includes(path));
    let cursor = 0;

    const loadNext = async () => {
      while (active && cursor < scopedPaths.length) {
        const path = scopedPaths[cursor];
        cursor += 1;
        if (!path || urlsRef.current[path]) {
          continue;
        }

        try {
          const objectUrl = await fetchProtectedBlobUrl(path);
          if (!active) {
            URL.revokeObjectURL(objectUrl);
            return;
          }
          createdObjectUrls.current.push(objectUrl);
          setUrls((prev) => {
            if (prev[path]) {
              URL.revokeObjectURL(objectUrl);
              return prev;
            }
            const next = {
              ...prev,
              [path]: objectUrl,
            };
            urlsRef.current = next;
            return next;
          });
        } catch {
          // ignore failures; fallback handling will show placeholder or retry later
        }
      }
    };

    const workerCount = Math.min(Math.max(1, maxConcurrent), scopedPaths.length);
    void Promise.all(Array.from({ length: workerCount }, () => loadNext()));

    return () => {
      active = false;
    };
  }, [paths.join('|'), maxConcurrent]);

  useEffect(() => {
    return () => {
      createdObjectUrls.current.forEach((objectUrl) => {
        if (objectUrl.startsWith('blob:')) {
          URL.revokeObjectURL(objectUrl);
        }
      });
    };
  }, []);

  return urls;
};
