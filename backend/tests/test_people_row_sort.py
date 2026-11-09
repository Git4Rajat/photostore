from storage_utils import _sort_people_rows


def test_named_first_then_unnamed_by_photo_count_desc_ties_keep_order():
    rows = [
        {'personId': 'u1', 'isNamed': False, 'faceCount': 3},
        {'personId': 'n1', 'isNamed': True, 'faceCount': 1},
        {'personId': 'u2', 'isNamed': False, 'faceCount': 9},
        {'personId': 'n2', 'isNamed': True, 'faceCount': 50},
        {'personId': 'u3', 'isNamed': False, 'faceCount': 3},
    ]
    _sort_people_rows(rows)
    assert [r['personId'] for r in rows] == ['n1', 'n2', 'u2', 'u1', 'u3']
