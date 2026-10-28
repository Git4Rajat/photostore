"""Real Azure Blob SDK validation in a disposable private container.

All names are generated beneath a newly created container. No production blobs,
queues, configuration or application tables are read or mutated. Always attempts
container deletion and verifies absence before recording cleanup success.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import platform
import statistics
import sys
import threading
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from azure.core import MatchConditions
from azure.core.exceptions import (HttpResponseError, ResourceExistsError,
    ResourceNotFoundError, ResourceModifiedError, ServiceResponseError)
from azure.core.pipeline.transport import RequestsTransport
from azure.identity import AzureCliCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from processing_artifacts import validate_derivative_bytes
from benchmark_vision_artifacts import corpus

PHASE=ContextVar('validation_phase',default='setup')


class MeterTransport(RequestsTransport):
    def __init__(self):
        super().__init__();self.records=[];self.lock=threading.Lock();self.fault=None
    def send(self,request,**kwargs):
        blob_put=request.method=='PUT' and request.headers.get('x-ms-blob-type')=='BlockBlob'
        fault=self.fault if blob_put else None
        if fault:self.fault=None
        if fault=='before_commit':raise ServiceResponseError('Injected before transport send')
        if fault=='bad_md5':request.headers['Content-MD5']=base64.b64encode(b'\0'*16).decode()
        started=time.perf_counter()
        response=super().send(request,**kwargs)
        with self.lock:self.records.append(dict(phase=PHASE.get(),method=request.method,
            status=response.status_code,milliseconds=(time.perf_counter()-started)*1000,
            upload_content_md5_present=bool(request.headers.get('Content-MD5')) if blob_put else None))
        if fault=='lost_success' and 200<=response.status_code<300:
            response.close();raise ServiceResponseError('Injected lost successful service response')
        return response


def read(blob,kind,etag=None):
    options=dict(etag=etag,match_condition=MatchConditions.IfNotModified) if etag else {}
    cap={'thumbnail':512000,'preview':1500000}[kind]
    download=blob.download_blob(offset=0,length=cap+1,max_concurrency=1,**options)
    data=download.readall();props=download.properties
    validate_derivative_bytes(data,kind)
    assert len(data)==props.size and props.content_settings.content_type=='image/jpeg'
    return data,props


def publish(blob,data,kind,*,response_receipt):
    width,height=validate_derivative_bytes(data,kind)
    sha=hashlib.sha256(data).hexdigest();response=None
    try:response=blob.upload_blob(data,overwrite=False,validate_content=response_receipt,
        content_settings=ContentSettings(content_type='image/jpeg'),max_concurrency=1)
    except ResourceExistsError:pass
    # Transport errors intentionally propagate; a failed attempt returns no receipt.
    if not response_receipt or not response or not response.get('etag'):
        actual,props=read(blob,kind)
        if actual!=data:raise ValueError('Existing artifact content mismatch')
        etag=str(props.etag)
    else:etag=str(response['etag'])
    return dict(kind=kind,sha256=sha,size=len(data),width=width,height=height,etag=etag)


def verify(blob,receipt):
    token=PHASE.set('independent_verification')
    try:
        data,props=read(blob,receipt['kind'],receipt['etag'])
        assert hashlib.sha256(data).hexdigest()==receipt['sha256']
        assert len(data)==receipt['size']
        assert validate_derivative_bytes(data,receipt['kind'])==(receipt['width'],receipt['height'])
        assert str(props.etag)==receipt['etag']
    finally:PHASE.reset(token)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',required=True)
    parser.add_argument('--corpus',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--images',type=int,default=13)
    parser.add_argument('--precreated-container',help='Disposable test container prepared by the permission controller')
    args=parser.parse_args()
    pairs=corpus(args.corpus,args.images)
    name=args.precreated_container or 'vision-receipt-validation-'+uuid.uuid4().hex[:16]
    import re
    if not re.fullmatch(r'vision-receipt-validation-[0-9a-f]{16}',name):
        raise ValueError('Only generated disposable validation containers are allowed')
    transport=MeterTransport();credential=AzureCliCredential()
    service=BlobServiceClient(f'https://{args.account}.blob.core.windows.net',credential=credential,
        transport=transport,retry_total=0,connection_timeout=10,read_timeout=30)
    container=service.get_container_client(name)
    report=dict(started_utc=datetime.now(timezone.utc).isoformat(),container=name,
        platform=platform.platform(),sdk_versions={p:importlib.metadata.version(p) for p in
        ('azure-storage-blob','azure-core','azure-identity')},retry_total=0,images=len(pairs),
        reports=[],cases=[],cleanup_verified=False)
    created=False
    def save():args.output.write_text(json.dumps(report,indent=2)+'\n')
    try:
        if args.precreated_container:container.get_container_properties()
        else:container.create_container()
        created=True;save()
        props=container.get_container_properties()
        assert not props.get('public_access')
        report['private_container_verified']=True
        for i,(thumb,preview) in enumerate(pairs):
            for kind,data in [('thumbnail',thumb),('preview',preview)]:
                container.get_blob_client(f'input/{i}/{kind}').upload_blob(data,overwrite=False,
                    content_settings=ContentSettings(content_type='image/jpeg'))
        # Warm auth/TLS separately from timed cases.
        read(container.get_blob_client('input/0/preview'),'preview')
        for round_number in range(args.rounds):
            modes=['current','reuse_preview','upload_response']
            for mode in modes if round_number%2==0 else reversed(modes):
                phase=f'benchmark/{round_number}/{mode}';before=len(transport.records)
                def image(index):
                    token=PHASE.set(phase)
                    try:
                        preview,_=read(container.get_blob_client(f'input/{index}/preview'),'preview')
                        receipts=[]
                        for kind in ('thumbnail','preview'):
                            if kind=='preview' and mode!='current':data=preview
                            else:data,_=read(container.get_blob_client(f'input/{index}/{kind}'),kind)
                            blob=container.get_blob_client(f'output/{round_number}/{mode}/{index}/{kind}')
                            receipt=publish(blob,data,kind,response_receipt=mode=='upload_response')
                            receipts.append((blob,receipt))
                        return receipts
                    finally:PHASE.reset(token)
                started,cpu=time.perf_counter(),time.process_time()
                with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(image,range(len(pairs))))
                elapsed,cpu_used=time.perf_counter()-started,time.process_time()-cpu
                calls=[r for r in transport.records[before:] if r['phase']==phase]
                for receipts in results:
                    for blob,receipt in receipts:verify(blob,receipt)
                report['reports'].append(dict(round=round_number,mode=mode,wall_seconds=elapsed,
                    cpu_seconds=cpu_used,images=len(pairs),gets=sum(r['method']=='GET' for r in calls),
                    puts=sum(r['method']=='PUT' for r in calls),independent_receipts_verified=2*len(pairs)))
                save();print(json.dumps(report['reports'][-1]),flush=True)
        data=pairs[0][1];kind='preview'
        def case(label,fn):
            token=PHASE.set('case/'+label)
            try:fn();report['cases'].append(dict(name=label,passed=True));save()
            finally:PHASE.reset(token)
        def existing():
            blob=container.get_blob_client('cases/existing')
            first=publish(blob,data,kind,response_receipt=True)
            second=publish(blob,data,kind,response_receipt=True)
            assert first==second;verify(blob,second)
        case('existing_identical_create_conflict',existing)
        def conflict():
            blob=container.get_blob_client('cases/wrong-content')
            out=io.BytesIO();Image.new('RGB',(80,80),'blue').save(out,'JPEG')
            blob.upload_blob(out.getvalue(),overwrite=False,content_settings=ContentSettings(content_type='image/jpeg'))
            try:publish(blob,data,kind,response_receipt=True)
            except ValueError:return
            raise AssertionError('Wrong content accepted')
        case('existing_wrong_content_rejected',conflict)
        for fault in ('before_commit','lost_success'):
            def failure(fault=fault):
                blob=container.get_blob_client('cases/'+fault)
                transport.fault=fault;receipt=None
                try:receipt=publish(blob,data,kind,response_receipt=True)
                except ServiceResponseError:pass
                else:raise AssertionError('Fault was not injected')
                assert receipt is None
                if fault=='before_commit':assert not blob.exists()
                else:assert blob.exists()
                receipt=publish(blob,data,kind,response_receipt=True);verify(blob,receipt)
            case(fault+'_no_receipt_and_safe_retry',failure)
        def checksum():
            blob=container.get_blob_client('cases/bad-checksum');transport.fault='bad_md5'
            try:publish(blob,data,kind,response_receipt=True)
            except HttpResponseError as exc:
                code=getattr(exc.error_code,'value',exc.error_code)
                assert exc.status_code==400 and str(code).lower()=='md5mismatch'
            else:raise AssertionError('Invalid transport checksum accepted')
            assert not blob.exists()
        case('azure_rejects_wrong_transport_md5',checksum)
        def mutation():
            blob=container.get_blob_client('cases/etag-change')
            receipt=publish(blob,data,kind,response_receipt=True)
            blob.upload_blob(data,overwrite=True,content_settings=ContentSettings(content_type='image/jpeg'))
            try:verify(blob,receipt)
            except ResourceModifiedError:return
            raise AssertionError('Stale ETag accepted')
        case('etag_pinned_verification_rejects_mutation',mutation)
        report['passed']=True
    except Exception as exc:
        report['passed']=False;report['error_type']=type(exc).__name__
        raise
    finally:
        try:
            if created:
                token=PHASE.set('cleanup')
                try:
                    container.delete_container()
                    try:container.get_container_properties()
                    except ResourceNotFoundError:report['cleanup_verified']=True
                    else:raise AssertionError('Container still exists')
                finally:PHASE.reset(token)
        finally:
            report['http_summary']={f'{method}:{status}':sum(r['method']==method and r['status']==status for r in transport.records)
                for method,status in sorted({(r['method'],r['status']) for r in transport.records})}
            report['validated_puts_with_md5']=sum(r['upload_content_md5_present'] is True for r in transport.records)
            report['finished_utc']=datetime.now(timezone.utc).isoformat();save();service.close();credential.close()
    print(json.dumps(dict(passed=report['passed'],cases=len(report['cases']),cleanup_verified=report['cleanup_verified'])),flush=True)


if __name__=='__main__':main()
