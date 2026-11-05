"""Offline YOLO CPU microbatches using existing weights and preprocessing.

Measures detector tensor preparation and forward inference only. Decoding and
production box filtering/NMS are outside timing. Uses actual production
postprocessing to validate batched outputs against single-image detections.
Does not change the production single-image model/output contract.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import time
from unittest.mock import patch

from benchmark_vision_stages import distribution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--rounds', type=int, default=4)
    args = parser.parse_args()
    if args.rounds < 2:
        parser.error('Use at least two rounds')
    import cv2
    import numpy as np
    import ipwork_face as face
    from image_utils import convert_image_to_jpeg
    from threadpoolctl import threadpool_info

    samples, hashes = [], []
    for item in json.loads(args.manifest.read_text()):
        path = Path(item['path'])
        preview = convert_image_to_jpeg(path.read_bytes(), item.get('filename', path.name))
        if not preview:
            raise RuntimeError('Input preview conversion failed')
        pixels = cv2.imdecode(np.frombuffer(preview, np.uint8), cv2.IMREAD_COLOR)
        if pixels is None:
            raise RuntimeError('Preview decode failed')
        samples.append(pixels)
        hashes.append(hashlib.sha256(preview).hexdigest())
    if not samples:
        parser.error('manifest is empty')
    session = face._get_yolo_session()
    input_name, output_name = session.get_inputs()[0].name, session.get_outputs()[0].name
    if session.get_providers() != ['CPUExecutionProvider']:
        raise RuntimeError('This harness requires the CPU execution provider')

    def tensor(image):
        boxed, _, _, _ = face._letterbox(image, face.YOLO_INPUT_SIZE)
        rgb = cv2.cvtColor(boxed, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        return np.transpose(rgb, (2, 0, 1))

    def infer(images):
        start = time.perf_counter()
        batch = np.stack([tensor(image) for image in images])
        preprocess = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        raw = session.run([output_name], {input_name: batch})[0]
        inference = (time.perf_counter() - start) * 1000
        if raw.ndim != 3 or raw.shape[0] != len(images) or raw.shape[1] != 5 or not np.all(np.isfinite(raw)):
            raise RuntimeError('Invalid batched detector output')
        return raw, preprocess, inference

    # Reuse all production validation, thresholding, coordinate conversion and
    # NMS. The fake session supplies one already-inferred output, and is used
    # only outside timed sections in this single-threaded offline experiment.
    class RecordedSession:
        def __init__(self, raw):
            self.raw = raw

        def get_inputs(self):
            return session.get_inputs()

        def get_outputs(self):
            return session.get_outputs()

        def run(self, *_):
            return [self.raw]

    def detections(image, raw):
        with patch.object(face, '_get_yolo_session', return_value=RecordedSession(raw)):
            return face.detect_faces(image)

    expected = [infer([image])[0] for image in samples]
    expected_boxes = [detections(image, raw) for image, raw in zip(samples, expected)]
    for size in (1, 2, 4, 8):
        infer(samples[:size])
    reports = []
    for round_index in range(args.rounds):
        for size in ((1, 2, 4, 8) if round_index % 2 == 0 else (8, 4, 2, 1)):
            cpu, started = time.process_time(), time.perf_counter()
            outputs, prep, forwards, amortized, latencies = [], [], [], [], []
            for offset in range(0, len(samples), size):
                images = samples[offset:offset + size]
                tick = time.perf_counter()
                raw, p, f = infer(images)
                ms = (time.perf_counter() - tick) * 1000
                prep.append(p)
                forwards.append(f)
                amortized.extend([ms / len(images)] * len(images))
                latencies.extend([ms] * len(images))
                outputs.extend(raw[i:i + 1] for i in range(len(images)))
            wall, cpu_seconds = time.perf_counter() - started, time.process_time() - cpu
            changed, coordinate_errors, score_errors = [], [], []
            for i, (image, raw) in enumerate(zip(samples, outputs)):
                boxes = detections(image, raw)
                baseline = expected_boxes[i]
                if len(boxes) != len(baseline):
                    changed.append(i)
                    continue
                coords = [abs(a[k] - b[k]) for a, b in zip(boxes, baseline)
                          for k in ('left', 'top', 'width', 'height')]
                scores = [abs(a['score'] - b['score']) for a, b in zip(boxes, baseline)]
                coordinate_errors.extend(coords)
                score_errors.extend(scores)
                if max(coords, default=0) > 0.001 or max(scores, default=0) > 0.00001:
                    changed.append(i)
            report = {
                'round': round_index, 'batch_size': size, 'wall_seconds': wall,
                'cpu_seconds': cpu_seconds, 'wall_ms_per_image': wall * 1000 / len(samples),
                'preprocess_ms_per_image': sum(prep) / len(samples),
                'inference_ms_per_image': sum(forwards) / len(samples),
                'amortized_service_ms_per_image': distribution(amortized),
                'image_batch_service_latency_ms': distribution(latencies),
                'max_raw_abs_error': max(float(np.max(np.abs(a - b))) for a, b in zip(outputs, expected)),
                'changed_detection_samples': changed,
                'max_box_coordinate_error_pixels': max(coordinate_errors, default=0),
                'max_box_score_error': max(score_errors, default=0),
            }
            reports.append(report)
            print(json.dumps(report), flush=True)
            args.output.write_text(json.dumps({
                'platform': platform.platform(), 'python': platform.python_version(),
                'versions': {n: importlib.metadata.version(n) for n in ('onnxruntime', 'numpy')},
                'native_threadpools': threadpool_info(),
                'sample_count': len(samples), 'input_preview_sha256': hashes,
                'input_shape': session.get_inputs()[0].shape,
                'provider': session.get_providers(),
                'reference_detected_faces': sum(map(len, expected_boxes)),
                'notes': ['Detector preparation and inference only; excludes JPEG decode and postprocessing.',
                          'Production NMS/box validation compared outside timed sections.',
                          'Serial batches exclude queue formation delay and competing source workers.',
                          'Box equality tolerance: 0.001 pixels and 0.00001 confidence.',
                          'Whole-pipeline recovery detection and landmarks are not batched here.'],
                'reports': reports,
            }, indent=2))


if __name__ == '__main__':
    main()
