"""Compare actual Table SDK read shapes in disposable synthetic partitions."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    from azure.data.tables import TableServiceClient
    from azure.core.exceptions import ResourceNotFoundError
    from table_reads import read_entities
    service=TableServiceClient.from_connection_string(os.environ['BENCHMARK_STORAGE_CONNECTION'])
    table=service.create_table_if_not_exists('benchreceiptreads')
    report=dict(scope='Synthetic receipt partitions; actual Table SDK; no production data',
        cpu_max=Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
        memory_max_bytes=int(Path('/sys/fs/cgroup/memory.max').read_text()),reports=[])
    assert report['cpu_max']=='200000 100000' and report['memory_max_bytes']==4*1024**3
    lock=threading.Lock();wire=[0]
    def response(_):
        with lock:wire[0]+=1
    class Client:
        def query_entities(self,*a,**kw):return table.query_entities(*a,raw_response_hook=response,**kw)
        def get_entity(self,*a,**kw):return table.get_entity(*a,raw_response_hook=response,**kw)
    client=Client()
    payload=json.dumps(dict(embedding=[(i-256)/257 for i in range(512)],embeddingVersion='synthetic-v1'))
    def key(i):return 'face!'+hashlib.sha256(str(i).encode()).hexdigest()
    for size in (1000,10000,50000):
        partition='benchmark-'+str(size)
        def seed(offset):
            table.submit_transaction([('create',dict(PartitionKey=partition,RowKey=key(i),payload=payload))
                for i in range(offset,min(size,offset+100))])
        start=time.perf_counter()
        with ThreadPoolExecutor(4) as pool:list(pool.map(seed,range(0,size,100)))
        print('SEEDED '+str(size)+' '+str(time.perf_counter()-start),flush=True)
        keys=[key(i) for i in range(size//2,size//2+20)]
        # Include absence controls as well as fully populated batches.
        for missing in (False,True):
            wanted=keys if not missing else keys[:16]+[key(size+i) for i in range(4)]
            def point(k):
                try:return k,client.get_entity(partition,k)
                except ResourceNotFoundError:return k,None
            for r in range(3):
                for mode in (['query_or','points4'] if r%2==0 else ['points4','query_or']):
                    wire[0]=0;cpu,start=time.process_time(),time.perf_counter()
                    if mode=='query_or':
                        rows=read_entities(client,partition,wanted,workers=4,group_namespaces=True)
                    else:
                        with ThreadPoolExecutor(4) as pool:rows=dict(pool.map(point,wanted))
                    wall=time.perf_counter()-start
                    assert sum(v is None for v in rows.values())==(4 if missing else 0)
                    assert all(v is None or v['payload']==payload for v in rows.values())
                    row=dict(partition_rows=size,missing=missing,round=r,mode=mode,keys=len(wanted),
                        wall_seconds=wall,process_cpu_seconds=time.process_time()-cpu,
                        http_responses=wire[0],returned_payload_bytes=sum(len(v['payload']) for v in rows.values() if v))
                    report['reports'].append(row);args.output.write_text(json.dumps(report,indent=2)+'\n')
                    print(json.dumps(row),flush=True)
    report['complete']=True;args.output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
