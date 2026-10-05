"""Naming a person confirms all their faces: parallel reads, batched writes."""
import app
from routes import people
from tests.fakes import FakeTable


def _seed(n):
    table = FakeTable()
    for i in range(n):
        table.upsert_entity({'PartitionKey': 'u1', 'RowKey': f'f{i}', 'filename': f'p{i % 7}.jpg', 'confidence': 0.4,
                             'rejected': True, 'rejectedReason': 'x', 'reviewStatus': 'suspicious'})
    return table


def test_confirms_every_face_and_reports_affected_files(monkeypatch):
    table = _seed(250)
    monkeypatch.setattr(app, 'face_table_client', table)
    files = people._confirm_faces_for_person('u1', [f'f{i}' for i in range(250)] + ['missing'])
    assert files == {f'p{i}.jpg' for i in range(7)}
    row = table.get_entity('u1', 'f10')
    assert row['confirmedByUser'] is True and row['reviewStatus'] == 'confirmed'
    assert row['rejected'] is False and row['confidence'] == 1.0 and 'rejectedReason' not in row


def test_writes_are_batched_into_transactions_of_at_most_100(monkeypatch):
    table = _seed(250)
    monkeypatch.setattr(app, 'face_table_client', table)
    people._confirm_faces_for_person('u1', [f'f{i}' for i in range(250)])
    sizes = sorted(len(ops) for ops in table.submit_transaction_calls)
    assert sizes == [50, 100, 100]


def test_falls_back_to_single_upserts_when_a_transaction_is_rejected(monkeypatch):
    table = _seed(30)

    def boom(ops):
        raise RuntimeError('rejected')

    table.submit_transaction = boom
    monkeypatch.setattr(app, 'face_table_client', table)
    people._confirm_faces_for_person('u1', [f'f{i}' for i in range(30)])
    assert all(table.get_entity('u1', f'f{i}')['confirmedByUser'] for i in range(30))
