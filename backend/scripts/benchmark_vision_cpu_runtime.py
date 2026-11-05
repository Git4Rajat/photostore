"""Offline controlled CPU experiments; no app, network or storage clients.

Compare AdaFace session settings and CLIP inference contexts/thread counts.
Uses real aligned face crops from the production processor and preprocessed
CLIP tensors. These are component experiments, not upload throughput tests.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import threading
import time


def quality(actual, expected):
    import numpy as np
    actual, expected = np.asarray(actual), np.asarray(expected)
    if actual.shape != expected.shape or not np.isfinite(actual).all():
        raise ValueError('Invalid output shape or nonfinite output')
    similarities = np.sum(actual * expected, axis=1)
    gram_a, gram_b = actual @ actual.T, expected @ expected.T
    np.fill_diagonal(gram_a, -2)
    np.fill_diagonal(gram_b, -2)
    return dict(min_cosine=float(similarities.min()),
                max_abs_error=float(np.abs(actual - expected).max()),
                max_pairwise_cosine_error=float(np.abs(gram_a - gram_b).max()),
                nearest_neighbor_changes=int(np.sum(gram_a.argmax(1) != gram_b.argmax(1))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--face-landmarker', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--section', choices=['components', 'pipeline'], default='components')
    parser.add_argument('--omp-thread-limit', type=int, default=4)
    parser.add_argument('--clip-onnx-directory', type=Path)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('rounds must be positive')
    for variable in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS',
                     'OMP_THREAD_LIMIT', 'VECLIB_MAXIMUM_THREADS'):
        os.environ[variable] = '1'
    os.environ['OMP_THREAD_LIMIT'] = str(args.omp_thread_limit)
    import numpy as np
    from PIL import Image
    import io
    import torch
    import onnxruntime as ort
    import ipwork_face as face
    import vision_utils as vision
    from image_utils import convert_image_to_jpeg
    face.FACE_LANDMARKER_MODEL_PATH = args.face_landmarker
    if not vision.image_encoder_available():
        raise RuntimeError('Real CLIP weights required')
    previews = []
    for row in json.loads(args.manifest.read_text()):
        path = Path(row['path'])
        data = convert_image_to_jpeg(path.read_bytes(), row.get('filename', path.name))
        if not data:
            raise RuntimeError('Invalid preview')
        previews.append(data)
    if not previews:
        raise ValueError('Empty input corpus')
    # Capture actual alignment outputs, without retaining image bytes in reports.
    aligned, positive_indices = [], []
    compute = face.compute_face_embedding
    def capture(crop):
        value = compute(crop)
        if value is not None:
            aligned.append(crop.copy())
        return value
    face.compute_face_embedding = capture
    try:
        for i, data in enumerate(previews):
            result = face.process_face('offline-runtime-benchmark', str(i) + '.jpg', data)
            if result.get('error'):
                raise RuntimeError('Face processor failed')
            if result.get('faces'):
                positive_indices.append(i)
    finally:
        face.compute_face_embedding = compute
    if len(aligned) < 2:
        raise RuntimeError('Need at least two embedded faces')
    face_inputs = [np.transpose(face.cv2.cvtColor(c, face.cv2.COLOR_BGR2RGB).astype(np.float32),
                                (2, 0, 1))[None] for c in aligned]
    image_inputs = [vision._PREPROCESS(Image.open(io.BytesIO(p)).convert('RGB')).unsqueeze(0)
                    for p in previews]
    # Fixed, distinct synthetic descriptions exercise short and long token inputs.
    texts = ['Photo number ' + str(i) + '. ' +
             ('People outside near trees buildings and cars. ' * (1, 3, 8)[i % 3])
             for i in range(len(previews))]
    tokens = [vision._TOKENIZER([text]) for text in texts]
    report = dict(platform=platform.platform(), machine=platform.machine(),
                  torch_version=torch.__version__, ort_version=ort.__version__,
                  cpu_count=os.cpu_count(), cpu_quota=None,
                  thread_environment={k:os.environ[k] for k in ('OPENBLAS_NUM_THREADS',
                      'MKL_NUM_THREADS', 'OMP_NUM_THREADS', 'OMP_THREAD_LIMIT', 'VECLIB_MAXIMUM_THREADS')},
                  images=len(previews), faces=len(aligned),
                  preview_sha256=[hashlib.sha256(p).hexdigest() for p in previews],
                  aligned_sha256=[hashlib.sha256(p.tobytes()).hexdigest() for p in aligned],
                  face_providers=face._get_adaface_session().get_providers(),
                  clip_device=str(next(vision._MODEL.parameters()).device),
                  reports=[])
    if Path('/sys/fs/cgroup/cpu.max').exists():
        report['cpu_quota'] = Path('/sys/fs/cgroup/cpu.max').read_text().strip()
    def save(row):
        report['reports'].append(row)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(row), flush=True)
    def measure(fn, inputs, workers):
        before, started = time.process_time(), time.perf_counter()
        with ThreadPoolExecutor(workers) as pool:
            values = list(pool.map(fn, inputs))
        wall = time.perf_counter() - started
        return values, dict(wall_seconds=wall, process_cpu_seconds=time.process_time() - before,
                            mean_amortized_ms=wall * 1000 / len(inputs))
    if args.section == 'pipeline':
        from contextvars import copy_context
        from contextlib import nullcontext
        from benchmark_vision_stages import fingerprint
        import ipwork_ocr as ocr
        import ipwork_vision as tagging
        from ipwork_image import image_job
        import processing_metrics as metrics
        if not tagging._load_vocabulary():
            raise RuntimeError('Real vocabulary required')
        profiles = dict(mixed=list(range(len(previews))), face_positive=positive_indices * 4)
        reference = {}
        baseline_session = face._get_adaface_session()
        options = face._single_threaded_session_options()
        options.intra_op_num_threads = 2
        two_thread_session = ort.InferenceSession(face.ADAFACE_MODEL_PATH,
            sess_options=options, providers=['CPUExecutionProvider'])
        def process(index, mode):
            with metrics.accounting() as counter, image_job(True):
                started = time.perf_counter()
                text = ocr.process_ocr('offline-runtime-benchmark', str(index) + '.jpg', previews[index])
                faces = face.process_face('offline-runtime-benchmark', str(index) + '.jpg', previews[index])
                with (torch.inference_mode() if mode == 'inference_mode' else nullcontext()):
                    result = tagging.process_vision('offline-runtime-benchmark', str(index) + '.jpg', previews[index])
                    semantic = vision.encode_text_embedding(texts[index])
                for name, output in [('ocr', text), ('face', faces), ('clip', result)]:
                    if output.get('error'):
                        raise RuntimeError('Pipeline ' + name + ' failed: ' + str(output['error']))
                values = counter.snapshot()
                return dict(image=result['imageEmbedding'], text=semantic,
                    decisions=(result['tags'], result['aiPersonCandidate'], result['aiPersonLabel']),
                    face_hash=fingerprint(faces), ocr_hash=fingerprint(text),
                    elapsed_ms=(time.perf_counter()-started)*1000,
                    steps={k:values[k] for k in ('face_embed_ms', 'face_detect_ms',
                        'clip_inference_ms', 'text_inference_ms', 'ocr_recognition_ms')})
        configurations = [('current1', 1, 1, 1, 'no_grad'), ('current2', 2, 1, 1, 'no_grad'),
                          ('current3', 3, 1, 1, 'no_grad'), ('inference3', 3, 1, 1, 'inference_mode'),
                          ('torch2_workers3', 3, 2, 1, 'no_grad'), ('face2_workers3', 3, 1, 2, 'no_grad')]
        original_image = vision._encode_image_tensor_single
        original_text = vision._encode_text_embedding_single
        sessions = {}
        if args.clip_onnx_directory:
            for threads in (1, 2):
                options = face._single_threaded_session_options()
                options.intra_op_num_threads = threads
                sessions[threads] = {k:ort.InferenceSession(str(args.clip_onnx_directory/(k+'.onnx')),
                    sess_options=options, providers=['CPUExecutionProvider']) for k in ('image','text')}
            configurations = [('current3',3,1,1,'no_grad'), ('onnx1',3,1,1,'no_grad'),
                              ('onnx2',3,1,1,'no_grad')]
        def onnx_encode(kind, tensor, threads):
            waiting = time.monotonic()
            with vision._MODEL_LOCK:
                metrics.add(**{('clip' if kind == 'image' else 'text')+'_lock_wait_ms':
                    (time.monotonic()-waiting)*1000})
                started = time.monotonic()
                value = sessions[threads][kind].run(['embedding'], {'input':tensor.numpy()})[0]
                metrics.add(**{('clip' if kind == 'image' else 'text')+'_inference_ms':
                    (time.monotonic()-started)*1000})
            return value.reshape(-1).tolist()
        def onnx_text(text, threads):
            if not text:
                return []
            started = time.monotonic()
            tensor = vision._TOKENIZER([text])
            metrics.add(text_tokenize_ms=(time.monotonic()-started)*1000)
            return onnx_encode('text', tensor, threads)
        # Warm resident OCR threads as well as the model/vocabulary paths.
        for workers in (1, 2, 3):
            with ThreadPoolExecutor(workers) as pool:
                list(pool.map(lambda i: process(i, 'no_grad'), range(12)))
        for profile, indices in profiles.items():
            for r in range(args.rounds):
                for name, workers, torch_threads, face_threads, mode in (
                        configurations if r % 2 == 0 else configurations[::-1]):
                    torch.set_num_threads(torch_threads)
                    face._adaface_session = baseline_session if face_threads == 1 else two_thread_session
                    if name.startswith('onnx'):
                        threads = int(name[-1])
                        vision._encode_image_tensor_single = lambda x,t=threads:onnx_encode('image',x,t)
                        vision._encode_text_embedding_single = lambda x,t=threads:onnx_text(x,t)
                    else:
                        vision._encode_image_tensor_single = original_image
                        vision._encode_text_embedding_single = original_text
                    before, started = time.process_time(), time.perf_counter()
                    with ThreadPoolExecutor(workers) as pool:
                        futures = [pool.submit(copy_context().run, process, i, mode) for i in indices]
                        values = [future.result() for future in futures]
                    wall, cpu = time.perf_counter()-started, time.process_time()-before
                    base = reference.setdefault(profile, values)
                    image_quality = quality([v['image'] for v in values], [v['image'] for v in base])
                    text_quality = quality([v['text'] for v in values], [v['text'] for v in base])
                    changed = sum((v['face_hash'], v['ocr_hash'], v['decisions']) !=
                        (b['face_hash'], b['ocr_hash'], b['decisions']) for v, b in zip(values, base))
                    if changed or min(image_quality['min_cosine'], text_quality['min_cosine']) < .99999:
                        raise RuntimeError('Pipeline output parity failed')
                    save(dict(component='pipeline', profile=profile, mode=name, round=r,
                        workers=workers, torch_threads=torch_threads, face_threads=face_threads,
                        context=mode, count=len(values), wall_seconds=wall, process_cpu_seconds=cpu,
                        mean_amortized_ms=wall*1000/len(values), image_quality=image_quality,
                        text_quality=text_quality, changed_output_samples=changed,
                        mean_steps_ms={k:sum(v['steps'][k] for v in values)/len(values)
                                       for k in values[0]['steps']}))
        face._adaface_session = baseline_session
        vision._encode_image_tensor_single = original_image
        vision._encode_text_embedding_single = original_text
        torch.set_num_threads(1)
        return
    reference_face = []
    base = face._get_adaface_session()
    input_name, output_name = base.get_inputs()[0].name, base.get_outputs()[0].name
    def face_run(session, tensor):
        value = session.run([output_name], {input_name: tensor})[0].reshape(-1)
        return value / np.linalg.norm(value)
    reference_face = [face_run(base, x) for x in face_inputs]
    configs = [(1, 'all', True), (2, 'all', True), (4, 'all', True),
               (1, 'all', False), (1, 'extended', True)]
    # Session/model load and first inference are excluded from measured rounds.
    for intra, graph, spin in configs:
        options = face._single_threaded_session_options()
        options.intra_op_num_threads = intra
        options.graph_optimization_level = (ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            if graph == 'all' else ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED)
        if not spin:
            options.add_session_config_entry('session.intra_op.allow_spinning', '0')
            options.add_session_config_entry('session.inter_op.allow_spinning', '0')
        session = ort.InferenceSession(face.ADAFACE_MODEL_PATH, sess_options=options,
                                       providers=['CPUExecutionProvider'])
        for tensor in face_inputs[:3]:
            face_run(session, tensor)
        for r in range(args.rounds):
            for workers in ([1, 3] if r % 2 == 0 else [3, 1]):
                values, timing = measure(lambda x: face_run(session, x), face_inputs, workers)
                parity = quality(values, reference_face)
                if parity['min_cosine'] < .99999 or parity['max_abs_error'] > .0001:
                    raise RuntimeError('Face parity check failed')
                save(dict(component='adaface', round=r, workers=workers, intra_threads=intra,
                          graph=graph, spinning=spin, count=len(values), quality=parity, **timing))
        del session
    model_lock = threading.Lock()
    def clip_run(i, context):
        # Preserve production image/text serialization on the same shared model.
        with context(), model_lock:
            image = vision._MODEL.encode_image(image_inputs[i])
            text = vision._MODEL.encode_text(tokens[i])
            image = image / image.norm(dim=-1, keepdim=True)
            text = text / text.norm(dim=-1, keepdim=True)
            return image.squeeze(0).cpu().numpy().copy(), text.squeeze(0).cpu().numpy().copy()
    baseline = [clip_run(i, torch.no_grad) for i in range(len(previews))]
    configs = [(threads, mode, workers) for threads in (1, 2, 4)
               for mode in ('no_grad', 'inference_mode') for workers in (1, 3)]
    for r in range(args.rounds):
        for threads, mode, workers in (configs if r % 2 == 0 else configs[::-1]):
            torch.set_num_threads(threads)
            context = getattr(torch, mode)
            for i in range(3):
                clip_run(i, context)
            values, timing = measure(lambda i: clip_run(i, context), range(len(previews)), workers)
            image_quality = quality([v[0] for v in values], [v[0] for v in baseline])
            text_quality = quality([v[1] for v in values], [v[1] for v in baseline])
            retrieval = np.asarray([v[0] for v in values]) @ np.asarray([v[1] for v in values]).T
            reference = np.asarray([v[0] for v in baseline]) @ np.asarray([v[1] for v in baseline]).T
            for parity in (image_quality, text_quality):
                if parity['min_cosine'] < .99999 or parity['max_abs_error'] > .0001:
                    raise RuntimeError('CLIP parity check failed')
            save(dict(component='clip_image_and_text', round=r, workers=workers,
                      torch_threads=threads, context=mode, count=len(values),
                      image_quality=image_quality, text_quality=text_quality,
                      cross_modal_top1_changes=int(np.sum(retrieval.argmax(1) != reference.argmax(1))),
                      max_cross_modal_score_error=float(np.abs(retrieval-reference).max()), **timing))
    torch.set_num_threads(1)


if __name__ == '__main__':
    main()
