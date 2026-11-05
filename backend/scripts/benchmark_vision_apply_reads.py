"""Isolated follow-up to the deployed face write batching implementation.

Compare current reads, deferred lease checks, and batched embedding ETag reads.
Uses real production control flow with ETag-aware local doubles. Per-call
latencies are simulated; no Azure or production data mutations.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import statistics
import sys
import argparse

sys.path.insert(0,str(Path(__file__).resolve().parent))
from benchmark_face_persistence import execute as _execute
import face_write_batch as batches
import storage_utils as storage
from table_reads import read_entities
import pytest


def execute(*args, **kwargs):
    # Preserve the pre-change deployed baseline even after implementation lands.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv('IPWORK_FACE_APPLY_READS_BATCH_ENABLED','false')
        return _execute(*args, **kwargs)


@contextmanager
def candidate(mode, *, after_versions=None):
    """Prototype only; fresh source/filename checks still precede every write."""
    if mode=='current':
        yield
        return
    with pytest.MonkeyPatch.context() as mp:
        original_renew=storage._renew_face_filename_write
        original_flush=batches.flush
        original_enqueue=batches.enqueue
        def renew(*args,**kwargs):
            batch=batches._ACTIVE.get()
            if batch is not None and kwargs.get('artifact_checks_source') and not batch.get('benchmark_flushing'):
                return  # Only queue operations occur here; no mutable write.
            return original_renew(*args,**kwargs)
        def enqueue(table,entity,fence,previous,previous_known):
            batch=batches._ACTIVE.get()
            if mode=='batched_versions' and batch is not None and hasattr(table,'submit_transaction') and not previous_known:
                batch.setdefault('benchmark_pending',[]).append((table,dict(entity),fence))
                return True
            return original_enqueue(table,entity,fence,previous,previous_known)
        def flush():
            batch=batches._ACTIVE.get()
            if batch is None:return original_flush()
            grouped={}
            for table,entity,fence in batch.pop('benchmark_pending',[]):
                grouped.setdefault((id(table),entity['PartitionKey']),[]).append((table,entity,fence))
            for entries in grouped.values():
                table=entries[0][0];partition=entries[0][1]['PartitionKey']
                versions=read_entities(table,partition,[entry[1]['RowKey'] for entry in entries],workers=1)
                for table,entity,fence in entries:
                    original_enqueue(table,entity,fence,versions[entity['RowKey']],True)
            if after_versions:after_versions()
            batch['benchmark_flushing']=True
            try:return original_flush()
            finally:batch.pop('benchmark_flushing',None)
        mp.setattr(storage,'_renew_face_filename_write',renew)
        mp.setattr(batches,'enqueue',enqueue)
        mp.setattr(batches,'flush',flush)
        yield


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=5)
    args=parser.parse_args();reports=[];references={}
    for delay in (0,10):
        for seeded in (False,True):
            for count in (0,1,8,32):
                for round_number in range(args.rounds):
                    modes=['current','defer_guards','batched_versions']
                    for mode in modes if round_number%2==0 else reversed(modes):
                        with candidate(mode):row=execute(count,'current',delay,seed=seeded)
                        row.update(mode=mode,round=round_number)
                        key=(count,seeded)
                        references.setdefault(key,row['output_digest'])
                        assert references[key]==row['output_digest'],'Output/curation mismatch'
                        reports.append(row)
                print(f'Completed delay={delay}, seeded={seeded}, faces={count}',flush=True)
                args.output.write_text(json.dumps(dict(
                    scope='Local ETag-aware storage doubles; delays simulated; production code unchanged',
                    output_parity=True,reports=reports),indent=2)+'\n')


if __name__=='__main__':main()
