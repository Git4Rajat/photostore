"""Regression tests for reverse_geocode's 'region' (admin1/state) field.

Previously each backend (offline GeoNames K-D tree, Nominatim, Photon)
already computed this value internally but only used it as a fallback for
`city` when the primary name was missing, then discarded it -- so a search
for a region/state name (e.g. "california", "provence") could never match a
photo whose city/country fields don't happen to repeat it, even though the
value was sitting right there in every geocode response.
"""
from __future__ import annotations

import maps_utils


class _FakeOfflineGeocoder:
    def __init__(self, result):
        self._result = result

    def search(self, coords, mode, verbose):
        return [self._result]


def test_offline_geocode_exposes_region_distinct_from_city(monkeypatch):
    monkeypatch.setattr(
        maps_utils, '_offline_geocoder',
        _FakeOfflineGeocoder({'name': 'Palo Alto', 'admin1': 'California', 'admin2': '', 'cc': 'US'}),
    )
    monkeypatch.setattr(maps_utils, 'prewarm_offline_geocoder', lambda: None)

    place = maps_utils._reverse_geocode_offline('37.44', '-122.14')

    assert place['city'] == 'Palo Alto'
    assert place['region'] == 'California'
    assert place['country'] == 'United States'


def test_offline_geocode_omits_region_when_it_duplicates_city_fallback(monkeypatch):
    # When `name`/`admin2` are both empty, city itself falls back to admin1 --
    # reporting the same value again as 'region' would be a redundant, not a
    # new, signal.
    monkeypatch.setattr(
        maps_utils, '_offline_geocoder',
        _FakeOfflineGeocoder({'name': '', 'admin1': 'Yukon', 'admin2': '', 'cc': 'CA'}),
    )
    monkeypatch.setattr(maps_utils, 'prewarm_offline_geocoder', lambda: None)

    place = maps_utils._reverse_geocode_offline('63.0', '-135.0')

    assert place['city'] == 'Yukon'
    assert place['region'] == ''


def test_photon_geocode_exposes_region_distinct_from_city(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {'features': [{'properties': {'city': 'Marseille', 'state': 'Provence-Alpes-Cote-d-Azur', 'country': 'France'}}]}

    monkeypatch.setattr(maps_utils.requests, 'get', lambda *a, **k: FakeResponse())

    place = maps_utils._reverse_geocode_photon('43.3', '5.4')

    assert place['city'] == 'Marseille'
    assert place['region'] == 'Provence-Alpes-Cote-d-Azur'
    assert place['country'] == 'France'
