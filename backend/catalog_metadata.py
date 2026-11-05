"""Admission of metadata rows into serving catalogs."""

# Include these in projected reads: omitting tracking fields would make a
# reservation indistinguishable from a legacy photo without upload tracking.
_FINALIZATION_FIELDS = (
    'uploadDate',
    'anonymousImageId', 'fileHash', 'perceptualHash', 'mimeType', 'verification_status',
)
CATALOG_ADMISSION_FIELDS = ('pendingAnonymousBlob', 'upload_started_at', *_FINALIZATION_FIELDS)


def is_upload_reservation_only(row):
    """Exclude unfinished transfers, without requiring processing completion.

    Preserve legacy photos without tracking, and previously finalized photos
    whose row is reused for another upload. Match the durable finalization
    signals used by failed-upload cleanup; early thumbnail claims and client
    metadata alone do not establish finalization.
    """
    # upload_started_at alone is also a supported legacy upload-date fallback.
    # Require an actual reservation before excluding a row.
    tracking = row.get('pendingAnonymousBlob')
    finalized = any(row.get(field) for field in _FINALIZATION_FIELDS)
    return bool(tracking) and not finalized
