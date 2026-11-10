"""Unit tests for _humanize_job's handling of jobType='ipwork' (app.py).

Each photo processed in backend/both processing mode gets its own 'ipwork'
job row. Before this fix, jobType='ipwork' fell into _humanize_job's generic
fallback (kind='job', title='Background task finished'), which the frontend
poller treats as toastable -- a bulk backend-mode upload of N photos would
surface N separate "Background task finished" bell/toast entries. Giving
ipwork jobs their own kind lets the frontend exclude them from the bell/toast
the same way it already excludes 'preview' (see AppServicesProvider.tsx).
"""
from __future__ import annotations

import app


def test_ipwork_job_gets_its_own_kind_not_generic_job():
    result = app._humanize_job({'jobId': 'job-1', 'jobType': 'ipwork', 'status': 'done'})

    assert result['kind'] == 'ipwork'
    assert result['title'] != 'Background task finished'


def test_ipwork_job_failed_status():
    result = app._humanize_job({'jobId': 'job-2', 'jobType': 'ipwork', 'status': 'failed', 'error': 'boom'})

    assert result['kind'] == 'ipwork'
    assert result['title'] == 'Photo processing failed'
    assert result['error'] == 'boom'


def test_unknown_job_type_still_falls_back_to_generic_job():
    result = app._humanize_job({'jobId': 'job-3', 'jobType': 'some_future_type', 'status': 'done'})

    assert result['kind'] == 'job'
    assert result['title'] == 'Background task finished'


def test_stalled_upload_wave_tells_user_to_retry_from_tools():
    result = app._humanize_job({'jobId': 'wave-stalled-w1-vision', 'jobType': 'upload_wave', 'status': 'failed',
                                'error': 'Wave dispatch attempts exhausted'})

    assert result['kind'] == 'upload_wave'
    assert result['title'] == 'Upload processing stalled'
    assert 'Tools' in result['message'] and 'retry' in result['message']
