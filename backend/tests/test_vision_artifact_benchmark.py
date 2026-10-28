"""Correctness checks for the isolated artifact-promotion prototype."""
import importlib.util
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from azure.core.exceptions import ServiceResponseError
from processing_artifacts import published_name, validate_receipt
from processing_completion import StaleProcessingResult
from processing_fence import ResultFence, result_fence, immutable_artifact_checks

spec=importlib.util.spec_from_file_location('artifact_benchmark',
    Path(__file__).resolve().parents[1]/'scripts'/'benchmark_vision_artifacts.py')
bench=importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name]=bench
spec.loader.exec_module(bench)


@pytest.fixture
def experiment(tmp_path):
    out=io.BytesIO();Image.new('RGB',(100,80),'red').save(out,'JPEG');data=out.getvalue()
    storage=bench.LocalStorage(tmp_path)
    row=dict(PartitionKey='lib',RowKey='x.jpg',anonymousImageId='photo',processingRevision='rev',
        fileHash='hash',processingFence='attempt',processing_lease_owner='worker',
        processing_lease_expires_at='2099-01-01T00:00:00+00:00')
    source=bench.Source(dict(row));fence=ResultFence(source,'lib','x.jpg','rev','attempt','worker','photo')
    storage._get_blob_client('thumbs','photo').path.write_bytes(data)
    return storage,row,source,fence,bench.Preview(bench.identity(row),data)


def promote(experiment):
    storage,row,_,fence,preview=experiment
    with result_fence(fence),immutable_artifact_checks(fence):
        bench.experimental_promote(storage,row,preview,upload_receipt=True)
    return [validate_receipt(row,k) for k in ('thumbnail','preview')]


def test_success_uses_one_source_get_and_two_create_only_puts(experiment):
    receipts=promote(experiment);storage=experiment[0]
    assert storage.counts['gets']==1 and storage.counts['puts']==2
    assert all(r['etag'] and r['sha256'] for r in receipts)


def test_retry_reads_existing_blobs_without_overwrite(experiment):
    first=promote(experiment);before=dict(experiment[0].counts)
    assert promote(experiment)==first
    assert experiment[0].counts['gets']-before['gets']==3


def test_existing_blob_with_wrong_content_is_rejected(experiment):
    storage,row,_,_,preview=experiment
    sha=bench.hashlib.sha256(preview.data).hexdigest()
    name=published_name('lib','photo','rev','thumbnail',sha)
    out=io.BytesIO();Image.new('RGB',(100,80),'blue').save(out,'JPEG')
    storage._get_blob_client('thumbs',name).path.write_bytes(out.getvalue())
    with pytest.raises(ValueError,match='mismatch'):promote(experiment)
    assert 'thumbnailArtifact' not in row


@pytest.mark.parametrize('fault',['interrupted','uncertain'])
def test_upload_failure_does_not_publish_receipt_and_retry_verifies(experiment,fault):
    storage,row,*_=experiment;storage.fault=fault
    with pytest.raises(ServiceResponseError):promote(experiment)
    assert 'thumbnailArtifact' not in row
    assert len(promote(experiment))==2
    # Lost success response leaves an object: retry must GET it.
    assert storage.counts['gets']==(3 if fault=='uncertain' else 2)


def test_missing_success_etag_falls_back_to_readback(experiment):
    experiment[0].fault='missing_etag'
    assert len(promote(experiment))==2
    assert experiment[0].counts['gets']==3


@pytest.mark.parametrize('field',['processingRevision','anonymousImageId','fileHash'])
def test_stale_preview_binding_never_uploads(experiment,field):
    storage,row,*_=experiment;row[field]='changed'
    with pytest.raises(StaleProcessingResult):promote(experiment)
    assert storage.counts['puts']==0


def test_source_change_after_upload_rejects_final_publication(experiment):
    storage,_,source,*_=experiment
    storage.after_upload=lambda:source.row.update(processingRevision='successor')
    with pytest.raises(StaleProcessingResult):promote(experiment)
    # Local candidate fields can exist; caller must not CAS/publish on failure.


def test_invalid_preview_cannot_complete(experiment):
    storage,row,source,fence,_=experiment
    bad=bench.Preview(bench.identity(row),b'not jpeg')
    with pytest.raises((ValueError,OSError)),result_fence(fence),immutable_artifact_checks(fence):
        bench.experimental_promote(storage,row,bad,upload_receipt=True)
    assert 'previewArtifact' not in row
