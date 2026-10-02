"""Cold partition sort-merge tests using lazy, ordered SDK-shaped iterators."""
from contextlib import nullcontext
import inspect

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
import numpy as np
import pytest

pytest.importorskip('faiss')
from faiss_assignment import AssignmentConfig, FaissAssigner


class StreamTable:
    def __init__(self, rows, *, point_reads=False):
        self.rows = rows
        self.point_reads = point_reads
        self.queries = []
        self.reads = []
        self.pulled = 0

    def query_entities(self, query_filter, **kwargs):
        self.queries.append((query_filter, kwargs))

        def walk():
            rows = self.rows() if callable(self.rows) else iter(self.rows)
            assert iter(rows) is rows
            for row in rows:
                self.pulled += 1
                yield row

        return walk()

    def get_entity(self, *, partition_key, row_key):
        self.reads.append((partition_key, row_key))
        assert self.point_reads, 'stream mode must never point-read embeddings'
        for row in self.rows:
            if (row['PartitionKey'], row['RowKey']) == (partition_key, row_key):
                return row
        raise ResourceNotFoundError('missing')


def face(key, *, user='u', **extra):
    return dict(PartitionKey=user, RowKey=key, personId='person',
                alignmentMethod='5pt', embeddingVersion='v1', **extra)


def embedding(key, *, user='u', value=(1, 0)):
    return dict(PartitionKey=user, RowKey=key, embedding=list(value))


def assigner(faces, embeddings, *, stream=True):
    return FaissAssigner(
        face_table=faces, person_table=StreamTable(iter(())),
        embedding_table=embeddings, metadata_callback=lambda *args: None,
        clusterable=lambda row: row.get('clusterable', True),
        eligible=lambda row: row.get('eligible', True),
        tier=lambda row: row['alignmentMethod'],
        version=lambda row: row['embeddingVersion'], lease=lambda user: nullcontext(),
        config=AssignmentConfig(cold_stream_embeddings=stream))


def test_large_cold_stream_is_incremental_and_has_no_point_reads():
    count = 3000
    faces = StreamTable(lambda: (face(f'{i:06d}') for i in range(count)))
    embeddings = StreamTable(lambda: (embedding(f'{i:06d}') for i in range(count)))
    rows = assigner(faces, embeddings)._cold_rows('u')
    assert inspect.isgenerator(rows) and iter(rows) is rows
    for i, row in enumerate(rows):
        assert row['faceId'] == f'{i:06d}'
        assert row['personId'] == 'person'
        assert row['tier'] == '5pt' and row['embeddingVersion'] == 'v1'
        np.testing.assert_allclose(row['embedding'], [1, 0])
        # Neither partition may be prefetched/materialized by the adapter.
        assert faces.pulled == embeddings.pulled == i + 1
    assert faces.pulled == count
    assert faces.queries == [("PartitionKey eq 'u'", {})]
    assert embeddings.queries == [("PartitionKey eq 'u'", {
        'select': ['RowKey', 'PartitionKey', 'embedding']})]
    assert not faces.reads and not embeddings.reads


def test_missing_orphan_and_dropped_faces_keep_merge_aligned():
    source = [face('a'), face('b'), face('c', rejected=True),
              face('d'), face('e'), face('f', clusterable=False),
              face('g', eligible=False), face('h'), face('i')]
    source[4]['personId'] = ''
    faces = StreamTable(iter(source))
    embeddings = StreamTable(iter(embedding(key) for key in ['0', 'b', 'c', 'e', 'f', 'g', 'h']))
    rows = list(assigner(faces, embeddings)._cold_rows('u'))
    assert [row['faceId'] for row in rows] == ['b', 'h']
    assert embeddings.pulled == 7 and not embeddings.reads


@pytest.mark.parametrize('empty', [None, '', [], '[]'])
def test_empty_inline_values_use_matching_stream_row(empty):
    faces = StreamTable(iter([face('a', embedding=empty)]))
    embeddings = StreamTable(iter([embedding('a', value=(3, 4))]))
    row, = assigner(faces, embeddings)._cold_rows('u')
    np.testing.assert_allclose(row['embedding'], [.6, .8])
    assert not embeddings.reads


def test_inline_preferred_and_invalid_inline_does_not_fallback():
    faces = StreamTable(iter([face('a', embedding='[0, 2]'),
                              face('b', embedding='not json'),
                              face('c', embedding=[True, 0]), face('d')]))
    embeddings = StreamTable(iter(embedding(key) for key in ['a', 'b', 'c', 'd']))
    rows = list(assigner(faces, embeddings)._cold_rows('u'))
    assert [row['faceId'] for row in rows] == ['a', 'd']
    np.testing.assert_allclose(rows[0]['embedding'], [0, 1])
    assert not embeddings.reads


def test_invalid_dedicated_vectors_are_skipped():
    rows = [embedding('a', value=(0, 0)), embedding('b', value=(float('nan'), 0)),
            embedding('c'), embedding('d')]
    rows[2]['embedding'] = '{"bad": 1}'
    faces = StreamTable(iter(face(key) for key in ['a', 'b', 'c', 'd']))
    assert [row['faceId'] for row in assigner(faces, StreamTable(iter(rows)))._cold_rows('u')] == ['d']


def test_escaped_partition_filter_and_literal_keys():
    user = "library'o"
    keys = ["a'quoted", 'b%20', 'c-é']
    faces = StreamTable(iter(face(key, user=user) for key in keys))
    embeddings = StreamTable(iter(embedding(key, user=user) for key in keys))
    assert [row['faceId'] for row in assigner(faces, embeddings)._cold_rows(user)] == keys
    assert faces.queries[0][0] == embeddings.queries[0][0] == "PartitionKey eq 'library''o'"


def test_empty_faces_never_open_embedding_query():
    def forbidden():
        pytest.fail('empty face partition must not scan embeddings')

    faces, embeddings = StreamTable(iter(())), StreamTable(forbidden)
    assert list(assigner(faces, embeddings)._cold_rows('u')) == []
    assert not embeddings.queries and not embeddings.reads


def test_trailing_embeddings_are_not_exhausted():
    def dedicated():
        yield embedding('a')
        raise AssertionError('no need to read beyond last face')

    faces, embeddings = StreamTable(iter([face('a')])), StreamTable(dedicated)
    assert len(list(assigner(faces, embeddings)._cold_rows('u'))) == 1
    assert embeddings.pulled == 1


@pytest.mark.parametrize('source', ['faces', 'embeddings'])
@pytest.mark.parametrize('failure', ['call', 'iteration'])
def test_network_errors_propagate(source, failure):
    error = HttpResponseError('network failure')

    def broken():
        yield face('a') if source == 'faces' else embedding('a')
        raise error

    faces = StreamTable(iter([face('a'), face('z')]))
    embeddings = StreamTable(iter([embedding('a'), embedding('z')]))
    table = faces if source == 'faces' else embeddings
    if failure == 'iteration':
        table.rows = broken
    else:
        def fail_query(*args, **kwargs):
            raise error
        table.query_entities = fail_query
    with pytest.raises(HttpResponseError) as caught:
        list(assigner(faces, embeddings)._cold_rows('u'))
    assert caught.value is error
    assert not embeddings.reads


@pytest.mark.parametrize('source', ['faces', 'embeddings'])
@pytest.mark.parametrize('bad', ['descending', 'duplicate', 'partition', 'missing', 'number', 'empty'])
def test_consumed_stream_keys_fail_closed(source, bad):
    make = face if source == 'faces' else embedding
    rows = [make('b'), make('c')]
    if bad in ('descending', 'duplicate'):
        rows[1]['RowKey'] = 'a' if bad == 'descending' else 'b'
    elif bad == 'partition':
        rows[1]['PartitionKey'] = 'other'
    elif bad == 'missing':
        del rows[1]['RowKey']
    else:
        rows[1]['RowKey'] = 42 if bad == 'number' else ''
    # Dropped metadata still participates in ordered identity validation.
    if source == 'faces':
        rows[1]['personId'] = ''
    faces = StreamTable(iter(rows if source == 'faces' else [face('b'), face('z')]))
    embeddings = StreamTable(iter(rows if source == 'embeddings' else [embedding('b'), embedding('z')]))
    with pytest.raises(ValueError, match='stream'):
        list(assigner(faces, embeddings)._cold_rows('u'))
    assert not embeddings.reads


@pytest.mark.parametrize('stream', [False, True])
def test_no_embedding_table_uses_inline_only(stream):
    faces = StreamTable(iter([face('a', embedding=[0, 1]), face('b')]))
    row, = assigner(faces, None, stream=stream)._cold_rows('u')
    assert row['faceId'] == 'a'
    np.testing.assert_allclose(row['embedding'], [0, 1])


def test_default_preserves_unsorted_fake_and_point_read_path():
    assert AssignmentConfig().cold_stream_embeddings is False
    faces = StreamTable(iter([face('b'), face('a', embedding=[0, 1]), face('c')]))
    embeddings = StreamTable([embedding('b')], point_reads=True)
    rows = list(assigner(faces, embeddings, stream=False)._cold_rows('u'))
    assert [row['faceId'] for row in rows] == ['b', 'a']
    assert embeddings.reads == [('u', 'b'), ('u', 'c')]
    assert not embeddings.queries


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_stream_config_requires_actual_bool(value):
    with pytest.raises(ValueError, match='cold_stream_embeddings must be a bool'):
        AssignmentConfig(cold_stream_embeddings=value)