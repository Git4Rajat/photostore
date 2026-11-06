"""Offline scale qualification for the CURRENT optimized frozen-wave runner.

Real 512D FAISS/training, SQLite, fsync and checkpoint/restore; fake remote
storage. Never contacts Azure. No production limit is relaxed. Each invocation
is one isolated scenario; failures are recorded, not converted to success.
"""
import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import platform
import resource
import shutil
import sys
import tempfile
import threading
import time
import traceback

import numpy as np
import psutil
import pytest

from benchmark_wave_coverage import BenchmarkTable
import app
import automatic_index_updates as automatic
import clustering_index as ci
import clustering_checkpoint as checkpoints
from clustering_runtime import LiveFaceIndex
from execution_lease import execution_scope
import processing_diagnostics as diagnostics
import processing_metrics as metrics
import storage_utils as su
import wave_assignment as wave
import wave_assignment_resume as resume
from wave_assignment_files import FileAssignmentJournal
from test_wave_assignment_journal import setup


def tree_bytes(root):
    total = 0
    for path in Path(root).rglob('*'):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            pass  # Native checkpoint publication/cleanup can rename a file.
    return total


class Sink:
    def append(self, value):
        pass


class Resources:
    def __init__(self, root):
        self.root = Path(root)
        self.process = psutil.Process()
        self.stop = threading.Event()
        self.peak_rss = self.process.memory_info().rss
        self.peak_work = self.peak_mount = self.peak_disk = 0
        self.errors = []
        self.thread = threading.Thread(target=self.poll, daemon=True)

    def sample(self):
        self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)
        work, mount = tree_bytes(self.root/'work'), tree_bytes(self.root/'mount')
        self.peak_work = max(self.peak_work, work)
        self.peak_mount = max(self.peak_mount, mount)
        self.peak_disk = max(self.peak_disk, work+mount)

    def poll(self):
        while not self.stop.wait(.5):
            try:
                self.sample()
            except Exception as error:
                self.errors.append(str(error))
                return

    def finish(self):
        self.stop.set()
        self.thread.join()
        self.sample()
        # ru_maxrss is bytes on macOS, KiB on Linux. It covers the full child,
        # including imports/seeding; sampled RSS additionally covers the runner.
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return dict(sampled_runner_peak_rss_bytes=self.peak_rss,
            process_lifetime_peak_rss_bytes=int(peak if sys.platform=='darwin' else peak*1024),
            peak_work_bytes=self.peak_work, peak_mount_bytes=self.peak_mount,
            peak_combined_disk_bytes=self.peak_disk,
            retained_mount_bytes=tree_bytes(self.root/'mount'), sampling_errors=self.errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--faces', type=int, required=True)
    parser.add_argument('--identities', type=int, default=128)
    parser.add_argument('--slice-seconds', type=float, default=120)
    parser.add_argument('--first-cut', type=int, default=4096,
        help='Force first checkpoint at this page-aligned prefix, then use time slices; 0 disables.')
    parser.add_argument('--seed', type=int, default=20261106)
    parser.add_argument('--fail-on-restore-miss', action='store_true',
        help='Stop qualification after an unmodified published snapshot fails to restore; do not replay it indefinitely.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not (1 <= args.faces <= 100000 and 1 <= args.identities <= 512
            and args.slice_seconds > 0 and args.first_cut >= 0 and args.first_cut % 128 == 0):
        parser.error('faces 1..100000; identities 1..512; positive slice seconds; first-cut multiple of 128')
    if shutil.disk_usage(tempfile.gettempdir()).free < 10 * 1024**3:
        raise RuntimeError('Scale benchmark requires at least 10 GiB free temporary space')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    state = dict(schema=1, status='preparing', faces=args.faces, identities=args.identities,
        dimensions=512, seed=args.seed, slice_seconds=args.slice_seconds, first_cut=args.first_cut,
        started_at=datetime.now(timezone.utc).isoformat(),
        environment=dict(python=sys.version, platform=platform.platform(), cpus=psutil.cpu_count(),
                         host_memory_bytes=psutil.virtual_memory().total, faiss=ci.faiss.__version__),
        source_sha256={name: hashlib.sha256((Path(__file__).resolve().parents[1]/name).read_bytes()).hexdigest()
            for name in ['wave_assignment.py','wave_assignment_files.py','wave_assignment_resume.py',
                         'clustering_checkpoint.py','clustering_runtime.py','clustering_index.py','faiss_assignment.py',
                         'automatic_index_updates.py']},
        scope='Current optimized frozen assignment runner and continuation. Fake remote storage; real local durability.',
        limitations=['No upload, Vision, receipt-loader, index publication or Azure latency.',
            'Synthetic noisy identity vectors; not a face-quality/recall qualification.',
            'Fake cloud rows remain in process RSS; RSS is not production native-runtime memory alone.',
            'Fake tables do not enforce all Azure wire/property limits; production journal/membership guards remain.',
            'One run per scenario, no claimed median or production throughput forecast.',
            'First checkpoint is forced to exercise resume; later slices use the actual soft time budget.',
            'Disk sizes sampled every 0.5s; short transient peaks can be missed.'],
        attempts=[], builds=[], rebuilds=[], saves=[], restores=[])
    output_lock = threading.RLock()
    def save():
        with output_lock:
            tmp = args.output.with_suffix('.tmp')
            tmp.write_text(json.dumps(state, indent=2, default=str)+'\n')
            tmp.replace(args.output)
    def emit(event, **fields):
        print(json.dumps(dict(event=event, **fields), default=str), flush=True)
    save()
    h, _, store, frozen, leases = setup()
    h.events = Sink()  # Do not retain test-only per-guard-check event history.
    for table in (h.faces, h.people, h.embeddings, h.members):
        table.rows.clear(); table.versions.clear()
    for identity in range(args.identities):
        vector = [float(i==identity) for i in range(512)]
        person, face = f'person-{identity:04d}', f'anchor-{identity:04d}'
        h.people.create_entity(dict(PartitionKey='u', RowKey=person, faceIds=json.dumps([face]),
                                   repEmbedding=json.dumps(vector), name=person))
        h.faces.create_entity(dict(PartitionKey='u', RowKey=face, filename=face+'.jpg', personId=person,
            alignmentMethod='5pt', embeddingVersion='v1', embedding=json.dumps(vector)))
    seed_started = time.monotonic()
    random = np.random.default_rng(args.seed)
    for i in range(args.faces):
        vector = random.normal(0, .005, 512)
        vector[i % args.identities] += 1
        # Six decimal places keep realistic distinct vectors without encoding
        # excessive synthetic precision into journal/property byte limits.
        embedding = json.dumps(np.round(vector, 6).tolist(), separators=(',', ':'))
        h.faces.create_entity(dict(PartitionKey='u', RowKey=f'incoming-{i:06d}', filename=f'{i:06d}.jpg',
            alignmentMethod='5pt', embeddingVersion='v1', personId='', embedding=embedding))
    state.update(seed_seconds=time.monotonic()-seed_started,
                 seeded_rss_bytes=psutil.Process().memory_info().rss)
    dirty, admission = BenchmarkTable(), BenchmarkTable()
    calls, depth, call_lock = Counter(), threading.local(), threading.Lock()
    for label, table in [('faces',h.faces),('people',h.people),('members',h.members),
                         ('embeddings',h.embeddings),('coverage',dirty),('admission',admission),('jobs',store.table)]:
        for name in ['get_entity','create_entity','update_entity','upsert_entity','delete_entity',
                     'query_entities','submit_transaction']:
            original = getattr(table, name, None)
            if original is None:
                continue
            def counted(*a, _fn=original, _key=label+'.'+name, **kw):
                nesting = getattr(depth,'value',0)
                if not nesting:
                    with call_lock:
                        calls[_key] += 1
                depth.value = nesting+1
                try:
                    return _fn(*a, **kw)
                finally:
                    depth.value = nesting
            setattr(table,name,counted)
    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory(prefix='cluster-scale-') as root:
        patch.setenv('WAVE_CLUSTER_COVERAGE_BATCH_ENABLED','true')
        patch.setenv('WAVE_CLUSTER_PERSON_READ_BATCH_ENABLED','true')
        patch.setattr(su,'DURABLE_INDEX_UPDATES',True)
        patch.setattr(su,'_CTX',{'search_index_dirty_table_client':dirty,'wave_owned':lambda _:True})
        patch.setattr(app,'jobs_table_client',admission)
        patch.setattr(automatic,'schedule',lambda *a: (_ for _ in ()).throw(AssertionError('Independent scheduling')))
        h.assigner.face_table = automatic.SourceTable(h.faces,'face')
        h.assigner.person_table = automatic.SourceTable(h.people,'person')
        Path(root,'work').mkdir()
        h.assigner.config = replace(h.assigner.config,work_dir=root+'/work',checkpoint_dir=root+'/mount')
        state['config'] = asdict(h.assigner.config)
        # Paths are temporary and removed after measurement, not deployable settings.
        state['config']['work_dir'] = '<local-temp>/work'
        state['config']['checkpoint_dir'] = '<local-temp>/mount'
        last_position = [0]
        real_build, real_rebuild = ci.build_face_index, LiveFaceIndex.rebuild
        real_save, real_restore = resume.save, resume.restore
        real_require = checkpoints._require
        def require(condition, *arguments):
            message=arguments[0] if arguments else 'Checkpoint validation failed'
            if not condition:
                caller=inspect.currentframe().f_back
                record=dict(function=caller.f_code.co_name,line=caller.f_lineno,message=message)
                index=caller.f_locals.get('index')
                if index is not None:
                    record.update(index_type=type(index).__name__,
                        use_precomputed_table=getattr(index,'use_precomputed_table',None))
                    if hasattr(index,'pq'):
                        record.update(nlist=index.nlist,pq_m=index.pq.M,pq_bits=index.pq.nbits,
                            dimension=index.d,quantizer_dimension=index.quantizer.d,
                            quantizer_count=index.quantizer.ntotal,quantizer_metric=index.quantizer.metric_type,
                            quantizer_trained=bool(index.quantizer.is_trained),
                            expected=caller.f_locals.get('group'))
                state.setdefault('checkpoint_rejections',[]).append(record)
                save()
            return real_require(condition,*arguments)
        patch.setattr(checkpoints,'_require',require)
        def build(*a, **kw):
            started = time.monotonic()
            emit('index_build_start',vectors=kw.get('vector_count'))
            result = real_build(*a,**kw)
            record = dict(seconds=time.monotonic()-started, vectors=result.vector_count,
                          index_type=result.index_type, dimension=result.dimension,
                          nlist=int(getattr(result.index,'nlist',0)))
            state['builds'].append(record); emit('index_build_done',**record); save()
            return result
        def rebuild(runtime):
            started = time.monotonic()
            emit('rebuild_start',position=last_position[0])
            real_rebuild(runtime)
            record = dict(seconds=time.monotonic()-started, page_progress=last_position[0],
                          base_vectors=sum(int(x.ntotal) for x in runtime._base.values()))
            record['kind']='compaction' if record['base_vectors'] else 'empty_initialization'
            state['rebuilds'].append(record); save()
            emit('rebuild_done',**record)
        def snapshot(journal, workspace, runtime, header, position, chain, local):
            started = time.monotonic()
            real_save(journal,workspace,runtime,header,position,chain,local)
            record = dict(position=position, seconds=time.monotonic()-started,
                workspace_bytes=(journal.directory/'workspace.snapshot').stat().st_size,
                native_checkpoint_tree_bytes=tree_bytes(journal.directory/'resume-runtime'),
                journal_bytes=(journal.directory/'journal.jsonl').stat().st_size)
            state['saves'].append(record); save(); emit('resume_saved',**record)
        def restore(*a, **kw):
            started = time.monotonic(); result = real_restore(*a,**kw)
            record = dict(seconds=time.monotonic()-started, hit=result is not None,
                          position=result[2] if result is not None else 0)
            state['restores'].append(record); save(); emit('resume_restored',**record)
            if args.fail_on_restore_miss and result is None and state['saves']:
                raise RuntimeError('Qualification stopped: an unmodified published checkpoint failed to restore')
            return result
        real_put = FileAssignmentJournal.put
        def journal_put(journal, key, payload):
            try:
                return real_put(journal,key,payload)
            except ValueError as error:
                if 'byte budget' in str(error):
                    ids=payload.get('person',{}).get('faceIds','[]')
                    state['journal_limit_failure']=dict(key=key,
                        payload_utf16_bytes=len(wave.encoded(payload).encode('utf-16-le')),
                        person_face_ids_utf16_bytes=len(ids.encode('utf-16-le')),
                        person_members=len(json.loads(ids)), journal_payload_limit_bytes=60000,
                        membership_limit_bytes=60*1024)
                    save()
                raise
        patch.setattr(FileAssignmentJournal,'put',journal_put)
        patch.setattr(ci,'build_face_index',build)
        patch.setattr(LiveFaceIndex,'rebuild',rebuild)
        patch.setattr(resume,'save',snapshot)
        patch.setattr(resume,'restore',restore)
        resources = Resources(root); resources.thread.start()
        phases = diagnostics.WaveMetrics('u',frozen['waveId'])
        token = diagnostics._WAVE.set(phases)
        state['status']='running'; save()
        started, cpu_started = time.monotonic(),time.process_time()
        failure = None
        with metrics.accounting() as counts:
            try:
                for attempt in range(1000):
                    owner = leases.claim('u',frozen['waveId'],'cluster',frozen['frozenRevision'])
                    if owner is None:
                        raise RuntimeError('Benchmark execution claim unavailable')
                    attempt_start = time.monotonic()
                    outcome='error'
                    def progress(position):
                        last_position[0]=position
                        leases.checkpoint(owner,{'facesApplied':position})
                        if position % 2048 == 0 or position == args.faces:
                            state['progress']=position; save()
                            emit('progress',faces=position,total=args.faces,attempt=attempt+1,
                                 elapsed=time.monotonic()-started,rss_bytes=psutil.Process().memory_info().rss)
                    def should_yield():
                        return (attempt==0 and args.first_cut>0 and last_position[0]>=args.first_cut
                                or time.monotonic()-attempt_start>=args.slice_seconds)
                    try:
                        with leases.keepalive(owner,local_checks=True) as alive:
                            class Check:
                                def __call__(self): alive()
                                def boundary(self): alive(); leases.check(owner)
                            check = Check()
                            with execution_scope(leases,owner,store,check):
                                receipt=wave.run_frozen_assignment(store,frozen,h.assigner,
                                    lambda: (dict(h.faces.rows['u',f'incoming-{i:06d}']) for i in range(args.faces)),
                                    check,progress=progress,local_journal=True,should_yield=should_yield)
                        outcome='complete'
                    except wave.AssignmentYield:
                        outcome='yield'
                    finally:
                        leases.release(owner)
                        state['attempts'].append(dict(attempt=attempt+1,outcome=outcome,
                            seconds=time.monotonic()-attempt_start,progress=last_position[0]))
                        save()
                    if outcome=='complete':
                        state.update(status='complete',receipt=receipt)
                        break
                else:
                    raise RuntimeError('Attempt count bound exceeded')
            except Exception as error:
                failure=error
                state.update(status='qualification_blocked' if 'Qualification stopped:' in str(error) else 'failed',error_type=type(error).__name__,error=str(error),
                             traceback=traceback.format_exc())
                emit('scenario_failed',error=repr(error),page_progress=last_position[0])
            finally:
                state.update(elapsed_seconds=time.monotonic()-started,
                    process_cpu_seconds=time.process_time()-cpu_started,
                    counters=counts.snapshot(),calls=dict(calls),phases=phases.phases,
                    resources=resources.finish())
                diagnostics._WAVE.reset(token)
        applied=0
        try:
            applied=0
            owners=hashlib.sha256()
            for i in range(args.faces):
                row=h.faces.rows['u',f'incoming-{i:06d}']
                person=row.get('personId')
                if person:
                    if person!=f'person-{i % args.identities:04d}':
                        raise AssertionError(f'Unexpected identity at face {i}: {person}')
                    applied+=1
                    owners.update(f'{i}:{person}\n'.encode())
            state.update(applied_faces=applied,owner_digest=owners.hexdigest(),
                         wrong_identity_count=0,total_counted_calls=sum(calls.values()))
            if failure is None:
                assert applied==args.faces and len(h.members.rows)==args.faces
                assert len(h.people.rows)==args.identities
                for identity in range(args.identities):
                    person=f'person-{identity:04d}'
                    actual=json.loads(h.people.rows['u',person]['faceIds'])
                    expected={f'incoming-{i:06d}' for i in range(identity,args.faces,args.identities)}
                    expected.add(f'anchor-{identity:04d}')
                    assert set(actual)==expected and len(actual)==len(expected)
                state['output_validation']='All expected owners, exact membership lists and member-row count verified'
                state['faces_per_second']=args.faces/state['elapsed_seconds']
            else:
                state['output_validation']='Partial source ownership verified; scenario did not complete'
        except Exception as error:
            state.update(status='validation_failed',error_type=type(error).__name__,
                         error=str(error),traceback=traceback.format_exc(),applied_faces=applied)
        control=automatic.configured_store().control('u','people_embedding')
        state['coverage_active_after_run']=control.get('active') if control else None
        state['finished_at']=datetime.now(timezone.utc).isoformat()
        save()
        emit('scenario_done',status=state['status'],faces=args.faces,applied_faces=applied,
             seconds=state['elapsed_seconds'],rebuilds=len(state['rebuilds']),attempts=len(state['attempts']),
             peak_rss=state['resources']['process_lifetime_peak_rss_bytes'])


if __name__=='__main__':
    main()
