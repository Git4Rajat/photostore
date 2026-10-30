"""Prepare isolated model graphs and held-out tensors; never touches storage."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--clip-graphs', type=Path, required=True)
    parser.add_argument('--face-landmarker', required=True)
    args = parser.parse_args()
    for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','OMP_THREAD_LIMIT','MKL_NUM_THREADS','VECLIB_MAXIMUM_THREADS'):
        os.environ[key] = '1'
    import numpy as np
    from PIL import Image
    import ipwork_face as face
    import ipwork_vision as tagging
    import vision_utils as vision
    from image_utils import convert_image_to_jpeg
    from onnxruntime.quantization import quantize_dynamic, quantize_static, QuantType, QuantFormat, CalibrationDataReader
    from onnxruntime.quantization.shape_inference import quant_pre_process
    face.FACE_LANDMARKER_MODEL_PATH = args.face_landmarker
    assert vision.image_encoder_available() and tagging._load_vocabulary()
    args.directory.mkdir(parents=True, exist_ok=True)
    rows = json.loads(args.manifest.read_text())
    images, previews = [], []
    for row in rows:
        path = Path(row['path'])
        data = convert_image_to_jpeg(path.read_bytes(), row.get('filename', path.name))
        previews.append(hashlib.sha256(data).hexdigest())
        images.append(vision._PREPROCESS(Image.open(io.BytesIO(data)).convert('RGB')).numpy())
    texts = ['Photo number '+str(i)+'. '+('People outside near trees buildings and cars. '*(1,3,8)[i%3]) for i in range(len(rows))]
    tokens = vision._TOKENIZER(texts).numpy()
    crops, seen = [], set()
    original = face.compute_face_embedding
    def capture(crop):
        value = original(crop)
        key = hashlib.sha256(crop.tobytes()).hexdigest()
        if value is not None and key not in seen:
            crops.append(crop.copy());seen.add(key)
        return value
    candidates = [Path(row['path']) for row in rows]
    # Extend only the existing local benchmark corpus directories, not user libraries.
    for parent in sorted({p.parent for p in candidates}, key=str):
        if parent.name in ('hq50k_images','img_celeba'):
            candidates.extend(sorted(parent.glob('*.jpg'))[:192])
    candidates = list(dict.fromkeys(candidates))
    face.compute_face_embedding = capture
    try:
        for i,path in enumerate(candidates):
            data = convert_image_to_jpeg(path.read_bytes(), path.name)
            if data:
                result = face.process_face('offline-int8-prepare',str(i)+'.jpg',data)
                if result.get('error'):raise RuntimeError('Face corpus processing error')
            if len(crops)>=96:break
    finally:
        face.compute_face_embedding = original
    if len(crops)<64:raise RuntimeError('Insufficient distinct face crops')
    # Alternate crop order before splitting so both original and extended inputs
    # contribute; calibration and held-out hashes must remain disjoint.
    crops = crops[::2]+crops[1::2]
    calibration, heldout = crops[:32], crops[32:96]
    to_tensor = lambda c:np.transpose(face.cv2.cvtColor(c,face.cv2.COLOR_BGR2RGB).astype(np.float32),(2,0,1))[None]
    class Reader(CalibrationDataReader):
        def __init__(self):self.iterator=iter(calibration)
        def get_next(self):
            crop=next(self.iterator,None)
            return {'data':to_tensor(crop)} if crop is not None else None
    quant_pre_process(face.ADAFACE_MODEL_PATH,str(args.directory/'face.preprocessed.onnx'),skip_symbolic_shape=True)
    quantize_static(str(args.directory/'face.preprocessed.onnx'),str(args.directory/'face.int8.onnx'),Reader(),
        quant_format=QuantFormat.QDQ, activation_type=QuantType.QUInt8, weight_type=QuantType.QInt8,
        per_channel=True,op_types_to_quantize=['Conv','MatMul','Gemm'])
    for kind in ('image','text'):
        # Infer shapes before quantization; preserve transformer MatMul nodes
        # instead of fusing them into a different attention operator here.
        quant_pre_process(str(args.clip_graphs/(kind+'.onnx')),str(args.directory/(kind+'.preprocessed.onnx')),
            skip_optimization=True,skip_symbolic_shape=True)
        quantize_dynamic(str(args.directory/(kind+'.preprocessed.onnx')),str(args.directory/(kind+'.int8.onnx')),
            weight_type=QuantType.QInt8,per_channel=True,op_types_to_quantize=['MatMul','Gemm'])
    np.savez_compressed(args.directory/'inputs.npz',images=np.asarray(images),tokens=tokens,
        faces=np.asarray(heldout),vocabulary=tagging._vocab_embeddings)
    meta=dict(images=len(images),calibration_faces=len(calibration),heldout_faces=len(heldout),
        preview_sha256=previews,calibration_sha256=[hashlib.sha256(c.tobytes()).hexdigest() for c in calibration],
        heldout_sha256=[hashlib.sha256(c.tobytes()).hexdigest() for c in heldout],
        vocabulary=tagging._vocab_labels,person_labels=sorted(tagging.PERSON_LABELS),
        person_threshold=tagging.PERSON_SCORE_THRESHOLD,top_k=tagging.DEFAULT_VOCAB_TOP_K,
        sensitive=[tagging.is_sensitive_auto_tag_label(label) for label in tagging._vocab_labels])
    assert set(meta['calibration_sha256']).isdisjoint(meta['heldout_sha256'])
    (args.directory/'inputs.json').write_text(json.dumps(meta,indent=2)+'\n')
    print(json.dumps({k:meta[k] for k in ('images','calibration_faces','heldout_faces')}),flush=True)


if __name__=='__main__':main()
