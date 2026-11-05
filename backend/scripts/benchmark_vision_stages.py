"""Offline real-model stage benchmark; no app import or storage writes.

Use the worker environment and a JSON list of {path, filename?}. Inputs are
converted to worker previews before timing. Model load and warmup are separate.
Output contains timings and output hashes, never text or embeddings.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import os
import statistics
import time


def cpu_stat():
    path = Path('/sys/fs/cgroup/cpu.stat')
    return {k: int(v) for k, v in (line.split() for line in path.read_text().splitlines())} if path.exists() else {}


def fingerprint(value):
    def clean(item):
        if isinstance(item, dict):
            return {k: clean(v) for k, v in item.items() if k != 'stageTimingsMs'}
        if isinstance(item, list):
            return [clean(v) for v in item]
        return item
    return hashlib.sha256(json.dumps(clean(value), sort_keys=True).encode()).hexdigest()


def distribution(values):
    values = sorted(values)
    return {'mean': statistics.mean(values), 'median': statistics.median(values),
            'p95': values[min(len(values) - 1, int(.95 * len(values)))],
            'min': values[0], 'max': values[-1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--face-landmarker', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--workers', type=int, nargs='+', default=[1, 2])
    parser.add_argument('--shared-decode', choices=['both', 'on', 'off'], default='both')
    parser.add_argument('--warmup-workers', type=int, default=2)
    parser.add_argument('--clip-batch-sizes', type=int, nargs='+', default=[1])
    parser.add_argument('--clip-batch-windows-ms', type=float, nargs='+', default=[2])
    parser.add_argument('--native-threads', type=int,
                        help='Set BLAS/OpenMP thread limits before importing native libraries')
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('rounds must be positive')
    if min(args.workers) < 1 or args.warmup_workers < 1:
        parser.error('worker counts must be positive')
    if not all(1 <= n <= 8 for n in args.clip_batch_sizes) or not all(0 <= n <= 50 for n in args.clip_batch_windows_ms):
        parser.error('Invalid CLIP batch sizes/windows')
    if args.native_threads is not None:
        if args.native_threads < 1:
            parser.error('native thread count must be positive')
        for variable in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS'):
            os.environ[variable] = str(args.native_threads)
    from image_utils import convert_image_to_jpeg
    from ipwork_image import image_job
    import ipwork_face as face
    import ipwork_ocr as ocr
    import ipwork_vision as vision
    import vision_utils
    import processing_metrics as metrics
    from threadpoolctl import threadpool_info
    face.FACE_LANDMARKER_MODEL_PATH = args.face_landmarker
    samples = []
    for row in json.loads(args.manifest.read_text()):
        path = Path(row['path'])
        name = row.get('filename', path.name)
        preview = convert_image_to_jpeg(path.read_bytes(), name)
        if not preview:
            raise RuntimeError(f'Preview failed: {path}')
        samples.append((name, preview))
    if not samples:
        parser.error('manifest is empty')
    started = time.perf_counter()
    if not vision_utils.image_encoder_available() or not vision._load_vocabulary():
        raise RuntimeError('Real CLIP model and vocabulary required')
    model_load_ms = (time.perf_counter() - started) * 1000

    def process(index, shared):
        name, data = samples[index]
        result, durations = {}, {}
        with metrics.accounting() as counter, image_job(shared):
            started = time.perf_counter()
            for stage, function in [('ocr', ocr.process_ocr), ('face', face.process_face),
                                    ('clip', vision.process_vision)]:
                before = time.perf_counter()
                result[stage] = function('offline-benchmark', name, data)
                durations[stage] = (time.perf_counter() - before) * 1000
                if result[stage].get('error') or result[stage].get('faceFailureStage') not in (
                        None, '', 'quality_filter_rejected'):
                    raise RuntimeError(f'{stage} sample {index}: {result[stage]}')
            durations['total'] = (time.perf_counter() - started) * 1000
        diagnostics = result['face']['faceDiagnostics']
        if not diagnostics['detectorModelReady']:
            raise RuntimeError('Real face detector required')
        return {'sample': index, 'ms': durations, 'metrics': counter.snapshot(),
                '_clip_vector': result['clip']['imageEmbedding'],
                '_clip_decisions': (result['clip']['tags'], result['clip']['aiPersonCandidate'], result['clip']['aiPersonLabel']),
                'face_stages_ms': diagnostics['stageTimingsMs'],
                'embedded_count': diagnostics['embeddedCount'],
                'face_count': len(result['face'].get('faces') or []),
                'raw_face_count': result['face'].get('rawFaceCount', 0),
                'text_positive': bool(result['ocr'].get('text')),
                'hash': fingerprint(result)}

    # Warm all face branches on portraits and all resident OCR engines. No
    # first-inference timing is counted as steady-state processing.
    warmup_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.warmup_workers) as pool:
        warmup = list(pool.map(lambda i: process(i, False), range(min(12, len(samples)))))
    warmup_ms = (time.perf_counter() - warmup_start) * 1000
    shared_modes = [False, True] if args.shared_decode == 'both' else [args.shared_decode == 'on']
    configurations = [(workers, shared, size, window)
                      for shared in shared_modes for workers in args.workers
                      for size in args.clip_batch_sizes
                      for window in ([0] if size == 1 else args.clip_batch_windows_ms)]
    reports, reference, clip_reference = [], {}, {}
    for round_index in range(args.rounds):
        for workers, shared, size, window in (configurations if round_index % 2 == 0 else configurations[::-1]):
            cpu_before, stat_before = time.process_time(), cpu_stat()
            started = time.perf_counter()
            with metrics.accounting() as wave_counter, vision_utils.image_embedding_batches(
                    source_workers=workers, batch_size=size, window=window / 1000), ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(copy_context().run, process, i, shared) for i in range(len(samples))]
                rows = [future.result() for future in futures]
            seconds = time.perf_counter() - started
            process_cpu = time.process_time() - cpu_before
            quality_changes, max_error, min_cosine = [], 0., 1.
            import numpy as np
            for row in rows:
                reference.setdefault(row['sample'], row['hash'])
                vector, decisions = row.pop('_clip_vector'), row.pop('_clip_decisions')
                base, base_decisions = clip_reference.setdefault(row['sample'], (vector, decisions))
                actual, expected = np.asarray(vector), np.asarray(base)
                max_error = max(max_error, float(np.max(np.abs(actual - expected))))
                min_cosine = min(min_cosine, float(np.dot(actual, expected) / (np.linalg.norm(actual) * np.linalg.norm(expected))))
                if decisions != base_decisions:
                    quality_changes.append(row['sample'])
            metric_names = ['clip_preprocess_ms', 'clip_lock_wait_ms', 'clip_inference_ms',
                            'ocr_engine_init_ms', 'ocr_recognition_ms']
            report = {'round': round_index, 'workers': workers, 'shared_decode': shared,
                      'clip_batch_size': size, 'clip_batch_window_ms': window,
                      'clip_changed_decision_samples': quality_changes,
                      'clip_max_embedding_abs_error': max_error, 'clip_min_embedding_cosine': min_cosine,
                      'wave_metrics': {k: v for k, v in wave_counter.snapshot().items()
                                       if k.startswith('clip_')},
                      'process_cpu_seconds': process_cpu,
                      'cgroup_delta': {k: v - stat_before.get(k, 0) for k, v in cpu_stat().items()},
                      'wall_seconds': seconds, 'images_per_second': len(rows) / seconds,
                      'per_image_ms': {stage: distribution([r['ms'][stage] for r in rows])
                                       for stage in ('face', 'clip', 'ocr', 'total')},
                      'face_stages_mean_ms': {k: statistics.mean(r['face_stages_ms'][k] for r in rows)
                                              for k in rows[0]['face_stages_ms']},
                      'face_stages_per_image_ms': {k: distribution([r['face_stages_ms'][k] for r in rows])
                                                  for k in rows[0]['face_stages_ms']},
                      'face_embedding_ms_per_embedded_face': (
                          sum(r['face_stages_ms']['embed'] for r in rows) /
                          sum(r['embedded_count'] for r in rows)
                          if sum(r['embedded_count'] for r in rows) else None),
                      'metrics_mean_ms': {k: statistics.mean(r['metrics'].get(k, 0) for r in rows)
                                          for k in metric_names},
                      'face_positive': sum(r['face_count'] > 0 for r in rows),
                      'accepted_faces': sum(r['face_count'] for r in rows),
                      'text_positive': sum(r['text_positive'] for r in rows),
                      'changed_samples': [r['sample'] for r in rows if r['hash'] != reference[r['sample']]],
                      'samples': rows}
            reports.append(report)
            print(json.dumps({k: v for k, v in report.items() if k != 'samples'}), flush=True)
            args.output.write_text(json.dumps({'platform': platform.platform(),
                'python': platform.python_version(), 'device': vision_utils._device(),
                'torch_threads': vision_utils.torch.get_num_threads(),
                'versions': {n: importlib.metadata.version(n) for n in
                             ('torch', 'onnxruntime', 'mediapipe', 'tesserocr', 'open_clip_torch')},
                'sample_count': len(samples), 'clip_load_ms': model_load_ms,
                'input_preview_sha256': [hashlib.sha256(data).hexdigest() for _, data in samples],
                'thread_environment': {k: os.environ.get(k) for k in
                                       ('OMP_THREAD_LIMIT', 'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS')},
                'native_threadpools': threadpool_info(),
                'cpu_quota': Path('/sys/fs/cgroup/cpu.max').read_text() if Path('/sys/fs/cgroup/cpu.max').exists() else None,
                'cpu_model': next((line for line in Path('/proc/cpuinfo').read_text().splitlines()
                                   if line.startswith('model name')), None) if Path('/proc/cpuinfo').exists() else platform.processor(),
                'onnx_inputs': {name: [{'name': value.name, 'shape': value.shape}
                                       for value in session.get_inputs()]
                                for name, session in [('yolo', face._get_yolo_session()),
                                                      ('adaface', face._get_adaface_session())]},
                'warmup_ms': warmup_ms, 'reports': reports}, indent=2))


if __name__ == '__main__':
    main()
