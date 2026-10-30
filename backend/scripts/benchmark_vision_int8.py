"""Isolated 2-vCPU/4-GiB inference benchmark; no production storage clients."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import threading
import time


def vector_quality(actual, reference):
    import numpy as np
    a,b=np.asarray(actual),np.asarray(reference)
    if a.shape!=b.shape or not np.isfinite(a).all():raise ValueError('Invalid model output')
    cos=np.sum(a*b,axis=1)
    ga,gb=a@a.T,b@b.T
    mask=np.triu(np.ones(ga.shape,dtype=bool),1)
    thresholds={str(t):int(np.sum(((ga>=t)!=(gb>=t))[mask])) for t in (.6,.65,.7,.78,.82)}
    np.fill_diagonal(ga,-2);np.fill_diagonal(gb,-2)
    return dict(min_cosine=float(cos.min()),median_cosine=float(np.median(cos)),
        max_abs_error=float(np.abs(a-b).max()),max_pairwise_error=float(np.abs(ga-gb).max()),
        nearest_neighbor_changes=int(np.sum(ga.argmax(1)!=gb.argmax(1))),threshold_pair_flips=thresholds)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=3)
    args=parser.parse_args()
    for k in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','OMP_THREAD_LIMIT','MKL_NUM_THREADS'):
        os.environ[k]='1'
    cpu_max=Path('/sys/fs/cgroup/cpu.max').read_text().strip()
    memory_max=int(Path('/sys/fs/cgroup/memory.max').read_text())
    quota,period=map(int,cpu_max.split())
    if quota/period!=2 or memory_max!=4*1024**3:
        raise RuntimeError('Benchmark requires exactly two CPU / four GiB')
    import numpy as np
    import onnxruntime as ort
    import torch
    import vision_utils as vision
    torch.set_num_threads(1)
    p=args.directory
    inputs=np.load(p/'inputs.npz')
    meta=json.loads((p/'inputs.json').read_text())
    if set(meta['calibration_sha256'])&set(meta['heldout_sha256']):raise ValueError('Calibration leakage')
    images,tokens=inputs['images'],inputs['tokens']
    faces=[np.transpose(c[...,::-1].astype(np.float32),(2,0,1))[None] for c in inputs['faces']]
    vocabulary=inputs['vocabulary']
    lock=threading.Lock()
    def cpu_stat():return {k:int(v) for k,v in (line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())}
    report=dict(platform=platform.platform(),cpu_max=cpu_max,memory_max_bytes=memory_max,
        cpu_model=next((l for l in Path('/proc/cpuinfo').read_text().splitlines() if l.startswith('model name')),None),
        ort_version=ort.__version__,torch_version=torch.__version__,source_workers=3,
        inputs={k:meta[k] for k in ('images','calibration_faces','heldout_faces','preview_sha256','calibration_sha256','heldout_sha256')},
        reports=[],quality={},errors=[])
    def save():args.output.write_text(json.dumps(report,indent=2)+'\n')
    def session(name):
        options=ort.SessionOptions();options.intra_op_num_threads=options.inter_op_num_threads=1
        return ort.InferenceSession(str(p/name),sess_options=options,providers=['CPUExecutionProvider'])
    def norm(v):
        a=v.reshape(-1);length=np.linalg.norm(a)
        if not np.isfinite(a).all() or length<=0:raise ValueError('Invalid model vector')
        return a/length
    def face_run(s,i):return norm(s.run(None,{'data':faces[i]})[0])
    def onnx_clip(s,i):
        with lock:
            return tuple(s[k].run(['embedding'],{'input':a[i:i+1]})[0].reshape(-1)
                         for k,a in [('image',images),('text',tokens)])
    def torch_clip(i):
        with torch.no_grad(),lock:
            out=[]
            for kind,values in [('image',images),('text',tokens)]:
                value=torch.from_numpy(values[i:i+1])
                result=(vision._MODEL.encode_image(value) if kind=='image' else vision._MODEL.encode_text(value))
                result=result/result.norm(dim=-1,keepdim=True)
                out.append(result.numpy().reshape(-1).copy())
            return tuple(out)
    def measured(section,mode,fn,count,r):
        for i in range(min(count,3)):fn(i)
        stat,before,started=cpu_stat(),time.process_time(),time.perf_counter()
        with ThreadPoolExecutor(3) as pool:values=list(pool.map(fn,range(count)))
        wall,cpu=time.perf_counter()-started,time.process_time()-before
        after=cpu_stat()
        row=dict(section=section,mode=mode,round=r,count=count,wall_seconds=wall,
            process_cpu_seconds=cpu,milliseconds_per_item=wall*1000/count,
            cpu_delta={k:v-stat.get(k,0) for k,v in after.items()},
            memory_current_bytes=int(Path('/sys/fs/cgroup/memory.current').read_text()),
            memory_peak_lifetime_bytes=int(Path('/sys/fs/cgroup/memory.peak').read_text()))
        report['reports'].append(row);save();print(json.dumps(row),flush=True)
        return values
    s=session('face.fp32.onnx');reference_face=[face_run(s,i) for i in range(len(faces))];del s;gc.collect()
    for r in range(args.rounds):
        for mode in (['fp32','int8'] if r%2==0 else ['int8','fp32']):
            s=session('face.'+mode+'.onnx')
            values=measured('face',mode,lambda i:face_run(s,i),len(faces),r)
            report['quality']['face_'+mode]=vector_quality(values,reference_face);save()
            del s;gc.collect()
    if not vision.image_encoder_available():raise RuntimeError('Actual production CLIP required')
    reference_clip=[torch_clip(i) for i in range(len(images))]
    def tag_decisions(v):
        scores=vocabulary@v
        indices=[int(i) for i in np.argsort(-scores) if not meta['sensitive'][int(i)]][:meta['top_k']]
        person=next((i for i in indices if meta['vocabulary'][i] in meta['person_labels']),None)
        return indices,bool(person is not None and scores[person]>=meta['person_threshold'])
    def clip_quality(values):
        a,b=[v[0] for v in values],[v[1] for v in values]
        ra,rb=[v[0] for v in reference_clip],[v[1] for v in reference_clip]
        sim,ref=np.asarray(a)@np.asarray(b).T,np.asarray(ra)@np.asarray(rb).T
        return dict(image=vector_quality(a,ra),text=vector_quality(b,rb),
            tag_decision_changes=sum(tag_decisions(v)!=tag_decisions(w) for v,w in zip(a,ra)),
            cross_modal_top1_changes=int(np.sum(sim.argmax(1)!=ref.argmax(1))),
            max_cross_modal_score_error=float(np.abs(sim-ref).max()))
    for r in range(args.rounds):
        for mode in (['torch','fp32','int8'] if r%2==0 else ['int8','fp32','torch']):
            sessions=None if mode=='torch' else {k:session(k+'.'+mode+'.onnx') for k in ('image','text')}
            fn=torch_clip if mode=='torch' else lambda i:onnx_clip(sessions,i)
            values=measured('clip',mode,fn,len(images),r)
            report['quality']['clip_'+mode]=clip_quality(values);save()
            del sessions;gc.collect()
    # Actual kernels sequenced per source, excluding OCR/detector/I/O; every
    # source has one held-out face plus an image/text pair. This is an ablation,
    # not a replay of full upload processing or an image-hour forecast.
    configs=[('current','fp32','torch'),('onnx_fp32','fp32','fp32'),
             ('face_int8','int8','torch'),('clip_int8','fp32','int8'),('both_int8','int8','int8')]
    for r in range(args.rounds):
        for name,fmode,cmode in (configs if r%2==0 else configs[::-1]):
            fs=session('face.'+fmode+'.onnx')
            cs=None if cmode=='torch' else {k:session(k+'.'+cmode+'.onnx') for k in ('image','text')}
            def process(i):
                vector=face_run(fs,i)
                pair=torch_clip(i%len(images)) if cmode=='torch' else onnx_clip(cs,i%len(images))
                return vector,pair
            measured('combined_kernels',name,process,len(faces),r)
            del fs,cs;gc.collect()
    report['complete']=True;save();print('BENCHMARK_COMPLETE',flush=True)


if __name__=='__main__':main()
