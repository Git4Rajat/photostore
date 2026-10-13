export function distribution(values) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  const percentile = p => {
    if (!sorted.length) return null;
    const index = (sorted.length - 1) * p;
    const low = Math.floor(index);
    return sorted[low] + (sorted[Math.ceil(index)] - sorted[low]) * (index - low);
  };
  return { count: sorted.length, mean: sorted.length ? sorted.reduce((a, b) => a + b, 0) / sorted.length : null,
    p50: percentile(.5), p90: percentile(.9), p95: percentile(.95), max: sorted.at(-1) ?? null };
}

export function summarize(result) {
  const records = result.records;
  const finalized = records.filter(record => record.status === 'finalized');
  const generated = records.filter(record => record.sensor?.status === 'generated');
  const sensorAttempted = records.filter(record => record.sensor);
  const valid = records.filter(record => record.verification?.verified);
  return {
    arm: result.arm, wallMs: result.wallMs, attempted: records.length, finalized: finalized.length,
    failed: records.length - finalized.length, generated: generated.length, sensorAttempted: sensorAttempted.length, verified: valid.length,
    originalBytesFinalized: finalized.reduce((sum, record) => sum + record.originalBytes, 0),
    previewBytes: generated.reduce((sum, record) => sum + record.sensor.preview.bytes, 0),
    thumbnailBytes: generated.reduce((sum, record) => sum + record.sensor.thumbnail.bytes, 0),
    finalizedSuccessRate: records.length ? finalized.length / records.length : null,
    sensorGenerationSuccessRate: sensorAttempted.length ? generated.length / sensorAttempted.length : null,
    corpusDerivativeCoverage: result.arm === 'derivatives' && records.length ? generated.length / records.length : null,
    averageFileSuccessRate: records.length ? valid.length / records.length : null,
    timingsMs: Object.fromEntries(['hashMs', 'initMs', 'originalStorageMs', 'previewStorageMs', 'thumbnailStorageMs', 'finalizeMs', 'endToEndMs'].map(key => [key, distribution(finalized.map(record => record[key]))])),
    sensorMs: distribution(generated.map(record => record.sensor.workerWallMs)),
    previewSizes: distribution(generated.map(record => record.sensor.preview.bytes)),
    throughputMBps: result.wallMs > 0 ? finalized.reduce((sum, record) => sum + record.originalBytes, 0) / 1000 / result.wallMs : null,
  };
}
