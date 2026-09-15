"""Transactional email via Azure Communication Services (ACS).

Used to send password-reset links for the single-owner password auth mode.
Provisioned by the deployment template with an Azure-managed sender domain, so
the owner configures no DNS.

Configuration (env):
  ACS_CONNECTION_STRING   Full ACS connection string (simplest), OR
  ACS_ENDPOINT            ACS resource endpoint (used with managed identity)
  ACS_SENDER_ADDRESS      From address, e.g. donotreply@<guid>.azurecomm.net

If neither a connection string nor an endpoint is set, email is considered
"not configured" and callers should fall back to the Azure-portal reset path.
"""

from __future__ import annotations

import html
import logging
import os
from email.utils import parseaddr
from typing import Optional


logger = logging.getLogger(__name__)


def _sender_address() -> str:
    return os.getenv('ACS_SENDER_ADDRESS', '').strip()


def is_configured() -> bool:
    has_transport = bool(
        os.getenv('ACS_CONNECTION_STRING', '').strip()
        or os.getenv('ACS_ENDPOINT', '').strip()
    )
    return has_transport and bool(_sender_address())


def _build_client():
    """Return an azure.communication.email EmailClient, or raise if unavailable."""
    from azure.communication.email import EmailClient  # imported lazily

    conn = os.getenv('ACS_CONNECTION_STRING', '').strip()
    if conn:
        return EmailClient.from_connection_string(conn)

    endpoint = os.getenv('ACS_ENDPOINT', '').strip()
    if not endpoint:
        raise RuntimeError('ACS email is not configured (no connection string or endpoint).')

    # Managed-identity path — no secret to store.
    from azure.identity import DefaultAzureCredential
    return EmailClient(endpoint, DefaultAzureCredential())


def _normalize_recipient_address(address: str) -> str:
    """Return a clean recipient address or raise when malformed."""
    _, parsed = parseaddr(str(address or '').strip())
    cleaned = parsed.strip().lower()
    if not cleaned or '@' not in cleaned:
        raise ValueError('Recipient address is invalid.')
    local, _, domain = cleaned.partition('@')
    if not local or not domain:
        raise ValueError('Recipient address is invalid.')
    return f'{local}@{domain}'


def _is_apple_mail_domain(address: str) -> bool:
    domain = str(address or '').partition('@')[2].lower().strip()
    return domain in {'icloud.com', 'me.com', 'mac.com'}


def _send_message(client, message: dict, *, to_address: str) -> None:
    """Send message and fail loudly when ACS reports a non-success status.

    Only checks the status ``begin_send`` resolves with immediately (typically
    "queued" once ACS has accepted the message) — it does not poll for a
    terminal delivery outcome. That polling used to happen here, blocking the
    request thread for up to 25s; delivery failures past this point are no
    longer surfaced synchronously, only via server logs.
    """
    poller = client.begin_send(message)
    result = poller.result()
    if not isinstance(result, dict):
        return

    message_id = str(result.get('id') or result.get('messageId') or '').strip()
    raw_status = str(result.get('status') or '').strip()
    status = raw_status.lower()

    if status in {'succeeded', 'success', 'delivered'}:
        return
    if status in {'queued', 'outfordelivery', 'scheduled', 'started', 'running', 'inprogress'}:
        logger.warning('Email accepted but not yet terminal for %s (status=%s, messageId=%s)', to_address, raw_status or 'unknown', message_id)
        return

    error = result.get('error')
    if isinstance(error, dict):
        detail = str(error.get('message') or error.get('code') or '').strip()
    else:
        detail = str(error or '').strip()
    if not detail:
        detail = f'ACS status={raw_status or "unknown"}'
    if message_id:
        detail = f'{detail} (messageId={message_id})'

    if _is_apple_mail_domain(to_address):
        detail += (
            ' (Apple mailbox domains may reject low-reputation or unverified sender domains. '
            'Use a custom verified ACS sender domain with SPF and DKIM.)'
        )
    raise RuntimeError(detail)


def _wrap_email(*, app_name: str, heading: str, body_html: str, cta_url: Optional[str],
                cta_label: str, footer_note: str) -> str:
    """Wrap message content in a branded, email-client-safe HTML shell.

    Uses table layout and inline styles only (no <style> block or external
    assets) so it renders consistently across mail clients. ``app_name``,
    ``heading``, ``cta_label`` and ``footer_note`` are escaped here; ``body_html``
    is caller-controlled markup (already escaped where it embeds user input).
    ``cta_url`` may be omitted (falsy) for notification-only emails with no
    action to take — the button is left out entirely rather than rendered
    with an empty/`None` href.
    """
    safe_app = html.escape(app_name or 'Keepsake')
    safe_heading = html.escape(heading)
    accent = '#1e6ae1'
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    cta_html = (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0;">'
        f'<tr><td style="border-radius:10px;background:{accent};">'
        f'<a href="{cta_url}" style="display:inline-block;padding:12px 26px;color:#ffffff;'
        'text-decoration:none;font-weight:600;font-size:15px;">'
        f'{html.escape(cta_label)}</a></td></tr></table>'
    ) if cta_url else ''
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0"></head>'
        '<body style="margin:0;padding:0;background:#f2efe9;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f2efe9;padding:32px 12px;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:520px;background:#ffffff;border-radius:16px;overflow:hidden;'
        f'border:1px solid #e6e0d6;font-family:{font};">'
        f'<tr><td style="background:{accent};padding:22px 28px;">'
        f'<span style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:-0.01em;">{safe_app}</span>'
        '</td></tr>'
        '<tr><td style="padding:28px;color:#1a1a1a;font-size:15px;line-height:1.6;">'
        f'<h1 style="margin:0 0 16px;font-size:20px;font-weight:700;color:#1a1a1a;">{safe_heading}</h1>'
        f'{body_html}'
        f'{cta_html}'
        f'<p style="margin:16px 0 0;color:#6a6a6a;font-size:13px;line-height:1.6;">{html.escape(footer_note)}</p>'
        '</td></tr>'
        '<tr><td style="padding:16px 28px;background:#faf8f4;border-top:1px solid #eee7dc;'
        f'color:#9a938a;font-size:12px;">Sent by {safe_app} — your own private photo library.</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def send_password_reset_email(to_address: str, reset_url: str, app_name: str = 'Keepsake') -> None:
    """Send a password-reset email. Raises on misconfiguration or send failure."""
    if not is_configured():
        raise RuntimeError('Email sending is not configured on this deployment.')
    if not to_address:
        raise ValueError('Recipient address is required.')

    to_address = _normalize_recipient_address(to_address)

    sender = _sender_address()
    client = _build_client()

    html_body = _wrap_email(
        app_name=app_name,
        heading='Reset your password',
        body_html=(
            '<p style="margin:0 0 12px;">Hello,</p>'
            '<p style="margin:0 0 12px;">We received a request to reset your '
            f'{html.escape(app_name)} password. Use the button below to choose a new one — '
            'this link expires in 1 hour and can be used once.</p>'
        ),
        cta_url=reset_url,
        cta_label='Reset your password',
        footer_note='If you did not request this, you can safely ignore this email — your password will not change.',
    )
    plain = (
        f'We received a request to reset your {app_name} password.\n'
        f'Open this link (expires in 1 hour, single use):\n{reset_url}\n\n'
        f'If you did not request this, ignore this email.'
    )

    message = {
        'senderAddress': sender,
        'recipients': {'to': [{'address': to_address}]},
        'content': {
            'subject': f'Reset your {app_name} password',
            'plainText': plain,
            'html': html_body,
        },
    }
    _send_message(client, message, to_address=to_address)


def send_invite_email(
    to_address: str,
    invite_url: str,
    *,
    library_name: str = '',
    inviter: str = '',
    app_name: str = 'Keepsake',
) -> None:
    """Send a library invitation email. Raises on misconfiguration or failure.

    ``library_name`` and ``inviter`` are owner-supplied and are HTML-escaped
    before being embedded, so a malicious library name can't inject markup into
    the email body (stored-XSS-in-email defense).
    """
    if not is_configured():
        raise RuntimeError('Email sending is not configured on this deployment.')
    if not to_address:
        raise ValueError('Recipient address is required.')

    to_address = _normalize_recipient_address(to_address)

    sender = _sender_address()
    client = _build_client()

    safe_lib = html.escape(library_name or '')
    safe_inviter = html.escape(inviter or '') or 'Someone'
    where_html = f' to <strong>{safe_lib}</strong>' if safe_lib else ''
    where_plain = f' to {library_name}' if library_name else ''

    html_body = _wrap_email(
        app_name=app_name,
        heading='You have an invitation',
        body_html=(
            '<p style="margin:0 0 12px;">Hello,</p>'
            f'<p style="margin:0 0 12px;">{safe_inviter} has invited you{where_html} on '
            f'{html.escape(app_name)}. Use the button below to accept — this link expires '
            'in 72 hours and can be used once.</p>'
        ),
        cta_url=invite_url,
        cta_label='Accept your invitation',
        footer_note='If you were not expecting this, you can safely ignore this email.',
    )
    plain = (
        f'{safe_inviter} has invited you{where_plain} on {app_name}.\n'
        f'Open this link to accept (expires in 72 hours, single use):\n{invite_url}\n\n'
        f'If you were not expecting this, ignore this email.'
    )

    message = {
        'senderAddress': sender,
        'recipients': {'to': [{'address': to_address}]},
        'content': {
            'subject': f'You have been invited to {app_name}',
            'plainText': plain,
            'html': html_body,
        },
    }
    _send_message(client, message, to_address=to_address)


def send_library_clean_email(
    to_address: str,
    confirm_url: str,
    *,
    library_name: str = '',
    requested_by: str = '',
    app_name: str = 'Keepsake',
) -> None:
    """Send a "confirm library cleanup" email. Raises on misconfiguration/failure.

    ``library_name`` and ``requested_by`` are owner-supplied and are
    HTML-escaped before being embedded (stored-XSS-in-email defense).
    """
    if not is_configured():
        raise RuntimeError('Email sending is not configured on this deployment.')
    if not to_address:
        raise ValueError('Recipient address is required.')

    to_address = _normalize_recipient_address(to_address)

    sender = _sender_address()
    client = _build_client()

    safe_lib = html.escape(library_name or 'your library')
    safe_requester = html.escape(requested_by or '') or 'The library owner'

    html_body = _wrap_email(
        app_name=app_name,
        heading='Confirm library cleanup',
        body_html=(
            '<p style="margin:0 0 12px;">Hello,</p>'
            f'<p style="margin:0 0 12px;">{safe_requester} has requested to permanently delete '
            f'<strong>all photos and videos</strong> in <strong>{safe_lib}</strong> on {html.escape(app_name)}. '
            'This cannot be undone.</p>'
            '<p style="margin:0 0 12px;">If you agree, confirm below — this link expires in 30 minutes '
            'and can be used once.</p>'
        ),
        cta_url=confirm_url,
        cta_label='Confirm cleanup',
        footer_note='If you did not expect this, do not click the button — ignore this email and let the owner know.',
    )
    plain = (
        f'{safe_requester} has requested to permanently delete all photos and videos in {library_name or "your library"} '
        f'on {app_name}. This cannot be undone.\n\n'
        f'To confirm, open this link (expires in 30 minutes, single use):\n{confirm_url}\n\n'
        f'If you did not expect this, ignore this email.'
    )

    message = {
        'senderAddress': sender,
        'recipients': {'to': [{'address': to_address}]},
        'content': {
            'subject': f'Confirm: delete all photos in {library_name or "your library"}',
            'plainText': plain,
            'html': html_body,
        },
    }
    _send_message(client, message, to_address=to_address)


def send_library_cleanup_complete_email(
    to_address: str,
    library_name: str = '',
    photos_deleted: int = 0,
    app_name: str = 'Keepsake',
) -> None:
    """Send a "library cleanup completed" notification email. Raises on misconfiguration/failure.

    ``library_name`` is owner-supplied and is HTML-escaped before embedding.
    """
    if not is_configured():
        raise RuntimeError('Email sending is not configured on this deployment.')
    if not to_address:
        raise ValueError('Recipient address is required.')

    to_address = _normalize_recipient_address(to_address)

    sender = _sender_address()
    client = _build_client()

    safe_lib = html.escape(library_name or 'your library')
    photos_str = f'{photos_deleted} photo(s)/video(s)' if photos_deleted > 0 else 'all photos and videos'

    html_body = _wrap_email(
        app_name=app_name,
        heading='Library cleanup complete',
        body_html=(
            '<p style="margin:0 0 12px;">Hello,</p>'
            f'<p style="margin:0 0 12px;">The cleanup of <strong>{safe_lib}</strong> on {html.escape(app_name)} has completed. '
            f'{photos_str} have been removed.</p>'
            '<p style="margin:0 0 12px;">You can now upload new photos and videos as usual.</p>'
        ),
        cta_url=None,
        cta_label='',
        footer_note='',
    )
    plain = (
        f'The cleanup of {library_name or "your library"} on {app_name} has completed. '
        f'{photos_str} have been removed.\n\n'
        'You can now upload new photos and videos as usual.'
    )

    message = {
        'senderAddress': sender,
        'recipients': {'to': [{'address': to_address}]},
        'content': {
            'subject': f'Cleanup complete: {library_name or "your library"}',
            'plainText': plain,
            'html': html_body,
        },
    }
    _send_message(client, message, to_address=to_address)


def masked_recipient(address: Optional[str]) -> str:
    """Return a privacy-preserving hint like ``j***@example.com`` for UI display."""
    address = (address or '').strip()
    if '@' not in address:
        return ''
    local, _, domain = address.partition('@')
    head = local[0] if local else ''
    return f'{head}***@{domain}'
