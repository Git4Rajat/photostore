"""Queue tuning preserves the raw ARM scaler identity and sibling rules."""
from copy import deepcopy
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy'))
from queue_scale_patch import build_patch


def resource():
    return dict(identity=dict(type='SystemAssigned'), properties=dict(template=dict(
        containers=[dict(name='vision',image='immutable-image')], revisionSuffix='old',
        scale=dict(minReplicas=0,maxReplicas=2,rules=[dict(name='queue',custom=dict(type='azure-queue',identity='system',
        metadata=dict(accountName='store',queueName='work',queueLength='5',queueLengthStrategy='all'))),
        dict(name='deferred',custom=dict(type='azure-queue',identity='system',
        metadata=dict(accountName='store',queueName='deferred',queueLength='1',queueLengthStrategy='visibleonly')))])) ))


def test_patch_changes_only_requested_queue_target_and_removes_old_suffix():
    before=resource();patch=build_patch(before,'queue',1)
    expected=deepcopy(before['properties']['template']);expected.pop('revisionSuffix')
    expected['scale']['rules'][0]['custom']['metadata']['queueLength']='1'
    assert patch == dict(properties=dict(template=expected))
    assert before == resource()


@pytest.mark.parametrize('damage', ['identity','metadata','assignment','floor'])
def test_incomplete_cli_projection_or_invalid_identity_is_rejected(damage):
    before=resource()
    custom=before['properties']['template']['scale']['rules'][0]['custom']
    if damage=='identity':custom.pop('identity')
    elif damage=='metadata':custom['metadata']={}
    elif damage=='assignment':before['identity']={}
    else:before['properties']['template']['scale']['minReplicas']=1
    with pytest.raises(ValueError):build_patch(before,'queue',1)
