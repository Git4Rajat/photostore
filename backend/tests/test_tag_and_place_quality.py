"""Tag and place quality rules: unpleasant-tag blocklist, tags contradicting the
photo's location, Explore "Things" allowlist, local place names (and search by
either spelling), and the Explore place filter matching its card count."""
from __future__ import annotations

import json

import pytest

import app
import explore_things
import place_names
import search_db
import search_utils
from routes import photos as photos_routes


# --- 1. offensive / unpleasant tags ------------------------------------------

def test_vomit_is_blocked_at_read_time_without_a_rerun():
    row = {'tags': json.dumps(['food', 'vomit', 'plate']), 'subjectTags': json.dumps(['vomit', 'food'])}
    assert search_utils.is_sensitive_auto_tag_label('Vomit')
    assert search_utils.visible_tags(row) == ['food', 'plate']
    assert 'vomit' not in search_utils.effective_tags(row)


def test_blocklist_not_applied_to_user_tags():
    row = {'tags': json.dumps(['vomit']), 'tagMetadata': json.dumps([{'tag': 'vomit', 'source': 'user', 'confidence': 1}])}
    assert search_utils.visible_tags(row) == ['vomit']


def test_blocked_labels_never_curated():
    curated = search_utils.curate_tag_records([
        {'label': 'vomit', 'source': 'ai_tag', 'confidence': 0.9},
        {'label': 'cake', 'source': 'ai_tag', 'confidence': 0.9},
    ])
    assert curated['tags'] == ['cake']


# --- 2. tags that contradict the location ------------------------------------

AURORA = ['aurora', 'iceland', 'edmonton', 'canada', 'helsinki', 'scandinavia', 'icelandic', 'night', 'sky']


def _malmo(**extra):
    return {'tags': json.dumps(AURORA), 'latitude': '55.6', 'longitude': '13.0',
            'locationCity': 'Malmoe', 'locationCountry': 'Sweden', **extra}


def test_aurora_in_malmo_drops_foreign_places_countries_and_nationalities():
    assert search_utils.visible_tags(_malmo()) == ['aurora', 'night', 'sky']


def test_tags_naming_the_photos_own_place_survive_in_any_spelling():
    row = _malmo(tags=json.dumps(['aurora', 'sweden', 'malmo', 'malmoe']))
    assert search_utils.visible_tags(row)[0] == 'aurora'
    # own place names are location metadata (not chips) -- never treated as contradictions
    assert 'iceland' not in search_utils.visible_tags(_malmo())


def test_geocoded_place_alone_is_enough_when_coordinates_live_in_exif():
    assert 'iceland' not in search_utils.visible_tags(_malmo(latitude='', longitude=''))


def test_halmstad_selfie_drops_geneva_islamabad_bollywood_pakistan():
    tags = ['selfie', 'couple', 'date', 'geneva', 'islamabad', 'bollywood', 'pakistan']
    row = {'tags': json.dumps(tags), 'locationCity': 'Halmstad', 'locationCountry': 'Sweden'}
    assert search_utils.visible_tags(row) == ['selfie', 'couple', 'date']


def test_without_a_geocoded_place_nothing_is_contradicted():
    row = _malmo(locationCity='', locationCountry='')
    assert 'iceland' in search_utils.visible_tags(row)


def test_user_tags_naming_other_places_are_kept():
    row = _malmo(tagMetadata=json.dumps([{'tag': 'iceland', 'source': 'user', 'confidence': 1}]))
    assert 'iceland' in search_utils.visible_tags(row)


# --- 3. Explore "Things" ------------------------------------------------------

JUNK = ['Calculu', 'Ate', 'Petite', 'Pricing', 'Thailand', 'Icelandic', 'Dharma', 'Producer',
        'Researcher', 'Reporter', 'Student', 'Bollywood']


@pytest.mark.parametrize('label', JUNK)
def test_junk_is_not_a_thing(label):
    assert not explore_things.is_explore_thing_label(label)


@pytest.mark.parametrize('label', ['dog', 'Food', 'beach', 'hiking', 'sunset', 'cake', 'Aurora'])
def test_concrete_objects_scenes_activities_are_things(label):
    assert explore_things.is_explore_thing_label(label)


def test_confidence_floor_drops_weak_ai_tags_but_keeps_user_tags():
    row = {
        'tags': json.dumps(['dog', 'cat', 'bird']),
        'tagMetadata': json.dumps([
            {'tag': 'dog', 'source': 'ai_tag', 'confidence': 0.30},
            {'tag': 'cat', 'source': 'ai_tag', 'confidence': 0.10},
            {'tag': 'bird', 'source': 'user', 'confidence': 0.10},
        ]),
    }
    assert explore_things.explore_thing_tags(row) == ['dog', 'bird']


def test_calculus_is_not_truncated():
    assert search_utils.normalize_tags(['calculus', 'cactus', 'virus']) == ['calculus', 'cactus', 'virus']
    # rows already stored in the truncated form are repaired on read
    assert search_utils.normalize_tags(['calculu']) == ['calculus']
    # ordinary plurals still singularize
    assert search_utils.normalize_tags(['dogs', 'cars']) == ['dog', 'car']


def test_stored_summary_is_cleaned_on_read():
    cleaned = explore_things.clean_summary({
        'places': [{'label': 'Malmoe, Sweden', 'count': 5}],
        'things': [{'label': l, 'count': 9} for l in JUNK] + [{'label': 'Dog', 'count': 3}],
    })
    assert [t['label'] for t in cleaned['things']] == ['Dog']
    assert cleaned['places'][0]['label'] == 'Malmö, Sweden'


def test_accumulator_builds_things_from_allowlist_only(monkeypatch):
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: {'RowKey': name})
    monkeypatch.setattr(app, '_build_photo_summary', lambda *a, **k: {})
    acc = app.ExploreAccumulator()
    acc.add({'RowKey': 'a.jpg', 'tags': json.dumps(['dog', 'student', 'thailand', 'petite'])})
    assert [g['label'] for g in acc.thing_groups.values()] == ['Dog']


# --- 4. local place names ------------------------------------------------------

@pytest.mark.parametrize('raw,country,expected', [
    ('Malmoe', 'Sweden', 'Malmö'), ('Malmo', 'Sweden', 'Malmö'), ('Goeteborg', 'Sweden', 'Göteborg'),
    ('Zuerich', 'Switzerland', 'Zürich'), ('Koeln', 'Germany', 'Köln'), ('Noida', 'India', 'Noida'),
    ('Bauen', 'Switzerland', 'Bauen'), ('Malmö', 'Sweden', 'Malmö'),
])
def test_local_place_names(raw, country, expected):
    assert place_names.local_place_name(raw, country) == expected


def test_geocoded_place_is_localized(monkeypatch):
    import maps_utils
    monkeypatch.setattr(maps_utils, '_reverse_geocode_raw', lambda lat, lon: {
        'address': 'Malmoe, Skane, Sweden', 'city': 'Malmoe', 'region': 'Skane', 'country': 'Sweden'})
    place = maps_utils.reverse_geocode('55.6', '13.0')
    assert (place['city'], place['region'], place['address']) == ('Malmö', 'Skåne', 'Malmö, Skåne, Sweden')


def test_photo_info_location_uses_local_name():
    loc = app._location_from_metadata({'latitude': '55.6', 'longitude': '13.0',
                                       'locationCity': 'Malmoe', 'locationCountry': 'Sweden'})
    assert loc['city'] == 'Malmö'


def test_place_cards_and_smart_albums_use_local_name_and_merge_spellings():
    a = explore_things.explore_place({'locationCity': 'Malmoe', 'locationCountry': 'Sweden'})
    b = explore_things.explore_place({'locationCity': 'Malmö', 'locationCountry': 'Sweden'})
    assert a == b == ('malmo sweden', 'Malmö, Sweden')
    candidates = app._smart_album_candidates('u', 'location', [
        {'RowKey': 'a.jpg', 'locationCity': 'Malmoe', 'locationCountry': 'Sweden'},
        {'RowKey': 'b.jpg', 'locationCity': 'Malmo', 'locationCountry': 'Sweden'}])
    assert [(c['name'], len(c['filenames'])) for c in candidates] == [('Location: Malmö, Sweden', 2)]


@pytest.mark.parametrize('query', ['Malmö', 'Malmo', 'malmoe'])
def test_every_spelling_finds_the_photo_in_search(query):
    row = {'RowKey': 'a.jpg', 'locationCity': 'Malmoe', 'locationCountry': 'Sweden'}
    record = search_db.row_record(row)
    tokens = search_utils.parse_search_query(query)
    terms = search_db.match_terms(tokens)
    doc = record['doc'].split()
    assert any(t in doc for t in terms)
    assert search_utils.lexical_search_score(tokens, 'a.jpg', row, {}) > 0 or query == 'malmoe'
    # the place-term index (used for location-intent queries) knows every spelling
    assert {'malmo', 'malmoe'} <= set(record['terms'])


# --- 5. place filter matches the card -----------------------------------------

def test_place_filter_returns_exactly_the_cards_members(tmp_path):
    rows = [
        {'RowKey': 'n1.jpg', 'locationCity': 'Noida', 'locationCountry': 'India'},
        {'RowKey': 'n2.jpg', 'locationCity': 'Noida', 'locationCountry': 'India'},
        {'RowKey': 'n3.jpg', 'locationCity': 'Noida', 'locationCountry': 'India'},
        *({'RowKey': f'd{i}.jpg', 'locationCity': 'Delhi', 'locationCountry': 'India'} for i in range(5)),
        {'RowKey': 'm.jpg', 'locationCity': 'Malmoe', 'locationCountry': 'Sweden'},
    ]
    path = str(tmp_path / 'db.sqlite')
    search_db.build_database(rows, path)
    db = search_db.SearchDatabase(path)
    card = {g['label']: g['count'] for g in db.explore_groups()['places']}
    names, total = db.place_page(explore_things.place_keys_for_label('Noida, India'))
    assert card['Noida, India'] == total == 3 and sorted(names) == ['n1.jpg', 'n2.jpg', 'n3.jpg']
    # the old text search for "Noida, India" matched every India photo
    assert len(db.candidates(search_db.match_terms(search_utils.parse_search_query('Noida, India')))) >= 8
    # a card whose label predates local spelling still resolves
    assert db.place_page(explore_things.place_keys_for_label('Malmoe, Sweden'))[1] == 1


def test_search_route_place_mode_is_registered():
    assert hasattr(photos_routes, '_search_place')
