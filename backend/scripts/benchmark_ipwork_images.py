"""Offline, opt-in worker benchmarks. Writes private per-photo outputs locally.

Run with PYTHONPATH=backend and the ipworker dependencies installed. A manifest
is a JSON list of {path, filename, optional expected_text}. Image mode compares
independent decoding with task-scoped RGB reuse on identical preview bytes.
Preview mode compares originals with stored previews. OCR mode additionally
requires RapidOCR in an isolated environment. Neither
mode imports app.py, writes storage, or changes production configuration.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import time


def clean_result(value):
    if isinstance(value, dict):
        return {key: clean_result(item) for key, item in value.items()
                if key != 'stageTimingsMs'}
    if isinstance(value, list):
        return [clean_result(item) for item in value]
    return value


def normalized(text):
    return ' '.join((text or '').split())


def timed(call):
    start = time.perf_counter()
    result = call()
    return result, time.perf_counter() - start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['images', 'preview', 'ocr'])
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--limit', type=int, default=24)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--face-landmarker')
    parser.add_argument('--ocr-model-root', default='/tmp/photostore-ocr-models')
    args = parser.parse_args()
    if args.rounds < 1 or args.workers < 1:
        parser.error('rounds and workers must be positive')
    if args.mode == 'ocr' and args.workers != 1:
        parser.error('OCR comparison requires one worker; candidate thread safety is not established')
    from image_utils import convert_image_to_jpeg
    from ipwork_image import image_job, rgb_image
    import ipwork_ocr
    rows = json.loads(Path(args.manifest).read_text())[:args.limit]
    samples = []
    for row in rows:
        original = Path(row['path']).read_bytes()
        name = row.get('filename') or Path(row['path']).name
        preview = convert_image_to_jpeg(original, name)
        if not preview:
            raise ValueError(f'Preview generation failed: {name}')
        samples.append((name, preview, row.get('expected_text'), original))
    if not samples:
        parser.error('manifest must contain at least one sample')
    model_start = time.perf_counter()
    candidate_engine = None
    if args.mode in ('images', 'preview'):
        import ipwork_face
        import ipwork_vision
        import vision_utils
        if args.face_landmarker:
            ipwork_face.FACE_LANDMARKER_MODEL_PATH = args.face_landmarker
        if not vision_utils.image_encoder_available() or not ipwork_vision._load_vocabulary():
            raise RuntimeError('Real CLIP weights/vocabulary are required; no fallback benchmark')
        def run(sample, candidate):
            name, preview, _, original = sample
            data = original if args.mode == 'preview' and not candidate else preview
            with image_job(candidate if args.mode == 'images' else False):
                return clean_result({
                    'ocr': ipwork_ocr.process_ocr('benchmark', name, data),
                    'face': ipwork_face.process_face('benchmark', name, data),
                    'vision': ipwork_vision.process_vision('benchmark', name, data),
                })
    else:
        from rapidocr import RapidOCR, OCRVersion, ModelType, LangRec
        candidate_engine = RapidOCR(params={
            'Global.model_root_dir': args.ocr_model_root,
            'Global.log_level': 'warning',
            'Det.ocr_version': OCRVersion.PPOCRV5,
            'Det.model_type': ModelType.MOBILE,
            'Rec.ocr_version': OCRVersion.PPOCRV5,
            'Rec.model_type': ModelType.MOBILE,
            'Rec.lang_type': LangRec.EN,
            'EngineConfig.onnxruntime.intra_op_num_threads': 1,
            'EngineConfig.onnxruntime.inter_op_num_threads': 1,
        })
        def run(sample, candidate):
            name, data, _, _ = sample
            if not candidate:
                result = ipwork_ocr.process_ocr('benchmark', name, data)
                if result.get('error'):
                    raise RuntimeError(result['error'])
                return normalized(result.get('text'))
            with rgb_image(data, name) as image:
                result = candidate_engine(image)
            return normalized(' '.join(result.txts or []))
    init_seconds = time.perf_counter() - model_start
    # First inference separately captures lazy model initialization. Warm both
    # modes before crossing their order each round; inputs already in RAM.
    _, baseline_first = timed(lambda: run(samples[0], False))
    _, candidate_first = timed(lambda: run(samples[0], True))
    outcomes, durations, details = {}, {'baseline': [], 'candidate': []}, []
    for round_index in range(args.rounds):
        for candidate in ((False, True) if round_index % 2 == 0 else (True, False)):
            label = 'candidate' if candidate else 'baseline'
            def process(index):
                result, seconds = timed(lambda: run(samples[index], candidate))
                return index, result, seconds
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                results = list(pool.map(process, range(len(samples))))
            durations[label].append(time.perf_counter() - started)
            for index, result, seconds in results:
                outcomes[(label, index, round_index)] = result
                details.append({'sample': index, 'round': round_index, 'mode': label,
                                'seconds': seconds, 'result': result})
            print(json.dumps({'round': round_index, 'mode': label,
                              'wall_seconds': durations[label][-1]}), flush=True)
    changed = sorted({index for index in range(len(samples))
                      for round_index in range(args.rounds)
                      if outcomes[('baseline', index, round_index)] != outcomes[('candidate', index, round_index)]})
    errors = [row for row in details if args.mode in ('images', 'preview') and any(
        step.get('error') for step in row['result'].values())]
    # Exact outputs are a conservative gate, not a substitute for independently
    # labelled OCR recall/precision or Linux deployment measurements.
    baseline = statistics.mean(durations['baseline'])
    candidate = statistics.mean(durations['candidate'])
    summary = {
        'mode': args.mode, 'samples': len(samples), 'rounds': args.rounds,
        'workers': args.workers, 'platform': platform.platform(),
        'python': platform.python_version(), 'model_init_seconds': init_seconds,
        'first_inference_seconds': {'baseline': baseline_first, 'candidate': candidate_first},
        'wall_seconds_by_round': durations,
        'mean_wall_seconds': {'baseline': baseline, 'candidate': candidate},
        'time_reduction_percent': 100 * (1 - candidate / baseline),
        'changed_samples': changed, 'exact_same_samples': len(samples) - len(changed),
        'processing_errors': len(errors),
        'baseline_text_positive_samples': sum(bool(outcomes[('baseline', index, 0)]) for index in range(len(samples))) if args.mode == 'ocr' else None,
        'candidate_text_positive_samples': sum(bool(outcomes[('candidate', index, 0)]) for index in range(len(samples))) if args.mode == 'ocr' else None,
        'labelled_text_controls': sum(expected is not None for _, _, expected, _ in samples),
        'labelled_exact_matches': {label: sum(
            normalized(expected) == outcomes[(label, index, 0)]
            for index, (_, _, expected, _) in enumerate(samples) if expected is not None)
            for label in ('baseline', 'candidate')} if args.mode == 'ocr' else None,
        'adoption_gate': args.mode == 'ocr' and candidate < .6 * baseline and not changed,
        'preview_bytes_total': sum(len(data) for _, data, _, _ in samples),
        'original_bytes_total': sum(len(data) for _, _, _, data in samples),
        'versions': {name: importlib.metadata.version(name) for name in
                     (['Pillow', 'tesserocr', 'rapidocr', 'onnxruntime'] if candidate_engine else
                      ['Pillow', 'tesserocr', 'torch', 'onnxruntime', 'open_clip_torch'])},
    }
    Path(args.output).write_text(json.dumps({'summary': summary, 'details': details}, indent=2))
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
