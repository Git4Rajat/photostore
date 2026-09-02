import os
import threading
from typing import Dict

import requests

try:
    from geopy.geocoders import Nominatim
    from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
except Exception:
    Nominatim = None
    GeocoderTimedOut = None
    GeocoderUnavailable = None

try:
    import reverse_geocoder as _offline_geocoder
except Exception:
    _offline_geocoder = None

try:
    import pycountry
except Exception:
    pycountry = None

_GEOCODER = None

# GeoNames/reverse_geocoder emit a couple of user-assigned country codes that
# aren't in the official ISO 3166-1 list pycountry ships, so they'd otherwise
# resolve to ''.
_COUNTRY_CODE_OVERRIDES = {
    'XK': 'Kosovo',
}

# reverse_geocoder's own RGeocoder is a bare check-then-set singleton (not
# thread-safe): two gunicorn threads racing to build the K-D tree on first
# use would both do the ~1s build redundantly. This lock makes that build
# happen exactly once regardless of whether prewarm_offline_geocoder() was
# called ahead of time or a request just happens to be first.
_offline_geocoder_lock = threading.Lock()
_offline_geocoder_ready = False


def _get_geocoder():
    global _GEOCODER
    if _GEOCODER is not None:
        return _GEOCODER
    if Nominatim is None:
        return None
    user_agent = os.getenv('GEOCODER_USER_AGENT', 'photostore-backend')
    timeout = int(os.getenv('GEOCODER_TIMEOUT', '8'))
    _GEOCODER = Nominatim(user_agent=user_agent, timeout=timeout)
    return _GEOCODER


def _country_name(country_code: str) -> str:
    code = (country_code or '').strip().upper()
    if not code:
        return ''
    if code in _COUNTRY_CODE_OVERRIDES:
        return _COUNTRY_CODE_OVERRIDES[code]
    if pycountry is not None:
        country = pycountry.countries.get(alpha_2=code)
        if country is not None:
            return country.name
    return code


def prewarm_offline_geocoder() -> None:
    """Builds the offline city K-D tree once, up front. Cheap (~1s) but
    avoids every request/ipwork thread racing to build it concurrently on
    first use, and removes first-call latency from the request path. Safe to
    call from multiple threads/processes and multiple times (idempotent)."""
    global _offline_geocoder_ready
    if _offline_geocoder is None or _offline_geocoder_ready:
        return
    with _offline_geocoder_lock:
        if _offline_geocoder_ready:
            return
        _offline_geocoder.search((0.0, 0.0), mode=1, verbose=False)
        _offline_geocoder_ready = True


def _reverse_geocode_offline(latitude: str, longitude: str) -> Dict[str, str]:
    if _offline_geocoder is None:
        return {}
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return {}
    prewarm_offline_geocoder()
    try:
        result = _offline_geocoder.search((lat, lon), mode=1, verbose=False)[0]
    except Exception:
        return {}

    city = result.get('name') or result.get('admin2') or result.get('admin1') or ''
    admin1 = result.get('admin1') or ''
    country = _country_name(result.get('cc', ''))
    address_bits = [city, admin1, country]
    return {
        'address': ', '.join(dict.fromkeys(bit for bit in address_bits if bit)),
        'city': city,
        'country': country,
    }


def reverse_geocode(latitude: str, longitude: str) -> Dict[str, str]:
    # Offline (local K-D tree over a GeoNames city database, no network call)
    # is the default: Nominatim's public API caps at 1 req/sec globally,
    # which every concurrent ipworker thread/replica and backend replica was
    # blowing through with zero coordination, and failures (incl. 429/403)
    # were silently swallowed as an empty result. City/country-level
    # precision is all any current feature actually depends on (smart
    # albums, search, the photo info panel all already prefer city/country
    # over the full street address). 'nominatim'/'photon' remain available
    # as opt-in fallbacks via GEOCODER_MODE for anyone who needs street-level
    # addresses badly enough to deal with the rate limit.
    mode = os.getenv('GEOCODER_MODE', 'offline').lower().strip()
    if mode in ('', 'disabled', 'off', 'none'):
        return {}
    if mode == 'offline':
        return _reverse_geocode_offline(latitude, longitude)
    if mode == 'photon':
        return _reverse_geocode_photon(latitude, longitude)
    if mode not in ('nominatim', 'osm'):
        return {}
    geocoder = _get_geocoder()
    if geocoder is None:
        return {}
    try:
        location = geocoder.reverse(f"{latitude}, {longitude}", language='en')
    except (GeocoderTimedOut, GeocoderUnavailable, Exception):
        return {}
    if not location:
        return {}

    address = location.raw.get('address', {}) if hasattr(location, 'raw') else {}
    city = address.get('city') or address.get('town') or address.get('village') or address.get('state') or ''
    country = address.get('country', '')
    return {
        'address': location.address or '',
        'city': city,
        'country': country,
    }


def _reverse_geocode_photon(latitude: str, longitude: str) -> Dict[str, str]:
    endpoint = os.getenv('PHOTON_ENDPOINT', 'https://photon.komoot.io').rstrip('/')
    timeout = int(os.getenv('GEOCODER_TIMEOUT', '8'))
    try:
        response = requests.get(
            f'{endpoint}/reverse',
            params={'lat': latitude, 'lon': longitude, 'lang': 'en'},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}

    features = payload.get('features') or []
    if not features:
        return {}
    props = features[0].get('properties') or {}
    city = props.get('city') or props.get('county') or props.get('state') or ''
    country = props.get('country') or ''
    address_bits = [
        props.get('name') or '',
        props.get('street') or '',
        city,
        country,
    ]
    return {
        'address': ', '.join(dict.fromkeys([bit for bit in address_bits if bit])),
        'city': city,
        'country': country,
    }
