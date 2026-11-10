import json

import person_groups as pg


def row(rk, group='', shards=None):
    r = {'RowKey': rk}
    if group:
        r['groupId'] = group
    if shards is not None:
        r['shardIds'] = json.dumps(shards)
    return r


def test_reads_recorded_ids_without_the_persons_own():
    assert pg.shard_ids(row('A', shards=['S2', 'S1', 'A'])) == ['S1', 'S2']
    assert pg.shard_ids(row('A')) == [] and pg.shard_ids({'RowKey': 'A', 'shardIds': 'not json'}) == []


def test_missing_pointers_lists_only_groups_that_lack_an_overflow_row():
    rows = [row('A', shards=['S1']), row('S1', 'A'), row('S2', 'A'), row('B', shards=['T']), row('T', 'B'), row('C')]
    assert pg.missing_pointers(rows) == {'A': json.dumps(['S1', 'S2'], separators=(',', ':'))}


def test_missing_pointers_keeps_recorded_ids_and_skips_a_vanished_first_person():
    rows = [row('A', shards=['OLD']), row('S1', 'A'), row('Z1', 'GONE')]
    assert pg.missing_pointers(rows) == {'A': json.dumps(['OLD', 'S1'], separators=(',', ':'))}
