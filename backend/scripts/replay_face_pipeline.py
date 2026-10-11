#!/usr/bin/env python3
"""Replay locally downloaded photos without changing storage, queues or people.

The manifest is a JSON array with filename, path, status and faceCount fields.
Use the same images/model assets for baseline and candidate runs. Reports omit
embeddings and image content. A saved face is not proof of correct recognition.
Landmark acceptance is not proof that a crop contains a human face. Use
--expect-empty for reviewed negatives and --enable-recovery only for experiments.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_utils import convert_image_to_jpeg
from storage_utils import _client_face_passes_quality_gate
import storage_utils
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--models', type=Path, required=True,
                        help='Directory containing yolo-face/model.onnx and adaface/model.onnx')
    parser.add_argument('--landmarker', type=Path, required=True)
    parser.add_argument('--module', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'ipwork_face.py',
                        help='Face processor source; use a saved copy for the baseline')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--enable-recovery', action='store_true',
                        help='Explicitly enable the experimental recovery path for local evaluation')
    parser.add_argument('--expect-empty', action='store_true',
                        help='Negative-test gate: exit 1 for any accepted face or inference error')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= 10000:
        parser.error('Manifest must contain 1–10000 photo entries')
    names = [row['filename'] for row in manifest]
    if len(names) != len(set(names)):
        parser.error('Manifest filenames must be unique')
    for row in manifest:
        if not Path(row['path']).is_file():
            parser.error(f"Missing local photo: {row['path']}")
    for asset in (args.models / 'yolo-face/model.onnx',
                  args.models / 'adaface/model.onnx', args.landmarker):
        if not asset.is_file():
            parser.error(f'Missing model asset: {asset}')
    spec = importlib.util.spec_from_file_location('replay_ipwork_face', args.module)
    face = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(face)
    face.YOLO_FACE_MODEL_PATH = str(args.models / 'yolo-face/model.onnx')
    face.ADAFACE_MODEL_PATH = str(args.models / 'adaface/model.onnx')
    face.FACE_LANDMARKER_MODEL_PATH = str(args.landmarker)
    if args.enable_recovery:
        if not hasattr(face, 'FACE_RECOVERY_ENABLED'):
            parser.error('Selected processor does not implement experimental recovery')
        face.FACE_RECOVERY_ENABLED = True

    observed = {}
    def record_detector(name, function):
        def detect(image):
            try:
                detections = function(image)
                observed[name] = {'candidate_count': len(detections),
                                  'max_score': max((float(d['score']) for d in detections), default=0.),
                                  'boxes': detections}
                return detections
            except Exception as exc:
                observed[name] = {'error_type': type(exc).__name__}
                raise
        return detect
    face.detect_faces = record_detector('primary', face.detect_faces)
    if hasattr(face, 'detect_recovery_faces'):
        face.detect_recovery_faces = record_detector('recovery', face.detect_recovery_faces)

    results = []
    for index, row in enumerate(manifest, 1):
        observed.clear()
        # The worker runs face processing on convert_image_to_jpeg's preview
        # after its preview step. Reproduce that input, including orientation.
        original = Path(row['path']).read_bytes()
        preview = convert_image_to_jpeg(original, row['filename'])
        started = time.perf_counter()
        result = face.process_face('local-replay', row['filename'], preview)
        faces = result.get('faces') or []
        accepted = [item for item in faces if _client_face_passes_quality_gate(item)]
        results.append({
            'filename': row['filename'], 'baseline_status': row.get('status'),
            'baseline_face_count': row.get('faceCount'), 'face_count': len(faces),
            'quality_accepted_face_count': len(accepted),
            'raw_face_count': result.get('rawFaceCount'),
            'failure_stage': result.get('faceFailureStage'),
            'error': result.get('error'),
            'negative_group': row.get('negative_group'),
            'detector_outputs': dict(observed),
            'diagnostics': result.get('faceDiagnostics'),
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 3),
            'candidates': [{'bbox': item.get('bbox'), 'confidence': item.get('confidence'),
                            'alignment_method': item.get('alignmentMethod'),
                            'quality_accepted': _client_face_passes_quality_gate(item),
                            'embedding_fingerprint': hashlib.sha256(np.asarray(
                                item.get('embedding', []), dtype=np.float32).tobytes()).hexdigest()}
                           for item in faces],
        })
        if index == 1 or index % 25 == 0 or index == len(manifest):
            print(f'Replayed {index}/{len(manifest)}', flush=True)
    runtime_reasons = {'landmark_exception', 'landmark_invalid', 'alignment_exception',
                       'embedding_exception', 'embedding_computation_failed'}
    inference_errors = [row['filename'] for row in results if row['error'] or
                        row['failure_stage'] in ('unsupported_runtime', 'detection_failed') or
                        runtime_reasons.intersection((row['diagnostics'] or {}).get('reasonCounts', {}))]
    unexpected_faces = [row['filename'] for row in results if row['quality_accepted_face_count']]
    summary = {
        'photos': len(results),
        'photos_with_face_output': sum(row['face_count'] > 0 for row in results),
        'photos_without_face_output': sum(row['face_count'] == 0 for row in results),
        'photos_with_quality_accepted_faces': sum(row['quality_accepted_face_count'] > 0 for row in results),
        'failure_stages': dict(Counter(row['failure_stage'] or 'none' for row in results)),
        'inference_error_count': len(inference_errors),
        'inference_error_files': inference_errors,
        'negative_test_passed': not unexpected_faces and not inference_errors if args.expect_empty else None,
        'unexpected_face_files': unexpected_faces if args.expect_empty else [],
        'note': 'Local quality-gate check uses current environment settings; no faces are persisted.',
    }
    config = {'recovery_enabled': getattr(face, 'FACE_RECOVERY_ENABLED', False),
              'processor_sha256': hashlib.sha256(args.module.read_bytes()).hexdigest(),
              'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              'quality_gate': {key: getattr(storage_utils, key) for key in (
                  'FACE_MIN_STORE_CONFIDENCE', 'FACE_LOW_CONFIDENCE_REJECT_BELOW',
                  'FACE_LOW_CONFIDENCE_MAX_AREA_RATIO', 'FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO')}}
    args.output.write_text(json.dumps({'read_only': True, 'configuration': config, 'summary': summary,
                                       'results': results}, indent=2))
    print(json.dumps(summary, indent=2))
    if face._face_mesh is not None:
        face._face_mesh.close()
    if getattr(face, '_recovery_detector', None) is not None:
        face._recovery_detector.close()
    return 1 if args.expect_empty and (unexpected_faces or inference_errors) else 0


if __name__ == '__main__':
    raise SystemExit(main())
