"""Compare old reachability SQL with complete current checkpoint validation."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import clustering_checkpoint as checkpoint
from clustering_runtime import LiveFaceIndex


OLD_SQL='''SELECT COUNT(*) FROM faces f WHERE
    (dirty=0 AND NOT EXISTS (SELECT 1 FROM base_slots s WHERE
     s.face_id=f.face_id AND s.revision=f.revision AND s.tier=f.tier AND
     s.version=f.version AND s.dimension=f.dimension)) OR
    (dirty=1 AND NOT EXISTS (SELECT 1 FROM delta_slots s WHERE
     s.face_row=f.id AND s.revision=f.revision AND s.tier=f.tier AND
     s.version=f.version AND s.dimension=f.dimension))'''


def measure(db,operation):
    calls=0
    def progress():
        nonlocal calls
        calls+=1
        return 0
    db.set_progress_handler(progress,1000)
    started=time.perf_counter()
    try:
        operation()
    finally:
        db.set_progress_handler(None,0)
    return dict(seconds=time.perf_counter()-started,sqlite_vm_steps_floor=calls*1000)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    results=[]
    for size in (1000,2000,4000):
        with tempfile.TemporaryDirectory() as directory:
            runtime=LiveFaceIndex(directory)
            try:
                runtime.build(dict(faceId=f'f-{i}',personId='alice',embedding=[1,0],
                    tier='5pt',embeddingVersion='v1') for i in range(size))
                for i in range(32):
                    runtime.upsert(f'f-{i}','bob',[0,1],'5pt','v1')
                manifest=dict(counts=checkpoint._counts(runtime._db),groups=[
                    checkpoint._description(runtime,key,index,kind,'unused')
                    for kind,indexes in [('base',runtime._base),('delta',runtime._delta)]
                    for key,index in indexes.items()])
                def old():
                    assert runtime._db.execute(OLD_SQL).fetchone()==(0,)
                results.append(dict(faces=size,
                    old_reachability_only=measure(runtime._db,old),
                    new_complete_database_validation=measure(runtime._db,
                        lambda:checkpoint._validate_database(runtime._db,manifest))))
            finally:
                runtime.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(dict(results=results,
        scope='Local single samples; old reachability query ONLY versus all current database checks, including reachability. SQLite VM count excludes Python/numpy operations.'),indent=2)+'\n')


if __name__=='__main__':
    main()
