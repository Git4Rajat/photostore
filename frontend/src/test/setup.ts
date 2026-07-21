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

export {};
