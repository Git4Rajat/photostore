"""Offline same-weight CLIP CPU comparison: production PyTorch versus ONNX.

Export dependencies and graphs belong in a temporary directory. Export/model
load/preprocessing are excluded from timing. No production settings change.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    for name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
        os.environ[name] = '1'
    os.environ['OMP_THREAD_LIMIT'] = '4'
    import numpy as np
    import torch
    import onnxruntime as ort
    from PIL import Image
    import vision_utils as vision
    import ipwork_vision as tagging
    from image_utils import convert_image_to_jpeg
    from benchmark_vision_cpu_runtime import quality
    if not vision.image_encoder_available() or not tagging._load_vocabulary():
        raise RuntimeError('Real CLIP and vocabulary required')
    args.directory.mkdir(parents=True, exist_ok=True)
    images, hashes = [], []
    for row in json.loads(args.manifest.read_text()):
        path = Path(row['path'])
        data = convert_image_to_jpeg(path.read_bytes(), row.get('filename', path.name))
        hashes.append(hashlib.sha256(data).hexdigest())
        images.append(vision._PREPROCESS(Image.open(io.BytesIO(data)).convert('RGB')).unsqueeze(0))
    texts = ['Photo number ' + str(i) + '. ' +
             ('People outside near trees buildings and cars. ' * (1, 3, 8)[i % 3])
             for i in range(len(images))]
    tokens = [vision._TOKENIZER([text]) for text in texts]
    class Tower(torch.nn.Module):
        def __init__(self, model, kind):
            super().__init__()
            self.model, self.kind = model, kind
        def forward(self, value):
            features = (self.model.encode_image(value) if self.kind == 'image'
                        else self.model.encode_text(value))
            return features / features.norm(dim=-1, keepdim=True)
    model = vision._MODEL
    towers = {kind: Tower(model, kind).eval() for kind in ('image', 'text')}
    def torch_run(i, context=torch.no_grad):
        with context():
            return tuple(towers[k](x[i]).cpu().numpy().reshape(-1).copy()
                         for k, x in [('image', images), ('text', tokens)])
    baseline = [torch_run(i) for i in range(len(images))]
    started = time.perf_counter()
    # The legacy exporter needs the decomposed attention path, rather than
    # the CPU fast-path native attention operator. Production baseline above
    # retains its normal fast-path behavior.
    fastpath = torch.backends.mha.get_fastpath_enabled()
    torch.backends.mha.set_fastpath_enabled(False)
    try:
        with torch.no_grad():
            for kind, value in [('image', images[0]), ('text', tokens[0])]:
                torch.onnx.export(towers[kind], (value,), str(args.directory / (kind + '.onnx')),
                    input_names=['input'], output_names=['embedding'], opset_version=17,
                    dynamo=False)
    finally:
        torch.backends.mha.set_fastpath_enabled(fastpath)
    export_seconds = time.perf_counter()-started
    print('EXPORT_READY ' + str(export_seconds), flush=True)
    sessions = {}
    for threads in (1, 2, 4):
        options = ort.SessionOptions()
        options.intra_op_num_threads, options.inter_op_num_threads = threads, 1
        sessions[threads] = {k:ort.InferenceSession(str(args.directory/(k+'.onnx')),
            sess_options=options, providers=['CPUExecutionProvider']) for k in towers}
    def ort_run(i, threads):
        return tuple(sessions[threads][k].run(['embedding'], {'input':x[i].numpy()})[0].reshape(-1)
                     for k, x in [('image', images), ('text', tokens)])
    def decisions(vector):
        scores = tagging._vocab_embeddings @ vector
        indices = [int(i) for i in np.argsort(-scores)
                   if not tagging.is_sensitive_auto_tag_label(tagging._vocab_labels[int(i)])][:tagging.DEFAULT_VOCAB_TOP_K]
        labels = [tagging._vocab_labels[i] for i in indices]
        person = next((i for i in indices if tagging._vocab_labels[i] in tagging.PERSON_LABELS), None)
        return labels, bool(person is not None and scores[person] >= tagging.PERSON_SCORE_THRESHOLD)
    base_decisions = [decisions(v[0]) for v in baseline]
    report = dict(platform=platform.platform(), torch_version=torch.__version__,
        ort_version=ort.__version__, inputs=len(images), preview_sha256=hashes,
        export_seconds=export_seconds, graph_bytes={k:(args.directory/(k+'.onnx')).stat().st_size for k in towers},
        graph_sha256={k:hashlib.sha256((args.directory/(k+'.onnx')).read_bytes()).hexdigest() for k in towers},
        providers={str(t):{k:s.get_providers() for k,s in d.items()} for t,d in sessions.items()},
        thread_environment={k:os.environ.get(k) for k in ('OMP_THREAD_LIMIT','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','VECLIB_MAXIMUM_THREADS')},
        reports=[])
    configs = [('torch1', lambda i:torch_run(i), 1),
               ('torch2', lambda i:torch_run(i), 2),
               ('torch_inference1', lambda i:torch_run(i, torch.inference_mode), 1),
               *[('onnx'+str(t), lambda i,t=t:ort_run(i,t), 1) for t in (1,2,4)]]
    for r in range(args.rounds):
        for mode, run, threads in (configs if r % 2 == 0 else configs[::-1]):
            torch.set_num_threads(threads)
            for i in range(3):run(i)
            before, started = time.process_time(), time.perf_counter()
            values = [run(i) for i in range(len(images))]
            wall, cpu = time.perf_counter()-started, time.process_time()-before
            iq, tq = (quality([v[j] for v in values], [v[j] for v in baseline]) for j in (0,1))
            score = np.asarray([v[0] for v in values]) @ np.asarray([v[1] for v in values]).T
            base_score = np.asarray([v[0] for v in baseline]) @ np.asarray([v[1] for v in baseline]).T
            changed = sum(decisions(v[0]) != b for v,b in zip(values,base_decisions))
            row = dict(round=r, mode=mode, count=len(images), wall_seconds=wall,
                process_cpu_seconds=cpu, milliseconds_per_image=wall*1000/len(images),
                image_quality=iq, text_quality=tq, changed_tag_or_person_decisions=changed,
                cross_modal_top1_changes=int(np.sum(score.argmax(1) != base_score.argmax(1))),
                max_cross_modal_score_error=float(np.abs(score-base_score).max()))
            report['reports'].append(row)
            args.output.write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(row), flush=True)
            if min(iq['min_cosine'],tq['min_cosine']) < .9999 or changed:
                raise RuntimeError('Candidate failed output quality check')


if __name__ == '__main__':
    main()
