"""Isolated Azure qualification and bounded compute/apply experiments.

No application entrypoint, production credentials or deployment changes. Uses
production source claims, apply/semantic/receipt/ack, admission and qualification.
Blob derivatives, stage ownership, wave freezing and publication are excluded.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import ExitStack
from contextvars import copy_context
import hashlib
import json
import os
from pathlib import Path
import resource
import threading
import time
import uuid

from azure.core.exceptions import ResourceNotFoundError
from benchmark_vision_stages import cpu_stat, fingerprint
from benchmark_vision_persistence import Meter, configure, inputs, patch
import processing_fence as fencing
from processing_completion import STEPS, prepare_completion
from processing_generation import allocate_generation, current_contract
from processing_receipts import ArtifactReceipts
from upload_waves import ShadowWaves, digest
from upload_wave_execution import _observe_qualified
from wave_source_batch import MetadataBatch, InspectionBatch, ReceiptBatch, reserve_attempts
from wave_settlement import source_state
import storage_utils as storage


def batch_create(table, entities):
    grouped = {}
    for entity in entities:
        grouped.setdefault(entity['PartitionKey'], []).append(entity)
    for rows in grouped.values():
        for offset in range(0, len(rows), 50):
            table.submit_transaction([('create', row) for row in rows[offset:offset+50]])


def source_row(library, index, *, complete=True):
    filename, photo = f'{index:05}.jpg', f'photo-{index}'
    row = dict(PartitionKey=library, RowKey=filename, anonymousImageId=photo, fileHash='a'*64,
        processing_lease_owner='', processing_lease='', processing_lease_expires_at='',
        faceCount=0, **allocate_generation('rev', photo_id=photo, file_hash='a'*64, is_video=False),
        **{step+'_status': 'no_data' for step in STEPS})
    if not complete:
        row.update({step+'_status': 'pending' for step in ('ocr', 'face', 'ai_vision')})
    return row


def member_row(store, wave, row):
    return dict(PartitionKey=store.partition, RowKey=f'e!{wave}!m!{row["RowKey"]}',
        waveId=wave, filename=row['RowKey'], photoId=row['anonymousImageId'],
        processingRevision=row['processingRevision'], outcome='pending',
        requiredSteps=json.dumps(sorted(STEPS)))


def seed_completed(tables, images, faces=1, background=0):
    """Valid immutable fixture rows; fixture construction is outside timing."""
    library, wave = 'bench-'+uuid.uuid4().hex, uuid.uuid4().hex
    store = ShadowWaves(tables['jobs'], library)
    receipts = ArtifactReceipts(tables['jobs'], library)
    metadata, jobs, members = [], [], []
    version = current_contract()['faceEmbeddingVersions'][0]
    for i in range(images):
        row = source_row(library, i)
        row.update(faceCount=faces, face_status='done' if faces else 'no_data', processingFence='attempt')
        snapshots = []
        for face_index in range(faces):
            payload = dict(PartitionKey=library, RowKey=f'face-{i}-{face_index}', filename=row['RowKey'],
                sourcePhotoId=row['anonymousImageId'], processingRevision='rev', processingFence='attempt',
                embeddingVersion=version, embedding=json.dumps(inputs(1)[0]['embedding']), bbox='{}')
            identity, entity = receipts._prepare('face', payload)
            snapshots.append(identity); jobs.append(entity)
        payload = dict(schemaVersion=1, library=library, filename=row['RowKey'], photoId=row['anonymousImageId'],
            revision='rev', statuses={step: row[step+'_status'] for step in STEPS}, derivatives={},
            faceCount=faces, faceSnapshots=snapshots, faceLookupGeneration='fixture')
        identity, entity = receipts._prepare('manifest', payload); jobs.append(entity)
        row['artifactReceipt'] = json.dumps(dict(id=identity, library=library, filename=row['RowKey'],
            photoId=row['anonymousImageId'], revision='rev'))
        prepare_completion(row)
        intent = json.loads(row['processingCompletionOutbox'])[0]
        # Accepted fixture is the same canonical payload consumed by qualification.
        from processing_completion import validate_intent
        key = 'source-completion!'+digest([row['anonymousImageId'], 'rev'])+'!'+intent['id']
        jobs.append(dict(PartitionKey=store.partition, RowKey=key, completionId=intent['id'],
            payload=json.dumps(validate_intent(intent, library), sort_keys=True, separators=(',', ':')),
            artifactReceiptQualified=True, completionQualified=False))
        metadata.append(row); members.append(member_row(store, wave, row))
    batch_create(tables['metadata'], metadata); batch_create(tables['jobs'], jobs)
    # Mix namespace/key ranges rather than placing all padding beyond the target range.
    for offset in range(0, background, 50):
        ids = range(offset, min(background, offset+50))
        batch_create(tables['metadata'], [dict(PartitionKey=library, RowKey=f'{i%images:05}.jpg-padding-{i}', padding='x'*256) for i in ids])
        batch_create(tables['jobs'], [dict(PartitionKey=store.partition,
            RowKey=('deleted-source!' if i%2 else 'source-completion!')+hashlib.sha256(str(i).encode()).hexdigest(), padding='x'*256) for i in ids])
    return store, members


def qualify_group(store, metadata, members, mode):
    """New snapshots after sources finish; never reuse pre-processing absence."""
    def one(member, rows, receipts, inspection=None, current_metadata=None):
        if source_state(store, member['waveId'], member, rows,
                        proofs=inspection.proofs if inspection is not None else None):
            return False
        completion, row = _observe_qualified(store, member, current_metadata or rows, receipts,
            inboxes=inspection.inboxes if inspection is not None else None)
        return bool(completion and row)
    if mode == 'parallel':
        def worker(member):
            # Per-source receipt cache avoids sharing mutable LocalRows across threads.
            with ReceiptBatch(store.table, store.library) as receipts:
                return one(member, metadata, receipts)
        with ThreadPoolExecutor(4) as pool:
            return list(pool.map(worker, members))
    with ReceiptBatch(store.table, store.library) as receipts:
        if mode == 'batch':
            rows = MetadataBatch(metadata, store.library, members)
            inspection = InspectionBatch(store, members, rows, receipts)
            # Prefetch plans evidence reads, but cannot authorize completion
            # against a source replaced/leased since the batch was fetched.
            return [one(m, rows, receipts, inspection, current_metadata=metadata) for m in members]
        if mode != 'serial':
            raise ValueError(mode)
        return [one(m, metadata, receipts) for m in members]


def measured_qualification(raw, *, images, faces, background, mode, fixture=None):
    counts, lock = Counter(), threading.Lock()
    tables = {n: Meter(t, n, counts, lock) for n, t in raw.items()}
    store, members = fixture or seed_completed(tables, images, faces, background)
    # Fixture may have raw clients; point the benchmark store at this run's meter.
    store = ShadowWaves(tables['jobs'], store.library)
    counts.clear(); before, cpu, start = cpu_stat(), time.process_time(), time.perf_counter()
    result = []
    for offset in range(0, len(members), 20):
        result.extend(qualify_group(store, tables['metadata'], members[offset:offset+20], mode))
    wall, cpu_used = time.perf_counter()-start, time.process_time()-cpu
    if not all(result) or len(result) != images:
        raise AssertionError('Qualification result mismatch')
    return dict(section='qualification', library=store.library, images=images, faces=faces, background=background, mode=mode,
        wall_seconds=wall, process_cpu_seconds=cpu_used, sdk_calls=sum(counts.values()), calls=dict(counts),
        cgroup_delta={k:v-before.get(k,0) for k,v in cpu_stat().items()}, output_digest=fingerprint(result))


def bounded_current(items, process, *, workers=3, admit=lambda items: None):
    remaining=iter(items); exhausted=False; active={}; results=[]
    with ThreadPoolExecutor(workers) as pool:
        while True:
            ready=[]
            while not exhausted and len(active)+len(ready)<workers:
                item=next(remaining,None)
                if item is None:
                    exhausted=True;break
                ready.append(item)
            if ready:
                admit(ready)
                for item in ready:
                    active[pool.submit(copy_context().run,process,item)]=item
            if not active:break
            done,_=wait(active,return_when=FIRST_COMPLETED)
            for future in done:
                active.pop(future);results.append(future.result())
    return results


def bounded_handoff(items, compute, apply, *, compute_workers=2, apply_workers=2, limit=6,
                    byte_limit=32*1024*1024, admit=lambda items: None):
    """Unified completion means apply completed; all failure paths drain pools.

    In-flight count is a strict cap. Byte budget controls admitting more compute
    jobs once completed outputs fill the apply queue. Each result has an explicit
    per-item cap so compute slots reserve enough headroom before they start.
    """
    if min(compute_workers, apply_workers, limit, byte_limit) < 1:
        raise ValueError('Positive bounded capacities required')
    per_item = byte_limit//limit
    if not per_item:
        raise ValueError('Byte budget smaller than source capacity')
    remaining = iter(items); exhausted = False
    running, applying, results = {}, {}, []
    peak_count = peak_bytes = pending_bytes = 0
    with ThreadPoolExecutor(compute_workers) as cp, ThreadPoolExecutor(apply_workers) as ap:
        try:
            while True:
                ready=[]
                while not exhausted and len(running)+len(ready)<compute_workers and len(running)+len(applying)+len(ready)<limit:
                    item = next(remaining, None)
                    if item is None:
                        exhausted = True; break
                    ready.append(item)
                if ready:
                    admit(ready)
                for item in ready:
                    running[cp.submit(copy_context().run, compute, item)] = item
                    peak_count = max(peak_count, len(running)+len(applying))
                if not running and not applying:
                    break
                done, _ = wait([*running, *applying], return_when=FIRST_COMPLETED)
                for future in done:
                    if future in applying:
                        size = applying.pop(future)
                        pending_bytes -= size
                        results.append(future.result())
                    else:
                        running.pop(future)
                        value = future.result()
                        size = len(json.dumps(value, default=str).encode())
                        if size > per_item:
                            raise ValueError('Compute output exceeds its reserved byte budget')
                        pending_bytes += size
                        peak_bytes = max(peak_bytes, pending_bytes)
                        if pending_bytes > byte_limit:
                            raise AssertionError('Byte budget exceeded')
                        applying[ap.submit(copy_context().run, apply, value)] = size
            return results, dict(peak_inflight=peak_count, peak_apply_bytes=peak_bytes,
                inflight_limit=limit, byte_limit=byte_limit, pending_at_finish=0)
        except BaseException:
            # Futures already admitted must finish/fail before scopes/resources exit.
            wait([*running, *applying])
            raise


def configure_pipeline(tables, store):
    configure(tables)
    storage._CTX.update(embeddings_table_client=tables['photoembeddings'],
        source_artifact_receipt_capture=lambda library, row: ArtifactReceipts(tables['jobs'], library).capture(storage, row),
        source_completion_delivery=lambda library, intent: store.accept_source_completion(intent))


def pipeline_run(raw, samples, indices, models, references, *, mode, qualification='serial'):
    from processing_cleanup import source_policy_scope, check_source_policy
    from processing_fence import ResultFence
    counts, lock = Counter(), threading.Lock()
    tables = {n: Meter(t,n,counts,lock) for n,t in raw.items()}
    library, wave = 'bench-'+uuid.uuid4().hex, uuid.uuid4().hex
    store = ShadowWaves(tables['jobs'], library); configure_pipeline(tables, store)
    storage._CTX['source_policy_check'] = lambda uid: check_source_policy(tables['jobs'], uid)
    rows = [source_row(library, i, complete=False) for i in range(len(indices))]
    if samples:
        for i,row in enumerate(rows):
            source_hash=hashlib.sha256(samples[indices[i]]).hexdigest()
            row.update(fileHash=source_hash,**allocate_generation('rev',photo_id=row['anonymousImageId'],
                file_hash=source_hash,is_video=False))
    members = [member_row(store, wave, row) for row in rows]
    batch_create(tables['metadata'], rows)
    batch_create(tables['lookup'], [dict(PartitionKey=library, RowKey=r['RowKey'],
        schemaVersion=storage._FACE_FILENAME_SCHEMA_VERSION, generation=uuid.uuid4().hex,
        state='complete', faceIds='[]', leaseExpiresAt='') for r in rows])
    # An active cleanup policy row makes admission and completion retain real CAS guards.
    tables['jobs'].submit_transaction([('create',dict(PartitionKey=store.partition, RowKey='policy', cleanupState=''))])
    timings = Counter(); leases = {}; completed = []; queue_stats = []
    def compute(position):
        began = time.perf_counter(); member = members[position]
        lease = storage.claim_processing_lease(library, member['filename'], 'bench-'+uuid.uuid4().hex,
            steps=['ocr','face','ai_vision'], mark_running=False)
        with lock:
            leases[position] = lease
        output = models(indices[position])
        model_digest = fingerprint(output)
        with lock:
            if references.setdefault(('model', indices[position]), model_digest) != model_digest:
                raise AssertionError('Model result changed')
            timings['compute_seconds_sum'] += time.perf_counter()-began
        return dict(position=position, output=output, model_digest=model_digest,
            source_started=began,compute_finished=time.perf_counter())
    def apply(bundle):
        began = time.perf_counter(); position = bundle['position']; row = rows[position]; lease = leases[position]
        # Validate at the handoff boundary; production apply also rereads/fences CAS.
        ResultFence(tables['metadata'], library, row['RowKey'], 'rev', lease['entity']['processingFence'],
            lease['ownerId'], row['anonymousImageId'], generation=row['processingGeneration']).check()
        result = storage.apply_client_processing_results_for_file(library, row['RowKey'],
            client_processing=bundle['output'], origin='ipworker', claimed_steps=['ocr','face','ai_vision'],
            lease_owner=lease['ownerId'], expected_processing_revision='rev',
            expected_processing_fence=lease['entity']['processingFence'])
        storage.release_processing_lease(library,row['RowKey'],lease['ownerId'],
            expected_processing_fence=lease['entity']['processingFence'])
        with lock:
            timings['apply_seconds_sum'] += time.perf_counter()-began
        return dict(position=position, model_digest=bundle['model_digest'],
            source_ms=(time.perf_counter()-bundle['source_started'])*1000,
            apply_queue_ms=(began-bundle['compute_finished'])*1000,
            compute_ms=(bundle['compute_finished']-bundle['source_started'])*1000,
            apply_ms=(time.perf_counter()-began)*1000)
    def sync(position):
        return apply(compute(position))
    before, cpu, start = cpu_stat(), time.process_time(), time.perf_counter(); counts.clear()
    with source_policy_scope(tables['jobs'], library), patch(fencing,'POLICY_CHECK',storage._CTX['source_policy_check']):
        def admit(positions):
            admitted=reserve_attempts(store,[members[i] for i in positions])
            if not all(allowed for _,allowed in admitted.values()):
                raise AssertionError('Admission budget denied; do not start this source')
        for offset in range(0, len(indices), 20):
            positions = list(range(offset,min(len(indices),offset+20)))
            if mode == 'current3':
                completed.extend(bounded_current(positions,sync,admit=admit))
            else:
                io_workers = int(mode.removeprefix('handoff2x'))
                results, stats = bounded_handoff(positions,compute,apply,compute_workers=2,
                    apply_workers=io_workers,limit=2+io_workers,admit=admit)
                completed.extend(results); queue_stats.append(stats)
            then = time.perf_counter()
            qualified = qualify_group(store,tables['metadata'],members[offset:offset+20],qualification)
            timings['qualification_seconds'] += time.perf_counter()-then
            if not all(qualified):
                raise AssertionError('Unqualified source after completed apply')
    wall, cpu_used = time.perf_counter()-start,time.process_time()-cpu
    cgroup = {k:v-before.get(k,0) for k,v in cpu_stat().items()}
    if len(completed)!=len(indices):
        raise AssertionError('Unfinished sources')
    # Fresh durable readback includes semantic vectors, completed ACK and face membership.
    canonical=[]
    for position,row in enumerate(rows):
        current=raw['metadata'].get_entity(library,row['RowKey'])
        if any(current.get(k) for k in ('processing_lease_owner','processing_lease','processing_lease_expires_at')) or current.get('processingCompletionOutbox') not in ('[]','',None):
            raise AssertionError('Lease/outbox not drained')
        vectors=raw['photoembeddings'].get_entity(library,row['RowKey'])
        semantic=json.loads(vectors['semanticEmbedding'])
        if len(semantic)!=512 or not vectors.get('semanticTextDigest'):
            raise AssertionError('Real semantic result required')
        key=('semantic',indices[position])
        if references.setdefault(key,fingerprint(semantic))!=fingerprint(semantic):
            raise AssertionError('Semantic result changed')
        ids=json.loads(raw['lookup'].get_entity(library,row['RowKey'])['faceIds'])
        face_values=[]
        for fid in ids:
            face=raw['faces'].get_entity(library,fid);embedding=raw['embeddings'].get_entity(library,fid)
            face_values.append(dict(bbox=face['bbox'],embedding=embedding['embedding']))
        canonical.append(dict(model=next(r['model_digest'] for r in completed if r['position']==position),
            semantic=fingerprint(semantic),faces=sorted(face_values,key=lambda v:v['bbox']),
            statuses={step:current.get(step+'_status') for step in STEPS}))
    from benchmark_vision_stages import distribution
    latency={k:distribution([r[k] for r in completed]) for k in ('source_ms','apply_queue_ms','compute_ms','apply_ms')}
    return dict(section='pipeline',library=library,mode=mode,qualification=qualification,images=len(indices),latency=latency,
        wall_seconds=wall,process_cpu_seconds=cpu_used,cgroup_delta=cgroup,sdk_calls=sum(counts.values()),
        calls=dict(counts),timings=dict(timings),queue_stats=queue_stats,
        process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        output_digest=fingerprint(canonical))


def main():
    from azure.data.tables import TableServiceClient
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--section',choices=('all','combined'),default='all')
    parser.add_argument('--handoff-mode',choices=('handoff2x1','handoff2x2','handoff2x3'),default='handoff2x2')
    args=parser.parse_args()
    os.environ['UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED']='true'
    os.environ['PROCESSING_GENERATION_ENFORCEMENT_ENABLED']='true'
    storage._LOGGER.setLevel('ERROR')
    import processing_diagnostics
    processing_diagnostics._LOGGER.setLevel('WARNING')
    service=TableServiceClient.from_connection_string(os.environ['BENCHMARK_STORAGE_CONNECTION'])
    tables={n:service.get_table_client('bench'+n) for n in ('metadata','faces','embeddings','lookup','jobs','photoembeddings')}
    reports=[];references={}
    def save(row):
        reports.append(row)
        args.output.write_text(json.dumps(dict(scope=__doc__,output_parity=True,
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            cpu_max=Path('/sys/fs/cgroup/cpu.max').read_text().strip(),reports=reports),indent=2))
        print('ROUND '+json.dumps({k:row[k] for k in ('section','mode','images','wall_seconds','sdk_calls')}),flush=True)
    # Main workload plus small-upload and face-count/partition controls.
    qualification_matrix=[(1000,1,0),(200,1,10000),(20,0,0),(100,8,0)] if args.section=='all' else []
    for images,faces,background in qualification_matrix:
        fixture=seed_completed(tables,images,faces,background)
        for round_number in range(args.rounds):
            modes=['serial','parallel','batch']
            for mode in modes if round_number%2==0 else reversed(modes):
                row=measured_qualification(tables,images=images,faces=faces,background=background,mode=mode,fixture=fixture)
                row['round']=round_number;save(row)
    from image_utils import convert_image_to_jpeg
    from ipwork_image import image_job
    import ipwork_face as face
    import ipwork_ocr as ocr
    import ipwork_vision as vision
    import vision_utils
    face.FACE_LANDMARKER_MODEL_PATH='/app/models/face_landmarker.task'
    samples=[]
    for row in json.loads(args.manifest.read_text()):
        data=convert_image_to_jpeg(Path(row['path']).read_bytes(),row['filename'])
        if not data:raise ValueError('Preview conversion failed')
        samples.append(data)
    if not vision_utils.image_encoder_available() or not vision._load_vocabulary():
        raise RuntimeError('Actual models required')
    def models(index):
        with image_job(True):
            output={key:fn('benchmark',f'{index:04}.jpg',samples[index]) for key,fn in
                [('ocr',ocr.process_ocr),('face',face.process_face),('ai_vision',vision.process_vision)]}
        if any(value.get('error') for value in output.values()) or not output['face']['faceDiagnostics']['detectorModelReady']:
            raise RuntimeError('Model failure')
        return output
    with ThreadPoolExecutor(3) as pool:
        warmed=list(pool.map(models,range(len(samples))))
    positive=[i for i,r in enumerate(warmed) if any(storage._client_face_passes_quality_gate(f) for f in r['face'].get('faces',[]))]
    if not positive:raise RuntimeError('Empty face-positive profile')
    if args.section=='combined':
        for round_number in range(args.rounds):
            cases=[('current3','serial'),('current3','batch'),(args.handoff_mode,'serial'),(args.handoff_mode,'batch')]
            for mode,qualification in cases if round_number%2==0 else reversed(cases):
                row=pipeline_run(tables,samples,positive*4,models,references,mode=mode,qualification=qualification)
                row.update(profile='combined_validation',round=round_number)
                key=('combined_validation',)
                if references.setdefault(key,row['output_digest'])!=row['output_digest']:
                    raise AssertionError('Combined pipeline output mismatch')
                save(row)
        return
    for profile,indices in [('mixed',list(range(len(samples)))),('face_positive',positive*4),('small',list(range(20)))]:
        for round_number in range(args.rounds):
            modes=['current3','handoff2x1','handoff2x2','handoff2x3']
            for mode in modes if round_number%2==0 else reversed(modes):
                row=pipeline_run(tables,samples,indices,models,references,mode=mode)
                row.update(profile=profile,round=round_number)
                key=('pipeline',profile)
                if references.setdefault(key,row['output_digest'])!=row['output_digest']:
                    raise AssertionError('Pipeline output mismatch')
                save(row)
    # Keep a matched full-component control for the winning qualification path.
    for round_number in range(args.rounds):
        modes=['serial','batch']
        for mode in modes if round_number%2==0 else reversed(modes):
            row=pipeline_run(tables,samples,positive*4,models,references,mode='current3',qualification=mode)
            row.update(profile='qualification_integrated',round=round_number);save(row)


if __name__=='__main__':
    main()
