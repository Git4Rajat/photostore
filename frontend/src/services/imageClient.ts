import { useEffect, useRef, useState } from 'react';
import { resolveApiUrl } from './apiClient';
import { getAccessToken, isAuthEnabled } from './authClient';

const DEFAULT_MAX_PROTECTED_IMAGE_REQUESTS = 4;

export const fetchProtectedBlobUrl = async (path: string): Promise<string> => {
  const url = resolveApiUrl(path);
  const headers: Record<string, string> = {};
  if (isAuthEnabled()) {
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

export const useProtectedBlobUrls = (paths: string[], maxConcurrent = DEFAULT_MAX_PROTECTED_IMAGE_REQUESTS) => {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const urlsRef = useRef<Record<string, string>>({});
  const createdObjectUrls = useRef<string[]>([]);

  useEffect(() => {
    urlsRef.current = urls;
  }, [urls]);

  useEffect(() => {
    if (!isAuthEnabled() || paths.length === 0) {
      return undefined;
    }

    let active = true;
    const uniquePaths = Array.from(new Set(paths.filter((path): path is string => Boolean(path))));
    let cursor = 0;

    const loadNext = async () => {
      while (active && cursor < uniquePaths.length) {
        const path = uniquePaths[cursor];
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

    const workerCount = Math.min(Math.max(1, maxConcurrent), uniquePaths.length);
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
