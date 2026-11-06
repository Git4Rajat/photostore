class MockIntersectionObserver implements IntersectionObserver {
  readonly root: Element | Document | null = null;
  readonly rootMargin: string = '0px';
  readonly thresholds: ReadonlyArray<number> = [0];

  disconnect(): void {}
  observe(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
  unobserve(): void {}
}

(globalThis as unknown as { IntersectionObserver: typeof IntersectionObserver }).IntersectionObserver =
  MockIntersectionObserver as unknown as typeof IntersectionObserver;

const originalWarn = console.warn;
console.warn = (...args: unknown[]) => {
  const message = String(args[0] ?? '');
  if (
    message.includes('No API base URL configured') ||
    message.includes('React Router Future Flag Warning')
  ) {
    return;
  }
  originalWarn(...args);
};

// Node 25+ ships its own experimental `localStorage` global that, without
// --localstorage-file, is undefined and shadows jsdom's. Give tests a working
// in-memory Storage whenever the one in scope is unusable.
const installMemoryStorage = (name: 'localStorage' | 'sessionStorage') => {
  const usable = (): boolean => {
    try {
      const storage = (globalThis as unknown as Record<string, Storage | undefined>)[name];
      return Boolean(storage && typeof storage.getItem === 'function');
    } catch {
      return false;
    }
  };
  if (usable()) return;
  const data = new Map<string, string>();
  const memory: Storage = {
    get length() { return data.size; },
    clear: () => data.clear(),
    getItem: (key) => (data.has(String(key)) ? data.get(String(key))! : null),
    key: (index) => Array.from(data.keys())[index] ?? null,
    removeItem: (key) => { data.delete(String(key)); },
    setItem: (key, value) => { data.set(String(key), String(value)); },
  };
  for (const target of typeof window === 'undefined' ? [globalThis] : [globalThis, window]) {
    Object.defineProperty(target, name, { value: memory, configurable: true, writable: true });
  }
};
installMemoryStorage('localStorage');
installMemoryStorage('sessionStorage');

// jsdom gives every test file a fresh window; a shared in-memory Storage must
// not leak one test's persisted state into the next.
beforeEach(() => {
  globalThis.localStorage?.clear();
  globalThis.sessionStorage?.clear();
});

export {};
