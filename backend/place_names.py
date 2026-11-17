"""Place-name quality: local spellings for geocoded names, and a small
gazetteer that tells a geographic word from an ordinary tag.

Two problems this solves, both visible in the UI before it existed:

* The offline reverse geocoder (reverse_geocoder's GeoNames cities1000
  extract) ships ASCII-only names, with umlauts/ring letters either written
  as digraphs ("Malmoe", "Goeteborg", "Zuerich") or simply stripped ("Malmo",
  "Orebro"). Both leaked straight into Photo info, Places, smart-album names
  and search chips. `local_place_name` turns them back into the local
  spelling ("Malmö", "Göteborg", "Zürich") -- applied at geocode time for new
  photos AND at read time for rows that already carry the ASCII form, so no
  re-geocoding is needed. Search keeps matching every spelling (see
  `place_name_variants`).

* CLIP's zero-shot vocabulary contains thousands of place names, countries
  and demonyms, and an aurora shot taken in Malmö happily came back tagged
  "iceland", "edmonton", "canada", "helsinki", "scandinavia", "icelandic".
  `is_geographic_label` is the gazetteer behind `search_utils`'s rule that a
  photo with GPS and a reverse-geocoded place drops tags naming any other
  place, country or nationality, and behind Explore's "Things" shelf, which
  never shows a place name at all (that is what "Places" is for).
"""
from __future__ import annotations

import re
import threading
import unicodedata
from typing import Dict, Iterable, List, Optional, Set

try:
    import pycountry
except Exception:  # pragma: no cover - exercised only where the dep is absent
    pycountry = None


def ascii_fold(value: str) -> str:
    return unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode('ascii')


def _normalize(value: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', ascii_fold(value).lower())).strip()


# --- local spellings -----------------------------------------------------------

# GeoNames' ASCII names write umlauts/ring letters as digraphs in a few
# languages where that transliteration is conventional and reversible. Keyed
# by ISO country code; only names that are pure ASCII are rewritten, and only
# for countries whose orthography makes the reverse mapping safe.
_DIGRAPH_RULES: Dict[str, Dict[str, str]] = {
    # Swedish / Finnish: ö ä (ü is not native; 'ue' stays as written).
    'SE': {'oe': 'ö', 'ae': 'ä', 'OE': 'Ö', 'AE': 'Ä', 'Oe': 'Ö', 'Ae': 'Ä'},
    'FI': {'oe': 'ö', 'ae': 'ä', 'OE': 'Ö', 'AE': 'Ä', 'Oe': 'Ö', 'Ae': 'Ä'},
    # Danish / Norwegian: ø æ.
    'DK': {'oe': 'ø', 'ae': 'æ', 'OE': 'Ø', 'AE': 'Æ', 'Oe': 'Ø', 'Ae': 'Æ'},
    'NO': {'oe': 'ø', 'ae': 'æ', 'OE': 'Ø', 'AE': 'Æ', 'Oe': 'Ø', 'Ae': 'Æ'},
    # Icelandic: ö æ (ð/þ are stripped by GeoNames and cannot be recovered).
    'IS': {'oe': 'ö', 'ae': 'æ', 'OE': 'Ö', 'AE': 'Æ', 'Oe': 'Ö', 'Ae': 'Æ'},
    # German-speaking: ö ä ü.
    'DE': {'oe': 'ö', 'ae': 'ä', 'ue': 'ü', 'OE': 'Ö', 'AE': 'Ä', 'UE': 'Ü', 'Oe': 'Ö', 'Ae': 'Ä', 'Ue': 'Ü'},
    'AT': {'oe': 'ö', 'ae': 'ä', 'ue': 'ü', 'OE': 'Ö', 'AE': 'Ä', 'UE': 'Ü', 'Oe': 'Ö', 'Ae': 'Ä', 'Ue': 'Ü'},
    'CH': {'oe': 'ö', 'ae': 'ä', 'ue': 'ü', 'OE': 'Ö', 'AE': 'Ä', 'UE': 'Ü', 'Oe': 'Ö', 'Ae': 'Ä', 'Ue': 'Ü'},
}

# Names in those countries where the digraph is genuine (not an umlaut), so
# the rule must leave them alone.
_DIGRAPH_EXCEPTIONS: Set[str] = {
    'soest', 'coesfeld', 'itzehoe', 'moers', 'voerde', 'oer-erkenschwick', 'laer', 'raesfeld', 'buer',
    'oelde', 'bad oeynhausen', 'oeynhausen', 'oberhausen', 'troisdorf', 'duesseldorf-oberkassel',
}

# Names GeoNames ships with the diacritics simply stripped (also what a
# browser-side geocoder or an older row may have stored). Keyed by ISO
# country code, then the lower-cased ASCII form.
_LOCAL_NAME_OVERRIDES: Dict[str, Dict[str, str]] = {
    'SE': {
        'malmo': 'Malmö', 'goteborg': 'Göteborg', 'vasteras': 'Västerås', 'orebro': 'Örebro',
        'linkoping': 'Linköping', 'jonkoping': 'Jönköping', 'norrkoping': 'Norrköping', 'boras': 'Borås',
        'gavle': 'Gävle', 'vaxjo': 'Växjö', 'lulea': 'Luleå', 'umea': 'Umeå', 'skane': 'Skåne',
        'ostersund': 'Östersund', 'hassleholm': 'Hässleholm', 'angelholm': 'Ängelholm', 'hoganas': 'Höganäs',
        'sodertalje': 'Södertälje', 'nykoping': 'Nyköping', 'skovde': 'Skövde', 'vastervik': 'Västervik',
        'falkoping': 'Falköping', 'harnosand': 'Härnösand', 'ornskoldsvik': 'Örnsköldsvik', 'mjolby': 'Mjölby',
        'taby': 'Täby', 'jarfalla': 'Järfälla', 'pitea': 'Piteå', 'skelleftea': 'Skellefteå',
        'vanersborg': 'Vänersborg', 'amal': 'Åmål', 'are': 'Åre', 'almhult': 'Älmhult', 'ystad': 'Ystad',
        'vastra gotaland': 'Västra Götaland', 'ostergotland': 'Östergötland', 'jamtland': 'Jämtland',
        'vastmanland': 'Västmanland', 'vasterbotten': 'Västerbotten', 'sodermanland': 'Södermanland',
        'varmland': 'Värmland', 'vasternorrland': 'Västernorrland', 'gavleborg': 'Gävleborg',
        'malmo kommun': 'Malmö kommun', 'goteborgs kommun': 'Göteborgs kommun',
    },
    'FI': {'jyvaskyla': 'Jyväskylä', 'hameenlinna': 'Hämeenlinna', 'seinajoki': 'Seinäjoki', 'jarvenpaa': 'Järvenpää'},
    'DK': {'kobenhavn': 'København', 'helsingor': 'Helsingør', 'koge': 'Køge', 'ronne': 'Rønne', 'taastrup': 'Taastrup'},
    'NO': {'tromso': 'Tromsø', 'bodo': 'Bodø', 'alesund': 'Ålesund', 'sandefjord': 'Sandefjord'},
    'IS': {'reykjavik': 'Reykjavík', 'akureyri': 'Akureyri'},
    'DE': {
        'koln': 'Köln', 'dusseldorf': 'Düsseldorf', 'munchen': 'München', 'nurnberg': 'Nürnberg',
        'saarbrucken': 'Saarbrücken', 'lubeck': 'Lübeck', 'osnabruck': 'Osnabrück', 'gottingen': 'Göttingen',
        'tubingen': 'Tübingen', 'wurzburg': 'Würzburg', 'monchengladbach': 'Mönchengladbach', 'furth': 'Fürth',
        'munster': 'Münster', 'baden-wurttemberg': 'Baden-Württemberg', 'thuringen': 'Thüringen',
    },
    'AT': {'polten': 'Pölten', 'sankt polten': 'Sankt Pölten', 'worgl': 'Wörgl'},
    'CH': {'zurich': 'Zürich', 'zurich (kreis 1) / lindenhof': 'Zürich'},
    'BR': {'sao paulo': 'São Paulo', 'brasilia': 'Brasília', 'florianopolis': 'Florianópolis', 'goiania': 'Goiânia'},
    'MX': {'cancun': 'Cancún', 'merida': 'Mérida', 'queretaro': 'Querétaro'},
    'ES': {'malaga': 'Málaga', 'cordoba': 'Córdoba', 'a coruna': 'A Coruña', 'logrono': 'Logroño', 'cadiz': 'Cádiz'},
    'FR': {'orleans': 'Orléans', 'nimes': 'Nîmes', 'besancon': 'Besançon', 'angouleme': 'Angoulême'},
    'CZ': {'plzen': 'Plzeň'},
    'PL': {'krakow': 'Kraków', 'lodz': 'Łódź', 'wroclaw': 'Wrocław', 'poznan': 'Poznań', 'gdansk': 'Gdańsk'},
    'CA': {'montreal': 'Montréal', 'quebec': 'Québec'},
    'PT': {'setubal': 'Setúbal', 'evora': 'Évora'},
    'TR': {'izmir': 'İzmir'},
    'VN': {'da nang': 'Đà Nẵng'},
}

# What the geocoders write as the country field, back to an ISO code, for the
# handful of names pycountry does not resolve on its own.
_COUNTRY_CODE_ALIASES: Dict[str, str] = {
    'kosovo': 'XK', 'russia': 'RU', 'south korea': 'KR', 'korea': 'KR', 'north korea': 'KP', 'iran': 'IR',
    'vietnam': 'VN', 'syria': 'SY', 'laos': 'LA', 'taiwan': 'TW', 'tanzania': 'TZ', 'bolivia': 'BO',
    'venezuela': 'VE', 'moldova': 'MD', 'czech republic': 'CZ', 'turkey': 'TR', 'usa': 'US',
    'united states of america': 'US', 'uk': 'GB', 'great britain': 'GB', 'england': 'GB', 'scotland': 'GB',
    'wales': 'GB', 'holland': 'NL', 'the netherlands': 'NL', 'burma': 'MM', 'ivory coast': 'CI',
    'the gambia': 'GM', 'the bahamas': 'BS', 'cape verde': 'CV', 'brunei': 'BN', 'macau': 'MO',
    'palestine': 'PS', 'micronesia': 'FM', 'swaziland': 'SZ', 'macedonia': 'MK', 'north macedonia': 'MK',
}

_COUNTRY_CODE_CACHE: Dict[str, str] = {}
_COUNTRY_CODE_LOCK = threading.Lock()


def country_code(country: str) -> str:
    """ISO 3166-1 alpha-2 for a country as a geocoder names it ('' if unknown).
    Accepts a code already."""
    text = str(country or '').strip()
    if not text:
        return ''
    if len(text) == 2 and text.isalpha():
        return text.upper()
    key = _normalize(text)
    with _COUNTRY_CODE_LOCK:
        if key in _COUNTRY_CODE_CACHE:
            return _COUNTRY_CODE_CACHE[key]
    code = _COUNTRY_CODE_ALIASES.get(key, '')
    if not code and pycountry is not None:
        try:
            match = pycountry.countries.lookup(text)
            code = str(getattr(match, 'alpha_2', '') or '')
        except Exception:
            code = ''
    with _COUNTRY_CODE_LOCK:
        _COUNTRY_CODE_CACHE[key] = code
    return code


def _apply_digraphs(name: str, rules: Dict[str, str]) -> str:
    out = []
    i = 0
    while i < len(name):
        pair = name[i:i + 2]
        replacement = rules.get(pair)
        if replacement is not None:
            # 'ue' after a vowel is a real diphthong ("Bauen", "Neuenhof",
            # "Feuerthalen"), never an umlaut.
            if pair.lower() == 'ue' and i > 0 and name[i - 1].lower() in 'aeoq':
                out.append(name[i])
                i += 1
                continue
            out.append(replacement)
            i += 2
            continue
        out.append(name[i])
        i += 1
    return ''.join(out)


def local_place_name(name: str, country: str = '') -> str:
    """The local spelling of a geocoded place name: "Malmoe"/"Malmo" -> "Malmö"
    for Sweden, "Zuerich" -> "Zürich" for Switzerland. Names that already
    carry non-ASCII letters, and names in countries without a safe rule, are
    returned unchanged."""
    text = str(name or '').strip()
    if not text or text != ascii_fold(text):
        return text
    code = country_code(country)
    if not code:
        return text
    override = _LOCAL_NAME_OVERRIDES.get(code, {}).get(text.lower())
    if override:
        return override
    rules = _DIGRAPH_RULES.get(code)
    if not rules or text.lower() in _DIGRAPH_EXCEPTIONS:
        return text
    localized = _apply_digraphs(text, rules)
    override = _LOCAL_NAME_OVERRIDES.get(code, {}).get(ascii_fold(localized).lower())
    return override or localized


def localize_place(place: Dict[str, str]) -> Dict[str, str]:
    """A geocode result ({address, city, region, country}) with city/region in
    local spelling and the address rebuilt from them."""
    country = str(place.get('country') or '')
    city = local_place_name(place.get('city', ''), country)
    region = local_place_name(place.get('region', ''), country)
    out = dict(place, city=city, region=region)
    raw_address = str(place.get('address') or '')
    if raw_address:
        address = raw_address
        for before, after in ((place.get('city', ''), city), (place.get('region', ''), region)):
            if before and after and before != after:
                address = address.replace(before, after)
        out['address'] = address
    return out


def place_label(city: str, country: str, region: str = '') -> str:
    """"City, Country" as the UI shows it, in local spelling. Falls back to
    the region when there is no city."""
    city_local = local_place_name(city, country)
    region_local = local_place_name(region, country)
    return ', '.join(part for part in (city_local or region_local, str(country or '').strip()) if part)


def place_name_variants(name: str, country: str = '') -> List[str]:
    """Every spelling a user might type for this place name: as stored, the
    local one, its ASCII fold, and the GeoNames digraph form. Lower-cased,
    de-duplicated, stored spelling first."""
    text = str(name or '').strip()
    if not text:
        return []
    variants = [text]
    local = local_place_name(text, country)
    variants.append(local)
    variants.append(ascii_fold(local))
    code = country_code(country)
    rules = _DIGRAPH_RULES.get(code)
    if rules and local != ascii_fold(local):
        reverse = {v: k.lower() for k, v in rules.items() if k.islower()}
        variants.append(''.join(reverse.get(ch, ch) for ch in local.lower()))
    return list(dict.fromkeys(v.lower() for v in variants if v))


# --- gazetteer -----------------------------------------------------------------

CONTINENTS_AND_REGIONS: Set[str] = {
    'africa', 'america', 'americas', 'antarctica', 'arctic', 'asia', 'australasia', 'europe', 'eurasia',
    'oceania', 'polynesia', 'micronesia', 'melanesia', 'caribbean', 'mediterranean', 'scandinavia',
    'nordic', 'nordics', 'baltic', 'baltics', 'balkan', 'balkans', 'benelux', 'iberia', 'siberia',
    'sahara', 'himalaya', 'himalayas', 'alps', 'andes', 'rockies', 'patagonia', 'amazonia', 'lapland',
    'provence', 'tuscany', 'bavaria', 'catalonia', 'andalusia', 'brittany', 'normandy', 'flanders',
    'cornwall', 'yorkshire', 'kashmir', 'punjab', 'bengal', 'rajasthan', 'kerala', 'goa', 'gujarat',
    'maharashtra', 'karnataka', 'tamilnadu', 'assam', 'sikkim', 'ladakh', 'uttarakhand', 'himachal',
    'bali', 'java', 'sumatra', 'borneo', 'hokkaido', 'okinawa', 'tasmania', 'queensland', 'ontario',
    'quebec', 'alberta', 'yukon', 'nunavut', 'manitoba', 'saskatchewan', 'alaska', 'hawaii', 'california',
    'texas', 'florida', 'nevada', 'arizona', 'colorado', 'oregon', 'washington', 'montana', 'idaho',
    'utah', 'wyoming', 'dakota', 'nebraska', 'kansas', 'oklahoma', 'iowa', 'minnesota', 'wisconsin',
    'michigan', 'illinois', 'indiana', 'ohio', 'kentucky', 'tennessee', 'mississippi', 'alabama',
    'louisiana', 'arkansas', 'missouri', 'carolina', 'virginia', 'maryland', 'delaware', 'pennsylvania',
    'jersey', 'connecticut', 'massachusetts', 'vermont', 'maine', 'skane', 'scania', 'lappland',
    'gotland', 'oland', 'jutland', 'zealand', 'sjaelland', 'fyn', 'finnmark', 'tyrol', 'tirol',
    'silesia', 'bohemia', 'moravia', 'transylvania', 'crimea', 'caucasus', 'anatolia', 'levant',
    'mesopotamia', 'arabia', 'maghreb', 'sahel', 'serengeti', 'kilimanjaro', 'everest', 'fuji',
    'manhattan', 'brooklyn', 'hollywood', 'bollywood',
}

NATIONALITIES: Set[str] = {
    'afghan', 'african', 'albanian', 'algerian', 'american', 'arab', 'arabian', 'arabic', 'argentine',
    'argentinian', 'armenian', 'asian', 'aussie', 'australian', 'austrian', 'azerbaijani', 'bahraini',
    'balinese', 'bangladeshi', 'belarusian', 'belgian', 'bengali', 'bhutanese', 'bolivian', 'bosnian',
    'brazilian', 'british', 'briton', 'bulgarian', 'burmese', 'cambodian', 'cameroonian', 'canadian',
    'caribbean', 'catalan', 'chilean', 'chinese', 'colombian', 'congolese', 'costarican', 'croatian',
    'cuban', 'cypriot', 'czech', 'dane', 'danish', 'dutch', 'ecuadorian', 'egyptian', 'emirati', 'english',
    'estonian', 'ethiopian', 'european', 'fijian', 'filipino', 'finn', 'finnish', 'flemish', 'french',
    'gaelic', 'georgian', 'german', 'germanic', 'ghanaian', 'greek', 'guatemalan', 'haitian', 'hawaiian',
    'hindi', 'hispanic', 'honduran', 'hungarian', 'icelander', 'icelandic', 'indian', 'indonesian', 'iranian',
    'iraqi', 'irish', 'israeli', 'italian', 'jamaican', 'japanese', 'javanese', 'jordanian', 'kazakh',
    'kenyan', 'korean', 'kurdish', 'kuwaiti', 'lao', 'laotian', 'latin', 'latino', 'latvian', 'lebanese',
    'libyan', 'lithuanian', 'macedonian', 'malagasy', 'malay', 'malaysian', 'maltese', 'maori', 'mexican',
    'moldovan', 'mongolian', 'moroccan', 'nepalese', 'nepali', 'nigerian', 'nordic', 'norse', 'norwegian',
    'omani', 'pakistani', 'palestinian', 'panamanian', 'paraguayan', 'persian', 'peruvian', 'philippine',
    'polish', 'polynesian', 'portuguese', 'punjabi', 'qatari', 'romanian', 'russian', 'rwandan', 'saudi',
    'scandinavian', 'scot', 'scottish', 'senegalese', 'serbian', 'siamese', 'singaporean', 'slovak',
    'slovakian', 'slovene', 'slovenian', 'somali', 'spaniard', 'spanish', 'srilankan', 'sudanese',
    'swede', 'swedish', 'swiss', 'syrian', 'tahitian', 'taiwanese', 'tamil', 'tanzanian', 'thai',
    'tibetan', 'tunisian', 'turk', 'turkish', 'ugandan', 'ukrainian', 'uruguayan', 'uzbek', 'venezuelan',
    'vietnamese', 'welsh', 'yemeni', 'zambian', 'zimbabwean',
}

# Countries and country aliases as single words (tags are single words).
COUNTRY_ALIASES: Set[str] = {
    'usa', 'america', 'uk', 'britain', 'england', 'scotland', 'wales', 'holland', 'netherlands',
    'russia', 'korea', 'iran', 'vietnam', 'syria', 'laos', 'czechia', 'taiwan', 'tanzania', 'bolivia',
    'venezuela', 'moldova', 'macedonia', 'turkey', 'turkiye', 'burma', 'myanmar', 'emirates', 'palestine',
    'kosovo', 'tibet', 'greenland', 'faroe', 'bahamas', 'philippines', 'maldives', 'seychelles',
    'mauritius', 'fiji', 'samoa', 'tonga', 'vatican', 'monaco', 'andorra', 'liechtenstein', 'luxembourg',
    'malta', 'cyprus', 'iceland', 'ireland', 'norway', 'sweden', 'finland', 'denmark', 'estonia',
    'latvia', 'lithuania', 'poland', 'germany', 'austria', 'switzerland', 'france', 'spain', 'portugal',
    'italy', 'greece', 'croatia', 'slovenia', 'slovakia', 'hungary', 'romania', 'bulgaria', 'serbia',
    'bosnia', 'montenegro', 'albania', 'belarus', 'ukraine', 'georgia', 'armenia', 'azerbaijan',
    'kazakhstan', 'uzbekistan', 'mongolia', 'china', 'japan', 'india', 'pakistan', 'bangladesh', 'nepal',
    'bhutan', 'srilanka', 'thailand', 'cambodia', 'malaysia', 'singapore', 'indonesia', 'australia',
    'canada', 'mexico', 'brazil', 'argentina', 'chile', 'peru', 'colombia', 'ecuador', 'uruguay',
    'paraguay', 'cuba', 'jamaica', 'haiti', 'panama', 'egypt', 'morocco', 'tunisia', 'algeria', 'libya',
    'sudan', 'ethiopia', 'kenya', 'uganda', 'rwanda', 'nigeria', 'ghana', 'senegal', 'cameroon',
    'congo', 'angola', 'zambia', 'zimbabwe', 'botswana', 'namibia', 'mozambique', 'madagascar',
    'israel', 'jordan', 'lebanon', 'iraq', 'kuwait', 'qatar', 'bahrain', 'oman', 'yemen', 'afghanistan',
}

# Cities a photo tagger plausibly emits: capitals and large, well-known cities
# (single words only, as tags are). The vocabulary shares many of these.
CITIES: Set[str] = {
    'abuja', 'accra', 'adelaide', 'ahmedabad', 'aleppo', 'alexandria', 'algiers', 'amman', 'amsterdam',
    'anchorage', 'ankara', 'antwerp', 'athens', 'atlanta', 'auckland', 'austin', 'baghdad', 'baku',
    'baltimore', 'bangalore', 'bengaluru', 'bangkok', 'barcelona', 'basel', 'beijing', 'beirut',
    'belfast', 'belgrade', 'bergen', 'berlin', 'bern', 'bilbao', 'birmingham', 'bogota', 'bologna',
    'bombay', 'bonn', 'bordeaux', 'boston', 'bratislava', 'bremen', 'brisbane', 'bristol', 'brussels',
    'bucharest', 'budapest', 'buffalo', 'cairo', 'calcutta', 'calgary', 'cambridge', 'canberra',
    'cancun', 'canterbury', 'caracas', 'cardiff', 'casablanca', 'chandigarh', 'charleston',
    'charlotte', 'chennai', 'chicago', 'christchurch', 'cincinnati', 'cleveland', 'cologne', 'colombo',
    'columbus', 'copenhagen', 'cordoba', 'cork', 'coventry', 'dakar', 'dallas', 'damascus', 'darwin',
    'delhi', 'denver', 'detroit', 'dhaka', 'doha', 'dortmund', 'dresden', 'dubai', 'dublin', 'dundee',
    'durban', 'dusseldorf', 'edinburgh', 'edmonton', 'florence', 'frankfurt', 'fukuoka', 'geneva',
    'genoa', 'glasgow', 'gothenburg', 'goteborg', 'granada', 'guadalajara', 'guangzhou', 'gurgaon',
    'gurugram', 'hamburg', 'hanoi', 'hannover', 'harare', 'havana', 'helsinki', 'helsingborg',
    'hiroshima', 'hobart', 'honolulu', 'houston', 'hyderabad', 'indianapolis', 'innsbruck', 'islamabad',
    'istanbul', 'izmir', 'jaipur', 'jakarta', 'jerusalem', 'johannesburg', 'kabul', 'kampala',
    'kanpur', 'karachi', 'kathmandu', 'kiev', 'kingston', 'kochi', 'kolkata', 'krakow', 'kuala',
    'kyiv', 'kyoto', 'lagos', 'lahore', 'lausanne', 'leeds', 'leipzig', 'lima', 'lisbon', 'liverpool',
    'ljubljana', 'london', 'lucknow', 'lund', 'luxembourg', 'lyon', 'macau', 'madrid', 'malaga',
    'malmo', 'manchester', 'manila', 'marrakech', 'marseille', 'melbourne', 'memphis', 'miami', 'milan',
    'milwaukee', 'minneapolis', 'minsk', 'montreal', 'moscow', 'mumbai', 'munich', 'muscat', 'nagoya',
    'nairobi', 'nantes', 'naples', 'nashville', 'newcastle', 'nice', 'noida', 'nottingham', 'nuremberg',
    'oakland', 'odessa', 'orlando', 'osaka', 'oslo', 'ottawa', 'oxford', 'palermo', 'paris', 'perth',
    'philadelphia', 'phoenix', 'pittsburgh', 'porto', 'portland', 'prague', 'pune', 'quebec', 'quito',
    'rabat', 'reykjavik', 'riga', 'rio', 'riyadh', 'rome', 'rotterdam', 'sacramento', 'salzburg',
    'santiago', 'sapporo', 'sarajevo', 'seattle', 'seoul', 'seville', 'shanghai', 'sheffield',
    'shenzhen', 'singapore', 'sofia', 'stockholm', 'strasbourg', 'stuttgart', 'sydney', 'taipei',
    'tallinn', 'tampa', 'tbilisi', 'tehran', 'tokyo', 'toronto', 'toulouse', 'tripoli', 'tucson',
    'tunis', 'turin', 'uppsala', 'valencia', 'vancouver', 'venice', 'verona', 'vienna', 'vilnius',
    'warsaw', 'wellington', 'winnipeg', 'yerevan', 'yokohama', 'zagreb', 'zurich',
}

_GAZETTEER: Optional[Set[str]] = None
_GAZETTEER_LOCK = threading.Lock()


def _country_names() -> Set[str]:
    names: Set[str] = set()
    if pycountry is None:
        return names
    for country in pycountry.countries:
        for attr in ('name', 'common_name'):
            value = getattr(country, attr, None)
            if not value:
                continue
            normalized = _normalize(value)
            if normalized and ' ' not in normalized:
                names.add(normalized)
    return names


def gazetteer() -> Set[str]:
    """Normalized single-word place names, countries, regions and demonyms."""
    global _GAZETTEER
    if _GAZETTEER is not None:
        return _GAZETTEER
    with _GAZETTEER_LOCK:
        if _GAZETTEER is None:
            words = set(CONTINENTS_AND_REGIONS) | set(NATIONALITIES) | set(COUNTRY_ALIASES) | set(CITIES)
            words |= _country_names()
            _GAZETTEER = {_normalize(w) for w in words if _normalize(w)}
    return _GAZETTEER


def is_geographic_label(label: str) -> bool:
    """True for a tag that names a place, country, region or nationality."""
    key = _normalize(label)
    if not key:
        return False
    if key in gazetteer():
        return True
    # normalize_tags strips a trailing 's' ("alps" -> "alp", "americas" -> "america").
    return key + 's' in gazetteer()


def own_place_terms(city: str = '', region: str = '', country: str = '') -> Set[str]:
    """Normalized words that name the photo's OWN place in any spelling, so a
    tag equal to one of them is consistent with the GPS rather than a
    contradiction."""
    terms: Set[str] = set()
    for name in (city, region, country):
        for variant in place_name_variants(name, country):
            for part in _normalize(variant).split(' '):
                if part:
                    terms.add(part)
    return terms


def contradicting_place_tags(tags: Iterable[str], *, city: str = '', region: str = '', country: str = '') -> Set[str]:
    """Among ``tags`` (already normalized), the ones naming a place that is
    not this photo's own reverse-geocoded place. Callers only ask when the
    photo has coordinates and a geocoded place; without those nothing can
    contradict and the set is empty."""
    if not (str(city or '').strip() or str(country or '').strip()):
        return set()
    own = own_place_terms(city, region, country)
    return {tag for tag in tags if is_geographic_label(tag) and _normalize(tag) not in own}
