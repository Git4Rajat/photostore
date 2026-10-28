"""Real-model text batching: offline capacity and current two-source arrivals.

Uses synthetic captions, real cached CLIP weights and production text encoders.
No hashing fallback is accepted. No private OCR/caption text enters the report.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import vision_utils as vision
from semantic_batching import TextBatcher


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--texts',type=int,default=32)
    args=parser.parse_args()
    if not vision._load_model(): raise RuntimeError('Real cached CLIP model required')
    texts=[f'A photograph of {scene}. {detail}' for scene in
        ('a family at the beach','a mountain lake','a crowded city street','a receipt on a desk',
         'a birthday party','a dog in a garden','a train station','snow covered forest')
        for detail in ('','Sunset and warm light.','Visible signs and buildings in the background.',
                       'People, trees, water and distant hills. The photograph records a holiday.')][:args.texts]
    if len(texts)<args.texts:raise ValueError('At most 32 unique texts')
    reference=np.asarray(vision.encode_text_embeddings_batch(texts),dtype=np.float32)
    if reference.shape!=(len(texts),512):raise RuntimeError('Unexpected real CLIP output')
    for size in (1,2,4,8):vision.encode_text_embeddings_batch(texts[:size])
    reports=[]
    def validate(values):
        value=np.asarray(values,dtype=np.float32)
        if value.shape!=reference.shape or not np.isfinite(value).all():raise RuntimeError('Invalid embeddings')
        cosine=np.sum(value*reference,axis=1)/(np.linalg.norm(value,axis=1)*np.linalg.norm(reference,axis=1))
        delta=float(np.max(np.abs(value-reference)))
        if float(cosine.min())<.99999 or delta>1e-4:raise RuntimeError('Embedding parity failed')
        return dict(min_cosine=float(cosine.min()),max_abs_delta=delta)
    for round_number in range(args.rounds):
        sizes=(1,2,4,8) if round_number%2==0 else (8,4,2,1)
        for size in sizes:
            started,cpu=time.perf_counter(),time.process_time()
            values=[]
            for offset in range(0,len(texts),size):
                values.extend(vision.encode_text_embeddings_batch(texts[offset:offset+size]))
            reports.append(dict(scenario='offline_capacity',batch_limit=size,round=round_number,
                texts=len(texts),wall_seconds=time.perf_counter()-started,cpu_seconds=time.process_time()-cpu,
                **validate(values)))
        # Two source workers cannot fill a batch of 4/8 while each blocks for
        # its result. Compare the current broker against synchronous single.
        modes=['direct','broker2','broker4','broker8']
        for gap_ms in (0,100):
            for mode in modes if round_number%2==0 else reversed(modes):
                calls=[];latencies=[];lock=threading.Lock()
                def encode(batch):
                    before=time.perf_counter();result=vision.encode_text_embeddings_batch(batch)
                    with lock:calls.append(dict(items=len(batch),service_ms=(time.perf_counter()-before)*1000))
                    return result
                batcher=None if mode=='direct' else TextBatcher(encode,
                    vision._encode_text_embedding_single,batch_size=int(mode[-1]),window=.005)
                def worker(parity):
                    result=[]
                    for index in range(parity,len(texts),2):
                        if gap_ms:time.sleep(gap_ms/1000)
                        before=time.perf_counter()
                        vector=(vision._encode_text_embedding_single(texts[index]) if batcher is None
                                else batcher.submit(texts[index]))
                        with lock:latencies.append((time.perf_counter()-before)*1000)
                        result.append((index,vector))
                    return result
                started,cpu=time.perf_counter(),time.process_time()
                try:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        pairs=[pair for part in pool.map(worker,(0,1)) for pair in part]
                    elapsed,cpu_used=time.perf_counter()-started,time.process_time()-cpu
                finally:
                    if batcher:batcher.close()
                values=[v for _,v in sorted(pairs)]
                reports.append(dict(scenario='two_sources',mode=mode,round=round_number,
                    synthetic_work_between_requests_ms=gap_ms,texts=len(texts),wall_seconds=elapsed,
                    cpu_seconds=cpu_used,request_median_ms=statistics.median(latencies),
                    request_p95_ms=float(np.percentile(latencies,95)),batch_sizes=[c['items'] for c in calls],
                    batch_service_ms=sum(c['service_ms'] for c in calls),**validate(values)))
            args.output.write_text(json.dumps(dict(platform=platform.platform(),device=vision._device(),
                torch_threads=vision.torch.get_num_threads(),scope='Real CLIP text inference; synthetic text and arrival gaps',
                rss_scope='Process lifetime peak including model load',reports=reports),indent=2)+'\n')
            print(f'Completed round {round_number+1}, gap {gap_ms} ms',flush=True)
    import resource
    output=json.loads(args.output.read_text())
    output['process_peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
    args.output.write_text(json.dumps(output,indent=2)+'\n')


if __name__=='__main__':main()
