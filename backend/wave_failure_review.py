"""Bounded, read-only repair manifest; never retries or resets source state."""
import json
import re

from azure.core.exceptions import ResourceNotFoundError
from processing_failures import source_details
from upload_waves import MAX_PAGE
from wave_settlement import verified


def page(store, metadata, wave_id, *, after=''):
    wave = store.status(wave_id)  # Validate library ownership/wave existence.
    prefix = f'e!{wave_id}!m!'
    if not isinstance(after, str) or (after and (not after.startswith(prefix)
            or re.fullmatch(r'[0-9a-f]{64}', after[len(prefix):]) is None)):
        raise ValueError('Invalid failed-member review cursor')
    members = store._page(prefix, after)
    failures = []
    for member in members:
        if member.get('outcome') != 'failed_exhausted':
            continue
        entry = dict(photoId=member['photoId'], processingRevision=member['processingRevision'],
                     disposition='invalid_proof', recommendation='investigate_proof')
        if not verified(store, member):
            failures.append(entry)
            continue
        proof = json.loads(store._required('settlement!' + member['evidenceId'])['payload'])
        entry.update(reason=proof.get('reason'), historicalFailure=proof.get('failure'))
        try:
            row = metadata.get_entity(store.library, member['filename'])
        except ResourceNotFoundError:
            entry.update(disposition='missing', recommendation='verify_deletion_or_source_identity')
        else:
            if (row.get('anonymousImageId'), row.get('processingRevision')) != (
                    member['photoId'], member['processingRevision']):
                entry.update(disposition='different_generation', recommendation='review_current_generation')
            elif row.get('processing_state') == 'deleted':
                entry.update(disposition='deleted', recommendation='respect_deletion')
            else:
                entry.update(disposition='matching_generation', recommendation='inspect_before_retry',
                             currentFailure=source_details(row))
        if wave['state'] == 'cancelled':
            entry['recommendation'] = 'respect_cancellation'
        elif entry.get('reason') == 'operator_release_replaced_source_without_admission':
            entry['recommendation'] = 'review_operator_release'
        failures.append(entry)
    return dict(waveId=wave_id, state=wave['state'], failures=failures,
        nextAfter=members[-1]['RowKey'] if len(members) == MAX_PAGE else '',
        scannedMembers=len(members), readOnly=True)
