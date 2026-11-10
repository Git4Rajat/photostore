"""Admission of metadata rows into serving catalogs."""

# Include these in projected reads: omitting tracking fields would make a
# reservation indistinguishable from a legacy photo without upload tracking.
_FINALIZATION_FIELDS = (
    'uploadDate',
    'anonymousImageId', 'fileHash', 'perceptualHash', 'mimeType', 'verification_status',
)
# A Live Photo's motion clip, once attached to its photo (see live_photos.py), is served with the photo, not
# as a gallery item of its own.
LIVE_PHOTO_OF = 'livePhotoOf'
CATALOG_ADMISSION_FIELDS = ('pendingAnonymousBlob', 'upload_started_at', LIVE_PHOTO_OF, *_FINALIZATION_FIELDS)


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


def is_live_photo_clip(row):
    """An attached Live Photo clip: the gallery and search leave it out, media access still serves it (the
    viewer plays it by its own filename)."""
    return bool(row.get(LIVE_PHOTO_OF))
