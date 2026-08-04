import os
from typing import Dict

import requests

try:
    from geopy.geocoders import Nominatim
    from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
except Exception:
    Nominatim = None
    GeocoderTimedOut = None
    GeocoderUnavailable = None

_GEOCODER = None


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


def reverse_geocode(latitude: str, longitude: str) -> Dict[str, str]:
    mode = os.getenv('GEOCODER_MODE', 'nominatim').lower().strip()
    if mode in ('', 'disabled', 'off', 'none'):
        return {}
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
