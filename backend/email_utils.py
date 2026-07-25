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
import os
from typing import Optional


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


def _wrap_email(*, app_name: str, heading: str, body_html: str, cta_url: str,
                cta_label: str, footer_note: str) -> str:
    """Wrap message content in a branded, email-client-safe HTML shell.

    Uses table layout and inline styles only (no <style> block or external
    assets) so it renders consistently across mail clients. ``app_name``,
    ``heading``, ``cta_label`` and ``footer_note`` are escaped here; ``body_html``
    is caller-controlled markup (already escaped where it embeds user input).
    """
    safe_app = html.escape(app_name or 'Keepsake')
    safe_heading = html.escape(heading)
    accent = '#1e6ae1'
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
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
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0;">'
        f'<tr><td style="border-radius:10px;background:{accent};">'
        f'<a href="{cta_url}" style="display:inline-block;padding:12px 26px;color:#ffffff;'
        'text-decoration:none;font-weight:600;font-size:15px;">'
        f'{html.escape(cta_label)}</a></td></tr></table>'
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
    poller = client.begin_send(message)
    poller.result()  # wait for the send to be accepted


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
    poller = client.begin_send(message)
    poller.result()  # wait for the send to be accepted


def masked_recipient(address: Optional[str]) -> str:
    """Return a privacy-preserving hint like ``j***@example.com`` for UI display."""
    address = (address or '').strip()
    if '@' not in address:
        return ''
    local, _, domain = address.partition('@')
    head = local[0] if local else ''
    return f'{head}***@{domain}'
