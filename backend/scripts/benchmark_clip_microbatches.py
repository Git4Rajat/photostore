"""Offline CPU CLIP image microbatch experiment; no app/storage writes.

Uses the real image tower, preprocessing and production tag scoring. All
configurations use the same worker previews. Timings include decoding, tensor
preparation, inference and tagging, but exclude preview creation/model loading.
No filenames, vectors, OCR text or images are written to the report.
"""
import argparse
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import resource
import time
from unittest.mock import patch

from benchmark_vision_stages import distribution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--batch-sizes', type=int, nargs='+', default=[1, 2, 4, 8])
    args = parser.parse_args()
    if args.rounds < 2 or min(args.batch_sizes) < 1 or 1 not in args.batch_sizes:
        parser.error('Use at least two rounds, positive batch sizes and a batch-1 control')

    import numpy as np
    from PIL import Image, ImageOps
    import torch
    from threadpoolctl import threadpool_info
    from image_utils import convert_image_to_jpeg
    from ipwork_image import image_job
    import ipwork_vision as vision
    import vision_utils as vu

    if vu._device() != 'cpu':
        raise RuntimeError('This harness measures CPU inference only')
    if not vu.image_encoder_available() or not vision._load_vocabulary():
        raise RuntimeError('Real CLIP model and tag vocabulary required')
    previews = []
    for item in json.loads(args.manifest.read_text()):
        path = Path(item['path'])
        preview = convert_image_to_jpeg(path.read_bytes(), item.get('filename', path.name))
        if not preview:
            raise RuntimeError('Input preview conversion failed')
        previews.append(preview)
    if not previews:
        parser.error('manifest is empty')

    # Baseline uses the actual production single-image encoder and tagger.
    # Experimental vectors are then fed to that same tagger, not a reimplemented
    # approximation. Patching is scoped to this single-threaded offline process.
    def score(vector):
        with image_job(False), patch.object(vu, 'encode_image_embedding', return_value=vector.tolist()):
            result = vision.process_vision('offline-benchmark', 'preview.jpg', b'')
        if result.get('error'):
            raise RuntimeError('Production scoring failed')
        return result

    with image_job(False):
        reference = [vision.process_vision('offline-benchmark', 'preview.jpg', data) for data in previews]
    if any(r.get('error') or not r.get('imageEmbedding') for r in reference):
        raise RuntimeError('Production single-image reference failed')
    expected = np.asarray([r['imageEmbedding'] for r in reference], dtype=np.float32)

    def run_batch(data):
        timings = {}
        started = time.perf_counter()
        tensors = []
        for raw in data:
            with Image.open(io.BytesIO(raw)) as image:
                with ImageOps.exif_transpose(image) as oriented:
                    with oriented.convert('RGB') as rgb:
                        tensors.append(vu._PREPROCESS(rgb))
        pixels = torch.stack(tensors).to(vu._device())
        timings['decode_preprocess_stack'] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        with torch.no_grad(), vu._MODEL_LOCK:
            vectors = vu._MODEL.encode_image(pixels)
            vectors = vectors / vectors.norm(dim=-1, keepdim=True)
        vectors = vectors.cpu().numpy()
        timings['inference_normalize'] = (time.perf_counter() - started) * 1000
        if vectors.shape != (len(data), expected.shape[1]) or not np.all(np.isfinite(vectors)):
            raise RuntimeError('Invalid batch output')
        started = time.perf_counter()
        scored = [score(vector) for vector in vectors]
        timings['production_scoring'] = (time.perf_counter() - started) * 1000
        return vectors, scored, timings

    # Warm each tensor shape, including any partial last batch, before timing.
    for size in sorted(set(args.batch_sizes + [len(previews) % n for n in args.batch_sizes]) - {0}):
        run_batch(previews[:size])
    reports = []
    for round_index in range(args.rounds):
        sizes = args.batch_sizes if round_index % 2 == 0 else args.batch_sizes[::-1]
        for size in sizes:
            cpu_before, start = time.process_time(), time.perf_counter()
            timings, vectors, scored, latencies, amortized = [], [], [], [], []
            for offset in range(0, len(previews), size):
                batch = previews[offset:offset + size]
                tick = time.perf_counter()
                features, results, phases = run_batch(batch)
                elapsed = (time.perf_counter() - tick) * 1000
                latencies.extend([elapsed] * len(batch))
                amortized.extend([elapsed / len(batch)] * len(batch))
                timings.append(phases)
                vectors.extend(features)
                scored.extend(results)
            wall = time.perf_counter() - start
            cpu = time.process_time() - cpu_before
            actual = np.asarray(vectors)
            norms = np.linalg.norm(actual, axis=1) * np.linalg.norm(expected, axis=1)
            cosine = np.sum(actual * expected, axis=1) / norms
            same_fields = ('tags', 'aiPersonCandidate', 'aiPersonLabel')
            changed = [i for i, result in enumerate(scored)
                       if any(result[k] != reference[i][k] for k in same_fields)]
            report = {
                'round': round_index, 'batch_size': size, 'wall_seconds': wall,
                'cpu_seconds': cpu, 'wall_ms_per_image': wall * 1000 / len(previews),
                'images_per_second': len(previews) / wall,
                'amortized_service_ms_per_image': distribution(amortized),
                'image_batch_service_latency_ms': distribution(latencies),
                'phase_ms_per_image': {k: sum(t[k] for t in timings) / len(previews)
                                       for k in timings[0]},
                'max_embedding_abs_error': float(np.max(np.abs(actual - expected))),
                'min_embedding_cosine': float(np.min(cosine)),
                'changed_tag_or_person_samples': changed,
                'max_top_prediction_score_error': max(
                    abs(a['score'] - b['score']) for r, ref in zip(scored, reference)
                    for a, b in zip(r['predictions'], ref['predictions']) if a['label'] == b['label']),
                'vector_sha256': hashlib.sha256(actual.tobytes()).hexdigest(),
                'peak_process_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss *
                                          (1 if platform.system() == 'Darwin' else 1024),
            }
            reports.append(report)
            print(json.dumps(report), flush=True)
            args.output.write_text(json.dumps({
                'platform': platform.platform(), 'python': platform.python_version(),
                'device': vu._device(), 'torch_threads': torch.get_num_threads(),
                'versions': {n: importlib.metadata.version(n) for n in ('torch', 'open_clip_torch', 'numpy')},
                'native_threadpools': threadpool_info(),
                'thread_environment': {k: os.environ.get(k) for k in
                                       ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'OMP_THREAD_LIMIT', 'VECLIB_MAXIMUM_THREADS')},
                'sample_count': len(previews),
                'input_preview_sha256': [hashlib.sha256(data).hexdigest() for data in previews],
                'reference_vector_sha256': hashlib.sha256(expected.tobytes()).hexdigest(),
                'notes': ['Serial batches with no queue formation delay or competing source workers.',
                          'Amortized per-image cost is not individual image latency.',
                          'RSS is cumulative process high-water mark, not per-configuration allocation.',
                          'Output consistency is measured on this corpus, not general model accuracy.'],
                'reports': reports,
            }, indent=2))


if __name__ == '__main__':
    main()
