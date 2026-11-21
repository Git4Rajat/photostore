"""Stored EXIF JSON must fit one Azure Table string property (32,768 chars)."""
import json

import exif_utils

AZURE_TABLE_STRING_LIMIT = 32768


def test_storage_limit_is_below_the_table_string_limit():
    assert exif_utils.MAX_EXIF_STORAGE_LENGTH < AZURE_TABLE_STRING_LIMIT


def test_oversized_exif_is_trimmed_to_fit_a_table_property():
    exif = {f'Tag{i:04d}': 'x' * 400 for i in range(200)}
    trimmed = exif_utils._trim_exif_for_storage(exif)
    stored = json.dumps(trimmed, ensure_ascii=False, separators=(',', ':'))
    assert len(stored) < AZURE_TABLE_STRING_LIMIT
    assert trimmed['EXIF.Truncated'] == 'true'


def test_small_exif_is_unchanged():
    exif = {'Make': 'Apple'}
    assert exif_utils._trim_exif_for_storage(exif) == exif
