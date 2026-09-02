"""Unit tests for _validate_client_blob_name.

/upload/finalize used to always re-derive the blob name it checks via
read_pending_anonymous_blob(user_id, filename) -- a lookup keyed only by
filename. When several files share an original filename (e.g. many photos
all named "Ip_image.jpeg"), every one of their /upload/init calls reserves
its own distinct blob but writes it onto that SAME shared metadata row
(renaming apart into distinct rows only happens later, inside finalize
itself) -- so whichever file's init call ran last "wins" the row, and an
earlier file's finalize call reads the wrong blob name and 404s/mismatches.
The fix has the client echo back the blobName it already received from its
own /upload/init call; _validate_client_blob_name is the server-side gate on
that client-supplied value, only accepting the exact UUID shape
_generate_anonymous_id() produces.
"""
from __future__ import annotations

import app


def test_accepts_a_well_formed_anonymous_blob_uuid():
    assert app._validate_client_blob_name('550e8400-e29b-41d4-a716-446655440000') == \
        '550e8400-e29b-41d4-a716-446655440000'


def test_accepts_uppercase_uuid():
    value = '550E8400-E29B-41D4-A716-446655440000'
    assert app._validate_client_blob_name(value) == value


def test_rejects_missing_value():
    assert app._validate_client_blob_name(None) is None
    assert app._validate_client_blob_name('') is None


def test_rejects_a_plain_filename():
    # Guards against a client (buggy or malicious) pointing finalize at an
    # arbitrary blob name instead of one legitimately minted by /upload/init.
    assert app._validate_client_blob_name('Ip_image.jpeg') is None


def test_rejects_path_like_values():
    assert app._validate_client_blob_name('../other-user-blob') is None
    assert app._validate_client_blob_name('a/b/c') is None


def test_rejects_non_string_input():
    assert app._validate_client_blob_name(12345) is None
    assert app._validate_client_blob_name({'not': 'a string'}) is None
