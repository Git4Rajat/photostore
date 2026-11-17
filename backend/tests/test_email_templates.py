"""Rendering of the redesigned emails (Emails screen of the redesign mock-up). Nothing is sent."""
import re
from datetime import datetime, timezone

import pytest

import email_utils as eu


def _check_common(built):
    assert set(built) == {'subject', 'plainText', 'html'}
    html = built['html']
    assert html.startswith('<!DOCTYPE html>')
    assert 'prefers-color-scheme: dark' in html
    assert 'color-scheme' in html
    assert '<script' not in html and 'src=' not in html  # no external assets
    # exactly one button
    assert html.count('text-decoration:none') <= 1


def _urls(text):
    return re.findall(r'https?://\S+', text)


def test_invite_matches_mockup():
    b = eu.build_invite_email(
        'https://x.test/accept-invite?token=abc', library_name='Verma family', inviter='Rajat',
        member_count=2, photo_count=31, expires_at=datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc))
    _check_common(b)
    assert b['subject'] == 'Rajat invited you to Verma family'
    assert 'Rajat invited you to join Verma family' in b['html']
    assert "You'll see the library's 31 photos and videos and can add your own." in b['plainText']
    assert '2 members' in b['html'] and 'Private. Only members can see it.' in b['html']
    assert 'Accept invite' in b['html']
    assert 'This link works once and ends on 14 Nov at 01:00 UTC. Not expecting this? Ignore this email and nothing happens.' in b['plainText']
    assert 'href="https://x.test/accept-invite?token=abc"' in b['html']
    assert _urls(b['plainText']) == ['https://x.test/accept-invite?token=abc']


def test_invite_without_optional_facts_and_fresh_target():
    b = eu.build_invite_email('https://x.test/i', library_name='Lib')
    assert 'Someone invited you to Lib' == b['subject']
    assert ' member' not in b['html'].replace('Only members', '') and 'ends in 72 hours' in b['plainText']
    fresh = eu.build_invite_email('https://x.test/i', inviter='a@b.c')
    assert fresh['subject'] == 'a@b.c invited you to Keepsake'


def test_invite_escapes_user_input():
    b = eu.build_invite_email('https://x.test/i?a=1&b="2', library_name='<img src=x onerror=1>', inviter='<b>x</b>')
    assert '<img' not in b['html'] and '&lt;img' in b['html']
    assert 'a=1&amp;b=&quot;2' in b['html']


def test_password_reset_matches_mockup():
    b = eu.build_password_reset_email('rajat@example.com', 'https://x.test/reset-password?token=t')
    _check_common(b)
    assert b['subject'] == 'Reset your Keepsake password'
    for text in (b['html'], b['plainText']):
        assert 'Choose a new password' in text
        assert 'Someone asked to reset the password for rajat@example.com.' in text
        assert 'The link works once and ends in 1 hour.' in text
        assert "Didn&#x27;t ask for this?" in text or "Didn't ask for this? Ignore this email. Your password stays the same." in text
    assert _urls(b['plainText']) == ['https://x.test/reset-password?token=t']


def test_album_shared_matches_mockup():
    b = eu.build_album_shared_email(
        'https://x.test/s/abc', album_name='Malmö nights', sender='Rajat', item_count=4,
        expires_on=datetime(2026, 12, 14), needs_access_code=True)
    _check_common(b)
    assert b['subject'] == 'Rajat shared “Malmö nights” with you'
    assert 'View album' in b['html'] and '4 photos and videos' in b['html']
    assert b['html'].count('Malmö nights') >= 2 and '<h1' not in b['html']
    assert 'The link works until 14 Dec. This album also needs an access code, which Rajat will send you separately.' in b['plainText']
    assert 'code:' not in b['plainText'].lower()
    plain_open = eu.build_album_shared_email('https://x.test/s/abc', album_name='A')
    assert 'access code' not in plain_open['plainText']


def test_clean_confirmation_matches_mockup():
    b = eu.build_library_clean_email(
        'https://x.test/confirm-library-clean?token=t', to_address='rajat@example.com',
        library_name='Verma family', requested_by='rajat@example.com',
        photo_count=31, album_count=2, member_count=2)
    _check_common(b)
    assert b['subject'] == 'Confirm deleting Verma family'
    assert 'Confirm you want to delete Verma family' in b['html']
    assert 'You asked to delete everything in this library.' in b['plainText']
    assert '31 photos and videos' in b['html'] and '2 albums' in b['html'] and 'Access for 2 members' in b['html']
    assert "<b>This can't be undone.</b>" in b['html'].replace('&#x27;', "'")
    assert 'Delete everything' in b['html'] and '#c23a4b' in b['html']  # the only red button
    assert 'The link ends in 30 minutes. If you' in b['plainText']
    assert _urls(b['plainText']) == ['https://x.test/confirm-library-clean?token=t']


def test_clean_confirmation_second_approver_wording():
    b = eu.build_library_clean_email('https://x.test/c', to_address='b@x.test', library_name='L', requested_by='a@x.test')
    assert 'a@x.test asked to delete everything in this library, and it needs your confirmation too.' in b['plainText']
    assert 'You asked' not in b['plainText']


def test_non_danger_emails_have_no_red_button():
    for b in (eu.build_invite_email('https://x.test/i'), eu.build_password_reset_email('a@b.c', 'https://x.test/r')):
        assert '#c23a4b' not in b['html'].split('</style>')[1]


def test_cleanup_complete_has_no_button():
    b = eu.build_library_cleanup_complete_email(library_name='Lib', photos_deleted=3)
    _check_common(b)
    assert 'href=' not in b['html'] and '3 photos and videos removed.' in b['plainText']


def test_send_uses_built_content(monkeypatch):
    sent = {}

    class Client:
        def begin_send(self, message):
            sent.update(message)

            class P:
                def result(self_inner):
                    return {'status': 'Succeeded'}
            return P()

    monkeypatch.setenv('ACS_CONNECTION_STRING', 'endpoint=x')
    monkeypatch.setenv('ACS_SENDER_ADDRESS', 'noreply@x.test')
    monkeypatch.setattr(eu, '_build_client', lambda: Client())
    eu.send_library_clean_email('Owner@Example.com', 'https://x.test/c', library_name='L', requested_by='owner@example.com')
    assert sent['recipients']['to'] == [{'address': 'owner@example.com'}]
    assert sent['content']['subject'] == 'Confirm deleting L'
    assert 'https://x.test/c' in sent['content']['plainText']
    with pytest.raises(ValueError):
        eu.send_invite_email('', 'https://x.test/i')
