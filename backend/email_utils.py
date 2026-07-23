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


def send_password_reset_email(to_address: str, reset_url: str, app_name: str = 'Photostore') -> None:
    """Send a password-reset email. Raises on misconfiguration or send failure."""
    if not is_configured():
        raise RuntimeError('Email sending is not configured on this deployment.')
    if not to_address:
        raise ValueError('Recipient address is required.')

    sender = _sender_address()
    client = _build_client()

    html = (
        f'<p>Hello,</p>'
        f'<p>We received a request to reset your {app_name} password. '
        f'Click the link below to choose a new one. This link expires in 1 hour '
        f'and can be used once.</p>'
        f'<p><a href="{reset_url}">Reset your password</a></p>'
        f'<p>If you did not request this, you can safely ignore this email — '
        f'your password will not change.</p>'
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
            'html': html,
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
    app_name: str = 'Photostore',
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

    html_body = (
        f'<p>Hello,</p>'
        f'<p>{safe_inviter} has invited you{where_html} on {app_name}. '
        f'Click the link below to accept. This link expires in 72 hours and can '
        f'be used once.</p>'
        f'<p><a href="{invite_url}">Accept your invitation</a></p>'
        f'<p>If you were not expecting this, you can safely ignore this email.</p>'
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
