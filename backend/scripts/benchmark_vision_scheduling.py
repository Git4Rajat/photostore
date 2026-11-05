"""Matched real-model scheduling experiments, bounded to two or four images.

No application import or storage writes. Reports contain hashes and aggregate
timings only. Use the same manifest and native thread limits as stage benchmarks.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import hashlib
import json
import os
from pathlib import Path
import platform
import threading
import time

from benchmark_vision_stages import cpu_stat, distribution, fingerprint


def resident_memory_bytes():
    statm = Path('/proc/self/statm')
    if statm.exists():
        return int(statm.read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    import psutil
    return psutil.Process().memory_info().rss


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--face-landmarker', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=4)
    args = parser.parse_args()
    if args.rounds < 2:
        parser.error('Use at least two alternating rounds')
    from image_utils import convert_image_to_jpeg
    from ipwork_image import image_job, rgb_image
    import ipwork_face as face
    import ipwork_ocr as ocr
    import ipwork_vision as vision
    import processing_metrics as metrics
    import vision_utils
    face.FACE_LANDMARKER_MODEL_PATH = args.face_landmarker
    samples = []
    for item in json.loads(args.manifest.read_text()):
        path = Path(item['path'])
        name = item.get('filename', path.name)
        data = convert_image_to_jpeg(path.read_bytes(), name)
        if not data:
            raise RuntimeError('Preview preparation failed')
        samples.append((name, data))
    if not samples or not vision_utils.image_encoder_available() or not vision._load_vocabulary():
        raise RuntimeError('Samples and real CLIP model/vocabulary required')
    functions = {'ocr': ocr.process_ocr, 'face': face.process_face, 'clip': vision.process_vision}
    reference = {}
    reports = []
    modes = ['ocr_face_clip', 'clip_ocr_face', 'async_clip_one', 'async_clip_two',
             'parallel_stages_two', 'parallel_stages_four']

    def run(mode, warmup=False):
        count = min(12, len(samples)) if warmup else len(samples)
        workers = 4 if mode == 'parallel_stages_four' else 2
        separate = mode.startswith('parallel_stages')
        asynchronous = mode.startswith('async_clip')
        timings = []
        timing_lock = threading.Lock()
        with ThreadPoolExecutor(1 if mode != 'async_clip_two' else 2) as clip_pool, \
                ThreadPoolExecutor(1) as ocr_pool, ThreadPoolExecutor(1) as face_pool:
            pools = {'clip': clip_pool, 'ocr': ocr_pool, 'face': face_pool}
            def stage(name, filename, data, queued):
                started = time.perf_counter()
                result = functions[name]('offline-benchmark', filename, data)
                if result.get('error') or result.get('faceFailureStage') not in (None, '', 'quality_filter_rejected'):
                    raise RuntimeError('Model stage failed: ' + name)
                with timing_lock:
                    timings.append((name, (started - queued) * 1000,
                                    (time.perf_counter() - started) * 1000))
                return result
            def image(index):
                filename, data = samples[index]
                result = {}
                with image_job(True):
                    with rgb_image(data, filename):
                        pass
                    if separate:
                        futures = {name: pools[name].submit(copy_context().run, stage,
                                   name, filename, data, time.perf_counter()) for name in functions}
                        result = {name: future.result() for name, future in futures.items()}
                    elif asynchronous:
                        future = clip_pool.submit(copy_context().run, stage, 'clip', filename, data, time.perf_counter())
                        for name in ('ocr', 'face'):
                            result[name] = stage(name, filename, data, time.perf_counter())
                        result['clip'] = future.result()
                    else:
                        order = ('clip', 'ocr', 'face') if mode == 'clip_ocr_face' else ('ocr', 'face', 'clip')
                        for name in order:
                            result[name] = stage(name, filename, data, time.perf_counter())
                return dict(sample=index, hash=fingerprint(result),
                            faces=len(result['face'].get('faces') or []),
                            text=bool(result['ocr'].get('text')),
                            decisions=fingerprint([result['clip']['tags'], result['clip']['aiPersonCandidate'],
                                                   result['clip']['aiPersonLabel']]))
            with metrics.accounting() as counter, ThreadPoolExecutor(workers) as pool:
                futures = [pool.submit(copy_context().run, image, index) for index in range(count)]
                rows = [future.result() for future in futures]
            return rows, timings, counter.snapshot()

    warm_started = time.perf_counter()
    run('ocr_face_clip', warmup=True)
    warmup_seconds = time.perf_counter() - warm_started
    for round_number in range(args.rounds):
        for mode in modes if round_number % 2 == 0 else reversed(modes):
            stop = threading.Event()
            memory = [resident_memory_bytes()]
            def sample_memory():
                while not stop.wait(.02):
                    memory.append(resident_memory_bytes())
            sampler = threading.Thread(target=sample_memory, daemon=True)
            sampler.start()
            started, cpu_before, stat_before = time.perf_counter(), time.process_time(), cpu_stat()
            try:
                rows, timings, counters = run(mode)
            finally:
                stop.set()
                sampler.join()
            elapsed = time.perf_counter() - started
            for row in rows:
                reference.setdefault(row['sample'], row)
            report = dict(mode=mode, round=round_number, images=len(rows), wall_seconds=elapsed,
                          process_cpu_seconds=time.process_time() - cpu_before,
                          cgroup_delta={key: value - stat_before.get(key, 0) for key, value in cpu_stat().items()},
                          rss_start_bytes=memory[0], rss_peak_bytes=max(memory),
                          changed_output_samples=[row['sample'] for row in rows if row != reference[row['sample']]],
                          changed_decision_samples=[row['sample'] for row in rows
                              if row['decisions'] != reference[row['sample']]['decisions']],
                          accepted_faces=sum(row['faces'] for row in rows),
                          text_positive=sum(row['text'] for row in rows),
                          stage_execution_ms={name: distribution([execution for stage_name, _, execution in timings
                                                               if stage_name == name]) for name in functions},
                          stage_queue_ms={name: distribution([queued for stage_name, queued, _ in timings
                                                           if stage_name == name]) for name in functions},
                          counters={key: value for key, value in counters.items() if value})
            reports.append(report)
            print(json.dumps(report), flush=True)
            args.output.write_text(json.dumps(dict(platform=platform.platform(), cpu_quota=(
                Path('/sys/fs/cgroup/cpu.max').read_text() if Path('/sys/fs/cgroup/cpu.max').exists() else None),
                native_threads={name: os.environ.get(name) for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS')},
                warmup_seconds=warmup_seconds, sample_count=len(samples),
                input_preview_sha256=[hashlib.sha256(data).hexdigest() for _, data in samples],
                reports=reports), indent=2))


if __name__ == '__main__':
    main()
