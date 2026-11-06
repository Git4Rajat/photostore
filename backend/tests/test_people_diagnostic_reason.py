"""The diagnostics headline names what is really keeping faces out of clustering."""
from routes.people import _no_faces_accepted_reason as reason


def test_names_the_biggest_blocker_first():
    text = reason(rejected=2, suspicious=74, stale_version=0, no_embedding=698)
    assert text.startswith('No face can be clustered: 698 faces have no embedding yet')
    assert text.index('698') < text.index('74 faces') < text.index('2 faces were rejected')


def test_leaves_out_empty_buckets():
    text = reason(rejected=0, suspicious=3, stale_version=0, no_embedding=0)
    assert text == 'No face can be clustered: 3 faces are held back as suspicious.'
