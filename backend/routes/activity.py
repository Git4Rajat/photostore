"""What the bell shows that only the server knows: shared albums someone opened, and invites about to end.

One cheap Core read for the browser to make on start-up and when the tab comes back: a filtered
query of the caller's own album partition and the owner's pending-invite index. The browser turns
each item into a notification once (see frontend/src/services/activityNotifications.ts).
"""
import time

from flask import Blueprint

import app

activity_bp = Blueprint('activity', __name__)

# Opens older than this are not news any more (matches the bell's own retention).
OPENS_MAX_AGE_SECONDS = 30 * 24 * 3600
# An invite becomes worth a reminder in its last day.
INVITE_ENDING_SECONDS = 24 * 3600


def album_opens(library_id: str, now: float):
    if not app._albums_table_available():
        return []
    rows = app.albums_table_client.query_entities(
        f"PartitionKey eq '{app._escape_odata(library_id)}' and lastOpenedAt gt {now - OPENS_MAX_AGE_SECONDS}",
        select=['RowKey', 'name', 'lastOpenedAt', 'openCount', 'deleted'])
    return [
        {'albumId': str(row['RowKey']), 'name': str(row.get('name') or ''),
         'openedAt': float(row['lastOpenedAt']), 'opens': int(row.get('openCount') or 0)}
        for row in rows if not app._coerce_bool(row.get('deleted'))
    ]


def invites_ending(account_id: str, library_id: str, now: float):
    if app.library_store is None or not app.library_store.is_owner(account_id, library_id):
        return []
    ending = []
    for invite in app.library_store.pending_invites(library_id):
        expires_at = int(invite.get('expiresAt', 0) or 0)
        if 0 < expires_at - now <= INVITE_ENDING_SECONDS:
            ending.append({
                'inviteId': str(invite.get('RowKey') or ''),
                'email': app.email_utils.masked_recipient(invite.get('emailNorm')),
                'expiresAt': expires_at,
            })
    return ending


@activity_bp.route('/api/activity', methods=['GET'])
def activity():
    account_id, library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    now = time.time()
    body = {'albumOpens': [], 'invitesEnding': []}
    # Each half degrades on its own: a storage hiccup costs one kind of entry, not both.
    try:
        body['albumOpens'] = album_opens(library_id, now)
    except Exception:
        app.app.logger.warning('Activity: album opens unavailable', exc_info=True)
    try:
        body['invitesEnding'] = invites_ending(account_id, library_id, now)
    except Exception:
        app.app.logger.warning('Activity: pending invites unavailable', exc_info=True)
    response = app.jsonify(body)
    response.headers['Cache-Control'] = 'no-store'
    return response
