import { distribution, summarize } from './results';

test('percentiles interpolate and ignore missing timings', () => {
  expect(distribution([10, 20, 30, undefined, NaN])).toEqual({ count: 3, mean: 20, p50: 20, p90: 28, p95: 29, max: 30 });
  expect(distribution([]).p50).toBeNull();
});

test('generation, finalization and verification denominators remain separate', () => {
  const result = { arm: 'derivatives', wallMs: 1000, records: [
    { status: 'finalized', originalBytes: 1000, endToEndMs: 20, verification: { verified: true }, sensor: { status: 'generated', workerWallMs: 10, preview: { bytes: 50 }, thumbnail: { bytes: 5 } } },
    { status: 'finalized', originalBytes: 2000, endToEndMs: 30, sensor: { status: 'failed' } },
    { status: 'failed', originalBytes: 3000 },
  ] };
  const summary = summarize(result);
  expect(summary.finalizedSuccessRate).toBe(2 / 3);
  expect(summary.sensorGenerationSuccessRate).toBe(1 / 2);
  expect(summary.corpusDerivativeCoverage).toBe(1 / 3);
  expect(summary.averageFileSuccessRate).toBe(1 / 3);
  expect(summary.timingsMs.endToEndMs.count).toBe(2);
});
