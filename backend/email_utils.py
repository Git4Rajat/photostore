"""Transactional email via Azure Communication Services (ACS).

Sends password-reset, invitation, album-shared and library-clean emails, laid out as in the
redesign mock-up's Emails screen (see the build_* functions, which are pure and testable).
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
from datetime import datetime, timezone
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
        logger.info('Email accepted for %s (status=%s, messageId=%s)', to_address, raw_status or 'unknown', message_id)
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


# Palette mirrors the redesign mock-up's light theme; the dark values are applied
# through a prefers-color-scheme block (Apple Mail, Gmail app, Outlook.com, Samsung).
# Clients that ignore <style> keep the inline light colours, which stay legible
# on their own because every background is set explicitly next to its text colour.
_LIGHT = {
    'page': '#f5f1ea', 'card': '#fffdf9', 'facts': '#f5f1ea', 'ink': '#1d1a16', 'muted': '#665f54',
    'line': '#e6e0d6', 'accent': '#2f5d82', 'on_accent': '#ffffff', 'danger': '#c23a4b', 'on_danger': '#ffffff',
}
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_DISPLAY = "Jost,Futura,'Century Gothic','Trebuchet MS',Helvetica,Arial,sans-serif"

_DARK_CSS = (
    ':root{color-scheme:light dark;supported-color-schemes:light dark;}'
    '@media (prefers-color-scheme: dark){'
    '.k-page{background:#131316 !important;}'
    '.k-card{background:#1c1c20 !important;}'
    '.k-facts{background:#131316 !important;}'
    '.k-ink,.k-ink a{color:#ede7da !important;}'
    '.k-muted{color:#a39b8c !important;}'
    '.k-line{border-color:#2f2f34 !important;}'
    '.k-mark{background:#7aaed3 !important;color:#0c1924 !important;}'
    '.k-pri{background:#7aaed3 !important;}'
    '.k-pri a{color:#0c1924 !important;}'
    '.k-dan{background:#ff7d8c !important;}'
    '.k-dan a{color:#2b0b11 !important;}'
    '}'
)


def _esc(value) -> str:
    return html.escape(str(value or ''))


def _p(text_html: str, *, muted: bool = False) -> str:
    cls, color, size = ('k-muted', _LIGHT['muted'], 12) if muted else ('k-ink', _LIGHT['ink'], 14)
    return (f'<p class="{cls}" style="margin:0 0 14px;font-size:{size}px;line-height:1.55;'
            f'color:{color};">{text_html}</p>')


def _facts_html(facts: list) -> str:
    """Rounded box listing short facts, one per row (``facts`` are plain strings)."""
    if not facts:
        return ''
    rows = ''.join(
        f'<tr><td class="k-ink" style="padding:4px 0;font-size:13px;line-height:1.4;color:{_LIGHT["ink"]};">'
        f'<span class="k-muted" style="color:{_LIGHT["muted"]};">&#8226;&nbsp;</span>{_esc(fact)}</td></tr>'
        for fact in facts
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px;">'
        f'<tr><td class="k-facts" style="background:{_LIGHT["facts"]};border-radius:14px;padding:8px 14px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0">{rows}</table></td></tr></table>'
    )


def _button_html(url: str, label: str, *, danger: bool = False) -> str:
    bg, fg = (_LIGHT['danger'], _LIGHT['on_danger']) if danger else (_LIGHT['accent'], _LIGHT['on_accent'])
    cls = 'k-dan' if danger else 'k-pri'
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 14px;">'
        f'<tr><td align="center" class="{cls}" bgcolor="{bg}" style="background:{bg};border-radius:999px;">'
        f'<a href="{html.escape(url, quote=True)}" style="display:block;padding:14px 24px;color:{fg};'
        f'text-decoration:none;font-weight:600;font-size:15px;border-radius:999px;">{_esc(label)}</a>'
        '</td></tr></table>'
    )


def _cover_html(title: str, subtitle: str) -> str:
    """Album cover block. The mock-up's cover is the album photo; email has no
    authenticated image access, so it is a dark band carrying the same title."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px;">'
        '<tr><td bgcolor="#1b1733" height="124" valign="bottom" style="background:#1b1733;'
        'background-image:linear-gradient(180deg,#1b1733 0%,#2e5a55 70%,#0f1517 100%);border-radius:12px;'
        'padding:14px;height:124px;">'
        f'<div style="font-family:{_DISPLAY};font-size:20px;font-weight:600;line-height:1.15;color:#ffffff;">{_esc(title)}</div>'
        f'<div style="font-size:12.5px;color:#ffffff;opacity:.9;">{_esc(subtitle)}</div>'
        '</td></tr></table>'
    )


def _wrap_email(*, app_name: str, heading: str, body_html: str, cta_url: Optional[str] = None,
                cta_label: str = '', cta_danger: bool = False, after_html: str = '',
                preheader: str = '', show_heading: bool = True) -> str:
    """Wrap message content in the redesigned, email-client-safe HTML shell.

    Mirrors the Emails screen of the redesign mock-up: the Keepsake mark and
    wordmark, a serif-free display heading, the body, at most one wide button,
    then fine print (``after_html``). Table layout and inline styles carry the
    light look; a small prefers-color-scheme block (classes ``k-*``) switches
    to the dark palette where clients support it.

    ``app_name``, ``heading``, ``cta_label`` and ``preheader`` are escaped here;
    ``body_html`` and ``after_html`` are caller-built markup (already escaped
    where they embed user input). ``cta_url`` may be falsy for notification-only
    emails; the button is then left out.
    """
    safe_app = _esc(app_name or 'Keepsake')
    c = _LIGHT
    brand = (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 16px;"><tr>'
        f'<td class="k-mark" width="22" height="22" align="center" bgcolor="{c["accent"]}" '
        f'style="width:22px;height:22px;border-radius:7px;background:{c["accent"]};color:{c["on_accent"]};'
        'font-size:13px;line-height:22px;font-family:Arial,sans-serif;">&#9829;</td>'
        f'<td class="k-ink" style="padding-left:7px;font-family:{_DISPLAY};font-size:15px;font-weight:600;'
        f'color:{c["ink"]};">{safe_app}</td></tr></table>'
    )
    heading_html = (
        f'<h1 class="k-ink" style="margin:0 0 14px;font-family:{_DISPLAY};font-size:23px;line-height:1.2;'
        f'font-weight:600;color:{c["ink"]};">{_esc(heading)}</h1>'
    ) if show_heading else ''
    hidden = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_esc(preheader)}</div>'
        if preheader else ''
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f'<title>{_esc(heading)}</title><style>{_DARK_CSS}</style></head>'
        f'<body class="k-page" style="margin:0;padding:0;background:{c["page"]};">{hidden}'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'class="k-page" style="background:{c["page"]};padding:24px 12px;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'class="k-card" style="max-width:480px;background:{c["card"]};border-radius:18px;font-family:{_FONT};">'
        '<tr><td style="padding:22px 20px 18px;">'
        f'{brand}'
        f'{heading_html}'
        f'{body_html}'
        f'{_button_html(cta_url, cta_label, danger=cta_danger) if cta_url else ""}'
        f'{after_html}'
        '</td></tr></table></td></tr></table></body></html>'
    )


def _display_name(person: str) -> str:
    """Name to show for a person. An email address becomes its local part, because mail
    clients (Gmail) turn anything that looks like an address into a blue underlined link."""
    person = (person or '').strip()
    if '@' in person:
        person = person.partition('@')[0].replace('.', ' ').replace('_', ' ').strip()
        if person.islower():
            person = person[:1].upper() + person[1:]
    return person


def _plural(n: int, one: str, many: str) -> str:
    return f'{n} {one if n == 1 else many}'


def _format_expiry(expires_at: Optional[datetime]) -> str:
    """"14 Nov at 01:00 UTC" — the server has no idea of the reader's timezone."""
    if expires_at is None:
        return ''
    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc)
    return f'{expires_at.day} {expires_at:%b} at {expires_at:%H:%M} UTC'


def _fine(text: str) -> str:
    return _p(_esc(text), muted=True)


def _hr() -> str:
    return f'<hr class="k-line" style="border:0;border-top:1px solid {_LIGHT["line"]};margin:2px 0 14px;">'


def build_password_reset_email(to_address: str, reset_url: str, app_name: str = 'Keepsake') -> dict:
    """Return ``{'subject', 'plainText', 'html'}`` for the password-reset email."""
    account = _esc(to_address)
    body = _p(f'Someone asked to reset the password for {account}. If that was you, choose a new one below.')
    after = _fine('The link works once and ends in 1 hour.') + _hr() + _fine(
        "Didn't ask for this? Ignore this email. Your password stays the same.")
    plain = (
        f'{app_name}\n\nChoose a new password\n\n'
        f'Someone asked to reset the password for {to_address}. If that was you, choose a new one here:\n\n'
        f'{reset_url}\n\n'
        'The link works once and ends in 1 hour.\n\n'
        "Didn't ask for this? Ignore this email. Your password stays the same.\n"
    )
    return {
        'subject': f'Reset your {app_name} password',
        'plainText': plain,
        'html': _wrap_email(app_name=app_name, heading='Choose a new password', body_html=body,
                            cta_url=reset_url, cta_label='Choose a new password', after_html=after,
                            preheader='The link works once and ends in 1 hour.'),
    }


def build_invite_email(invite_url: str, *, library_name: str = '', inviter: str = '',
                       app_name: str = 'Keepsake', member_count: Optional[int] = None,
                       photo_count: Optional[int] = None,
                       expires_at: Optional[datetime] = None) -> dict:
    """Return ``{'subject', 'plainText', 'html'}`` for a library invitation.

    ``library_name`` empty means the invitee gets a library of their own. Counts
    and ``expires_at`` are optional; the lines that need them are left out or
    reworded rather than showing a guess.
    """
    who = _display_name(inviter) or 'Someone'
    lib = (library_name or '').strip()
    if lib:
        subject = f'{who} invited you to {lib}'
        heading = f'{who} invited you to join {lib}'
        if photo_count is not None:
            intro = (f"You'll see the library's {_plural(photo_count, 'photo or video', 'photos and videos')} "
                     'and can add your own.')
        else:
            intro = "You'll see the library's photos and videos and can add your own."
        facts = []
        if member_count is not None:
            facts.append(_plural(member_count, 'member', 'members'))
        facts.append('Private. Only members can see it.')
    else:
        subject = f'{who} invited you to {app_name}'
        heading = f'{who} invited you to {app_name}'
        intro = "You'll get a private library of your own to keep your photos and videos in."
        facts = ['Private. Only you can see it.']
    expiry = _format_expiry(expires_at)
    ends = f'ends on {expiry}' if expiry else 'ends in 72 hours'
    fine = f'This link works once and {ends}. Not expecting this? Ignore this email and nothing happens.'
    plain = (
        f'{app_name}\n\n{heading}\n\n{intro}\n\n'
        + ''.join(f'- {fact}\n' for fact in facts)
        + f'\nAccept invite:\n{invite_url}\n\n{fine}\n'
    )
    return {
        'subject': subject,
        'plainText': plain,
        'html': _wrap_email(app_name=app_name, heading=heading, body_html=_p(_esc(intro)) + _facts_html(facts),
                            cta_url=invite_url, cta_label='Accept invite', after_html=_fine(fine),
                            preheader=intro),
    }


def build_album_shared_email(album_url: str, *, album_name: str, sender: str = '',
                             item_count: Optional[int] = None, expires_on: Optional[datetime] = None,
                             needs_access_code: bool = False, app_name: str = 'Keepsake') -> dict:
    """Return ``{'subject', 'plainText', 'html'}`` for "an album was shared with you".

    The access code is never part of this email; when the album needs one the
    fine print says it is sent separately.
    """
    who = _display_name(sender) or 'Someone'
    sub = _plural(item_count, 'photo or video', 'photos and videos') if item_count is not None else 'Shared album'
    intro = (f'{who} shared this album with you. You can look through it in your browser, '
             'and download it if you like.')
    until = f'The link works until {expires_on.day} {expires_on:%b}.' if expires_on else 'The link works until it is turned off.'
    fine = until + (f' This album also needs an access code, which {who} will send you separately.'
                    if needs_access_code else '')
    plain = f'{app_name}\n\n{album_name}\n{sub}\n\n{intro}\n\nView album:\n{album_url}\n\n{fine}\n'
    return {
        'subject': f'{who} shared “{album_name}” with you',
        'plainText': plain,
        'html': _wrap_email(app_name=app_name, heading=album_name,
                            body_html=_cover_html(album_name, sub) + _p(_esc(intro)),
                            cta_url=album_url, cta_label='View album', after_html=_fine(fine),
                            preheader=intro, show_heading=False),
    }


def build_library_clean_email(confirm_url: str, *, to_address: str = '', library_name: str = '',
                              requested_by: str = '', app_name: str = 'Keepsake',
                              photo_count: Optional[int] = None, album_count: Optional[int] = None,
                              member_count: Optional[int] = None) -> dict:
    """Return ``{'subject', 'plainText', 'html'}`` for the delete-everything confirmation.

    Recipients other than the requester are a second approver: the wording says
    who asked instead of "you asked".
    """
    lib = (library_name or '').strip() or 'your library'
    is_requester = not requested_by.strip() or requested_by.strip().lower() == (to_address or '').strip().lower()
    requester = _display_name(requested_by)
    intro = ('You asked to delete everything in this library.' if is_requester
             else f'{requester} asked to delete everything in this library, and it needs your confirmation too.')
    facts = []
    if photo_count is not None:
        facts.append(_plural(photo_count, 'photo or video', 'photos and videos'))
    if album_count is not None:
        facts.append(_plural(album_count, 'album', 'albums'))
    if member_count is not None:
        facts.append(f'Access for {_plural(member_count, "member", "members")}')
    warning = "This can't be undone. Nothing is deleted until you tap the button."
    fine = ("The link ends in 30 minutes. If you didn't ask for this, ignore it and change your password."
            if is_requester else
            "The link ends in 30 minutes. If you weren't expecting this, ignore it and nothing is deleted.")
    body = (_p(_esc(intro)) + _facts_html(facts)
            + _p(f'<b>This can\'t be undone.</b> Nothing is deleted until you tap the button.'))
    heading = f'Confirm you want to delete {lib}'
    plain = (
        f'{app_name}\n\n{heading}\n\n{intro}\n\n'
        + ''.join(f'- {fact}\n' for fact in facts)
        + f'\n{warning}\n\nDelete everything:\n{confirm_url}\n\n{fine}\n'
    )
    return {
        'subject': f'Confirm deleting {lib}',
        'plainText': plain,
        'html': _wrap_email(app_name=app_name, heading=heading, body_html=body, cta_url=confirm_url,
                            cta_label='Delete everything', cta_danger=True, after_html=_fine(fine),
                            preheader=intro),
    }


def build_library_cleanup_complete_email(*, library_name: str = '', photos_deleted: int = 0,
                                         app_name: str = 'Keepsake') -> dict:
    """Return ``{'subject', 'plainText', 'html'}`` for the "cleanup finished" notice (no button)."""
    lib = (library_name or '').strip() or 'your library'
    removed = (f'{_plural(photos_deleted, "photo or video", "photos and videos")} removed.'
               if photos_deleted > 0 else 'All photos and videos have been removed.')
    intro = f'Everything in {lib} has been deleted. {removed}'
    follow = 'You can upload new photos and videos as usual.'
    return {
        'subject': f'{lib} is now empty',
        'plainText': f'{app_name}\n\nLibrary deleted\n\n{intro}\n\n{follow}\n',
        'html': _wrap_email(app_name=app_name, heading='Library deleted',
                            body_html=_p(_esc(intro)) + _p(_esc(follow)), preheader=intro),
    }


def _send_built(to_address: str, built: dict) -> None:
    if not is_configured():
        raise RuntimeError('Email sending is not configured on this deployment.')
    if not to_address:
        raise ValueError('Recipient address is required.')
    to_address = _normalize_recipient_address(to_address)
    client = _build_client()
    message = {
        'senderAddress': _sender_address(),
        'recipients': {'to': [{'address': to_address}]},
        'content': {'subject': built['subject'], 'plainText': built['plainText'], 'html': built['html']},
    }
    _send_message(client, message, to_address=to_address)


def send_password_reset_email(to_address: str, reset_url: str, app_name: str = 'Keepsake') -> None:
    """Send a password-reset email. Raises on misconfiguration or send failure."""
    _send_built(to_address, build_password_reset_email(to_address, reset_url, app_name))


def send_invite_email(
    to_address: str,
    invite_url: str,
    *,
    library_name: str = '',
    inviter: str = '',
    app_name: str = 'Keepsake',
    member_count: Optional[int] = None,
    photo_count: Optional[int] = None,
    expires_at: Optional[datetime] = None,
) -> None:
    """Send a library invitation email. Raises on misconfiguration or failure.

    ``library_name`` and ``inviter`` are owner-supplied and are HTML-escaped
    before being embedded, so a malicious library name can't inject markup into
    the email body (stored-XSS-in-email defense).
    """
    _send_built(to_address, build_invite_email(
        invite_url, library_name=library_name, inviter=inviter, app_name=app_name,
        member_count=member_count, photo_count=photo_count, expires_at=expires_at))


def send_album_shared_email(to_address: str, album_url: str, *, album_name: str, sender: str = '',
                            item_count: Optional[int] = None, expires_on: Optional[datetime] = None,
                            needs_access_code: bool = False, app_name: str = 'Keepsake') -> None:
    """Send an "album shared with you" email. Raises on misconfiguration or failure."""
    _send_built(to_address, build_album_shared_email(
        album_url, album_name=album_name, sender=sender, item_count=item_count,
        expires_on=expires_on, needs_access_code=needs_access_code, app_name=app_name))


def send_library_clean_email(
    to_address: str,
    confirm_url: str,
    *,
    library_name: str = '',
    requested_by: str = '',
    app_name: str = 'Keepsake',
    photo_count: Optional[int] = None,
    album_count: Optional[int] = None,
    member_count: Optional[int] = None,
) -> None:
    """Send a "confirm library cleanup" email. Raises on misconfiguration/failure.

    ``library_name`` and ``requested_by`` are owner-supplied and are
    HTML-escaped before being embedded (stored-XSS-in-email defense).
    """
    _send_built(to_address, build_library_clean_email(
        confirm_url, to_address=to_address, library_name=library_name, requested_by=requested_by,
        app_name=app_name, photo_count=photo_count, album_count=album_count, member_count=member_count))


def send_library_cleanup_complete_email(
    to_address: str,
    library_name: str = '',
    photos_deleted: int = 0,
    app_name: str = 'Keepsake',
) -> None:
    """Send a "library cleanup completed" notification email. Raises on misconfiguration/failure."""
    _send_built(to_address, build_library_cleanup_complete_email(
        library_name=library_name, photos_deleted=photos_deleted, app_name=app_name))


def masked_recipient(address: Optional[str]) -> str:
    """Return a privacy-preserving hint like ``j***@example.com`` for UI display."""
    address = (address or '').strip()
    if '@' not in address:
        return ''
    local, _, domain = address.partition('@')
    head = local[0] if local else ''
    return f'{head}***@{domain}'
