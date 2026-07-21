import json
import os
import re
import unicodedata
from typing import Dict, List, Optional

from exif_utils import parse_exif_data

MAX_TAG_LENGTH = int(os.getenv('MAX_TAG_LENGTH', '48'))
MAX_TAGS_STORED = int(os.getenv('MAX_TAGS_STORED', '40'))
MAX_WEAK_TAGS_STORED = int(os.getenv('MAX_WEAK_TAGS_STORED', '40'))
AI_TAG_MIN_CONFIDENCE = float(os.getenv('AI_TAG_MIN_CONFIDENCE', '0.18'))
GENERIC_TAG_MIN_CONFIDENCE = float(os.getenv('GENERIC_TAG_MIN_CONFIDENCE', '0.55'))
TOKEN_CANONICAL_MAP = {
    'automobile': 'car',
    'automobiles': 'car',
    'bike': 'bicycle',
    'bikes': 'bicycle',
    'building': 'architecture',
    'buildings': 'architecture',
    'child': 'child',
    'children': 'child',
    'colours': 'color',
    'grey': 'gray',
    'human': 'person',
    'humans': 'person',
    'leaves': 'leaf',
    'men': 'man',
    'sverige': 'sweden',
    'malmo': 'malmo',
    'malmö': 'malmo',
    'nyc': 'new york',
    'people': 'person',
    'persons': 'person',
    'pics': 'photo',
    'pictures': 'photo',
    'sea': 'ocean',
    'seas': 'ocean',
    'women': 'woman',
}
GENERIC_LOW_VALUE_TAGS = {
    'abstract', 'background', 'close', 'closeup', 'color', 'day',
    'document', 'image', 'indoor', 'indoors', 'macro', 'monochrome',
    'object', 'outdoor', 'outdoors', 'photo', 'photograph',
    'picture', 'scene', 'screenshot', 'snapshot', 'stock photo', 'thing',
    'unknown',
}
BACKGROUND_HINT_TAGS = {
    'architecture', 'background', 'building', 'cloud', 'clouds', 'floor',
    'grass', 'indoor', 'outdoor', 'room', 'sky', 'street', 'wall',
}
ANIMAL_TAGS = {
    'animal', 'bear', 'bee', 'bird', 'butterfly', 'camel', 'cat', 'chicken',
    'cow', 'deer', 'dog', 'dolphin', 'duck', 'eagle', 'elephant', 'fish',
    'fox', 'frog', 'giraffe', 'goat', 'goose', 'horse', 'insect', 'kitten',
    'lion', 'livestock', 'lizard', 'monkey', 'owl', 'parrot', 'penguin',
    'pet', 'pig', 'puppy', 'rabbit', 'shark', 'sheep', 'snake', 'spider',
    'squirrel', 'swan', 'tiger', 'turtle', 'whale', 'wildlife', 'zebra',
}
PLANT_TAGS = {
    'bush', 'cactus', 'fern', 'flower', 'garden', 'grass', 'leaf', 'leaves',
    'mushroom', 'palmtree', 'plant', 'rose', 'sunflower', 'tree', 'tulip',
}
FLOWER_TAGS = {
    'blossom', 'daisy', 'flower', 'lavender', 'lily', 'orchid', 'rose',
    'sunflower', 'tulip',
}
PERSON_TAGS = {
    'adult', 'baby', 'boy', 'bride', 'child', 'couple', 'crowd', 'face',
    'family', 'girl', 'groom', 'group', 'man', 'person',
    'portrait', 'selfie', 'toddler', 'woman',
}
PLACE_TAGS = {
    'airport', 'beach', 'bridge', 'canyon', 'castle', 'church', 'city',
    'cliff', 'coast', 'countryside', 'desert', 'dock', 'forest', 'garden',
    'harbor', 'hill', 'house', 'island', 'lake', 'landscape', 'lighthouse',
    'malmo', 'market', 'mountain', 'new york', 'ocean', 'park', 'playground',
    'pond', 'river', 'sea', 'shore', 'skyscraper', 'stadium', 'station',
    'stream', 'street', 'sweden', 'valley', 'waterfall', 'waterfront',
}
ACTION_TAGS = {
    'climbing', 'cooking', 'cycling', 'dancing', 'driving', 'eating',
    'exercise', 'fishing', 'hiking', 'kayaking', 'playing', 'reading',
    'running', 'sailing', 'shopping', 'singing', 'skiing', 'sleeping',
    'snowboarding', 'surfing', 'swimming', 'traveling', 'walking', 'working',
    'yoga',
}
SCENE_TAGS = {
    'aerial', 'architecture', 'autumn', 'cityscape', 'cloud', 'clouds',
    'dawn', 'field', 'fireworks', 'food', 'meadow', 'night', 'nightsky',
    'snow', 'stars', 'sunrise', 'sunset', 'winter',
}
SEASON_TAGS = {'spring', 'summer', 'autumn', 'fall', 'winter'}
WEATHER_TAGS = {
    'cloud', 'clouds', 'fog', 'mist', 'rain', 'rainbow', 'snow', 'storm',
    'sunny', 'weather', 'wind',
}
BUCKET_RANK = {
    'person': 5.0,
    'animal': 4.8,
    'flower': 4.7,
    'plant': 4.6,
    'object': 4.0,
    'place': 3.5,
    'action': 3.0,
    'scene': 2.0,
    'season': 1.8,
    'weather': 1.6,
    'background': 1.0,
}
SOURCE_DEFAULT_CONFIDENCE = {
    'user': 1.0,
    'stored': 0.95,
    'ai_person': 0.9,
    'ai_object': 0.82,
    'ai_tag': 0.72,
    'ai_prediction': 0.0,
    'location': 0.88,
    'face': 0.82,
}
SOURCE_RANK = {
    'user': 3.0,
    'stored': 2.5,
    'ai_person': 2.4,
    'ai_object': 2.2,
    'location': 2.0,
    'face': 1.8,
    'ai_tag': 1.5,
    'ai_prediction': 1.0,
}
VISUAL_MODIFIERS = {
    'black', 'blue', 'brown', 'gold', 'gray', 'green', 'grey', 'orange',
    'pink', 'purple', 'red', 'silver', 'tan', 'teal', 'white', 'yellow',
}
SEARCH_STOP_WORDS = {'in', 'at', 'near', 'from', 'by', 'with', 'wearing', 'holding', 'beside', 'next', 'to', 'and'}
MODIFIER_FILLER_WORDS = {'color', 'colour'}
PREDICTION_TAG_MIN_SCORE = float(os.getenv('SEMANTIC_PREDICTION_TAG_MIN_SCORE', '0.08'))
MAX_PREDICTION_TAGS = int(os.getenv('SEMANTIC_PREDICTION_TAG_LIMIT', '160'))
SEMANTIC_TERM_EXPANSIONS = {
    'water': [
        'waterfall', 'river', 'lake', 'ocean', 'beach', 'waterfront', 'seaside',
        'pool', 'fountain', 'swimming', 'surfing', 'sailing', 'kayaking',
        'rafting', 'diving', 'snorkeling', 'reef', 'harbor', 'dock', 'marina',
    ],
    'waterfall': ['water', 'river', 'stream', 'nature', 'landscape', 'mist'],
    'ocean': ['sea', 'seaside', 'seascape', 'beach', 'water', 'waves', 'surfing', 'sailing', 'reef'],
    'beach': ['sand', 'ocean', 'sea', 'seaside', 'water', 'surfing', 'summer'],
    'river': ['water', 'waterfall', 'bridge', 'nature', 'landscape'],
    'lake': ['water', 'mountain', 'forest', 'reflection', 'landscape'],
    'sky': ['clouds', 'sunset', 'sunrise', 'rainbow', 'night sky', 'stars', 'twilight'],
    'blue': ['sky', 'ocean', 'water', 'lake', 'river', 'pool', 'seascape', 'waterfront', 'night sky', 'ice'],
    'green': ['grass', 'tree', 'forest', 'garden', 'park', 'field', 'leaf', 'nature'],
    'white': ['snow', 'clouds', 'ice', 'wedding', 'winter'],
    'yellow': ['sunrise', 'sunset', 'sunflower', 'flower', 'autumn'],
    'orange': ['sunset', 'sunrise', 'autumn', 'fireworks'],
    'red': ['rose', 'flower', 'sunset', 'festival', 'fireworks'],
    'pink': ['flower', 'rose', 'orchid', 'sunset'],
    'purple': ['flower', 'lavender', 'twilight', 'night sky'],
    'black': ['night', 'shadow', 'silhouette', 'night sky'],
    'grey': ['gray', 'clouds', 'fog', 'storm', 'rain'],
    'gray': ['grey', 'clouds', 'fog', 'storm', 'rain'],
    'nature': ['landscape', 'forest', 'tree', 'mountain', 'water', 'flower', 'wildlife'],
    'landscape': ['nature', 'mountain', 'forest', 'field', 'valley', 'water', 'sky'],
    'city': ['street', 'building', 'architecture', 'urban', 'cityscape', 'market'],
    'food': ['meal', 'breakfast', 'lunch', 'dinner', 'dessert', 'restaurant', 'plate'],
}
MAX_EXPANDED_TERMS_PER_TOKEN = 8


def _singularize_word(word: str) -> str:
    if len(word) <= 3 or word.endswith('ss'):
        return word
    if word.endswith('ies') and len(word) > 4:
        return f'{word[:-3]}y'
    if word.endswith('ves') and len(word) > 4:
        return f'{word[:-3]}f'
    if word.endswith('es') and len(word) > 3:
        base = word[:-2]
        if base.endswith(('s', 'x', 'z', 'ch', 'sh', 'o')):
            return base
    if word.endswith('s'):
        return word[:-1]
    return word


def _normalize_token(text: str) -> str:
    folded = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode('ascii')
    cleaned = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', folded.lower())).strip()
    if not cleaned:
        return ''
    canonical = []
    for tok in cleaned.split(' '):
        if not tok:
            continue
        mapped = TOKEN_CANONICAL_MAP.get(tok, tok)
        canonical.append(TOKEN_CANONICAL_MAP.get(mapped, _singularize_word(mapped)))
    return ' '.join(canonical)


def normalize_tags(tags: List[str]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for tag in tags:
        cleaned = _normalize_token(str(tag))
        if not cleaned:
            continue
        if ' ' in cleaned:
            continue
        if len(cleaned) > MAX_TAG_LENGTH:
            cleaned = cleaned[:MAX_TAG_LENGTH]
        if cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)
        if len(normalized) >= MAX_TAGS_STORED:
            break
    return normalized


def _coerce_confidence(value, source: str) -> float:
    try:
        confidence = float(value)
    except Exception:
        confidence = SOURCE_DEFAULT_CONFIDENCE.get(str(source or '').strip(), 0.5)
    return round(max(0.0, min(confidence, 1.0)), 4)


def classify_tag_bucket(tag: str, source: str = '') -> str:
    normalized = _normalize_token(tag)
    if not normalized:
        return 'background'
    if normalized in PERSON_TAGS or str(source or '').strip() in {'ai_person', 'face'}:
        return 'person'
    if normalized in ANIMAL_TAGS:
        return 'animal'
    if normalized in FLOWER_TAGS:
        return 'flower'
    if normalized in PLANT_TAGS:
        return 'plant'
    if normalized in PLACE_TAGS or str(source or '').strip() == 'location':
        return 'place'
    if normalized in ACTION_TAGS or normalized.endswith('ing'):
        return 'action'
    if normalized in SEASON_TAGS:
        return 'season'
    if normalized in WEATHER_TAGS:
        return 'weather'
    if normalized in SCENE_TAGS:
        return 'scene'
    if normalized in BACKGROUND_HINT_TAGS:
        return 'background'
    return 'object'


def _tag_importance(record: Dict) -> float:
    confidence = _coerce_confidence(record.get('confidence'), str(record.get('source') or ''))
    bucket = str(record.get('bucket') or classify_tag_bucket(str(record.get('tag') or ''), str(record.get('source') or '')))
    source = str(record.get('source') or '')
    generic_penalty = 2.0 if str(record.get('tag') or '') in GENERIC_LOW_VALUE_TAGS else 0.0
    subject_bonus = 0.45 if record.get('role') == 'subject' else 0.0
    return round((confidence * 10.0) + BUCKET_RANK.get(bucket, 1.0) + SOURCE_RANK.get(source, 0.5) + subject_bonus - generic_penalty, 4)


def _tag_record(label: str, source: str, confidence=None, provenance: Optional[Dict] = None) -> Optional[Dict]:
    tag = _normalize_token(label)
    if not tag:
        return None
    if len(tag) > MAX_TAG_LENGTH:
        tag = tag[:MAX_TAG_LENGTH].strip()
    if not tag:
        return None
    source = str(source or 'ai_tag').strip() or 'ai_tag'
    confidence_value = _coerce_confidence(confidence, source)
    bucket = classify_tag_bucket(tag, source)
    low_value = tag in GENERIC_LOW_VALUE_TAGS
    record = {
        'tag': tag,
        'source': source,
        'confidence': confidence_value,
        'bucket': bucket,
        'role': 'subject' if bucket in {'animal', 'flower', 'object', 'person', 'place', 'plant', 'action'} else 'background',
        'lowValue': low_value,
        'provenance': provenance or {},
    }
    record['importance'] = _tag_importance(record)
    return record


def curate_tag_records(records: List[Dict], *, max_tags: int = MAX_TAGS_STORED) -> Dict[str, object]:
    best_by_tag: Dict[str, Dict] = {}
    weak_by_tag: Dict[str, Dict] = {}

    for item in records or []:
        if not isinstance(item, dict):
            continue
        record = _tag_record(
            str(item.get('label') or item.get('tag') or ''),
            str(item.get('source') or 'ai_tag'),
            item.get('confidence'),
            item.get('provenance') if isinstance(item.get('provenance'), dict) else {},
        )
        if not record:
            continue
        protected = record['source'] in {'user', 'stored'}
        if (
            not protected
            and (
                record['confidence'] < AI_TAG_MIN_CONFIDENCE
                or (record['lowValue'] and record['confidence'] < GENERIC_TAG_MIN_CONFIDENCE)
            )
        ):
            existing_weak = weak_by_tag.get(record['tag'])
            if existing_weak is None or record['importance'] > existing_weak['importance']:
                weak_by_tag[record['tag']] = record
            continue
        existing = best_by_tag.get(record['tag'])
        if existing is None or record['importance'] > existing['importance']:
            best_by_tag[record['tag']] = record

    ranked = sorted(best_by_tag.values(), key=lambda item: (-item['importance'], item['tag']))
    selected = ranked[:max(0, int(max_tags))]
    selected_tags = [item['tag'] for item in selected]
    selected_set = set(selected_tags)
    weak = sorted(
        [item for tag, item in weak_by_tag.items() if tag not in selected_set],
        key=lambda item: (-item['confidence'], item['tag']),
    )[:MAX_WEAK_TAGS_STORED]

    buckets: Dict[str, List[str]] = {}
    for item in selected:
        buckets.setdefault(item['bucket'], []).append(item['tag'])

    subject_tags = [item['tag'] for item in selected if item['role'] == 'subject']
    background_tags = [item['tag'] for item in selected if item['role'] == 'background']
    metadata = [
        {
            'tag': item['tag'],
            'source': item['source'],
            'confidence': item['confidence'],
            'importance': item['importance'],
            'bucket': item['bucket'],
            'role': item['role'],
            **({'provenance': item['provenance']} if item.get('provenance') else {}),
        }
        for item in selected
    ]
    return {
        'tags': selected_tags,
        'subjectTags': subject_tags,
        'backgroundTags': background_tags,
        'weakTags': [
            {
                'tag': item['tag'],
                'source': item['source'],
                'confidence': item['confidence'],
                'bucket': item['bucket'],
                'reason': 'low_confidence' if item['confidence'] < AI_TAG_MIN_CONFIDENCE else 'low_value',
            }
            for item in weak
        ],
        'tagBuckets': buckets,
        'tagMetadata': metadata,
    }


def parse_tags(raw_tags) -> List[str]:
    if isinstance(raw_tags, list):
        return normalize_tags([str(tag) for tag in raw_tags])
    if isinstance(raw_tags, str):
        try:
            parsed = json.loads(raw_tags)
            if isinstance(parsed, list):
                return normalize_tags([str(tag) for tag in parsed])
        except Exception:
            pass
    return []


def parse_json_list(raw_value) -> List[str]:
    if isinstance(raw_value, list):
        return normalize_tags([str(item) for item in raw_value])
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, list):
                return normalize_tags([str(item) for item in parsed])
        except Exception:
            pass
    return []


def prediction_tags(metadata: Dict) -> List[str]:
    try:
        processing = json.loads(metadata.get('processing_metadata', '{}') or '{}')
    except Exception:
        processing = {}
    if not isinstance(processing, dict):
        return []
    ai_vision = processing.get('client_ai_vision')
    if not isinstance(ai_vision, dict):
        return []
    predictions = ai_vision.get('predictions')
    if not isinstance(predictions, list):
        return []

    labels = []
    for item in predictions[:MAX_PREDICTION_TAGS]:
        if not isinstance(item, dict):
            continue
        try:
            score = float(item.get('score') or 0)
        except Exception:
            score = 0.0
        if score < PREDICTION_TAG_MIN_SCORE:
            continue
        labels.append(str(item.get('label') or ''))
    return normalize_tags(labels)


def location_tags(metadata: Dict) -> List[str]:
    return normalize_tags([
        str(metadata.get('locationCity', '')),
        str(metadata.get('locationCountry', '')),
        str(metadata.get('address', '')),
    ])


def gps_presence_tags(metadata: Dict) -> List[str]:
    exif = parse_exif_data(metadata.get('exifData', '{}'))
    gps_present = ('GPSInfo' in exif) or any(str(key).startswith('GPS.') for key in exif.keys())
    has_readable_location = bool(
        str(metadata.get('latitude', '')).strip() and str(metadata.get('longitude', '')).strip()
    ) or bool(
        str(metadata.get('locationCity', '')).strip() or
        str(metadata.get('locationCountry', '')).strip() or
        str(metadata.get('address', '')).strip()
    )

    if gps_present and not has_readable_location:
        return ['gps tagged', 'location metadata']
    return []


def face_presence_tags(metadata: Dict) -> List[str]:
    try:
        face_count = int(metadata.get('faceCount', 0) or 0)
    except Exception:
        face_count = 0

    if face_count <= 0:
        return []

    tags = ['face', 'person', 'portrait', 'human']
    if face_count == 1:
        tags.append('selfie')
    elif face_count == 2:
        tags.extend(['selfie', 'pair'])
    else:
        tags.extend(['people', 'group', 'family', 'crowd'])

    ai_person_label = _normalize_token(str(metadata.get('aiPersonLabel') or ''))
    if ai_person_label:
        tags.append(ai_person_label)

    return normalize_tags(tags)


def effective_tags(metadata: Dict) -> List[str]:
    subjects = parse_json_list(metadata.get('subjectTags', '[]'))
    people = parse_json_list(metadata.get('peopleNames', '[]'))
    stored = parse_tags(metadata.get('tags', '[]'))
    objects = parse_json_list(metadata.get('objects', '[]'))
    background = parse_json_list(metadata.get('backgroundTags', '[]'))
    prediction = prediction_tags(metadata)
    location = location_tags(metadata)
    face = face_presence_tags(metadata)
    gps = gps_presence_tags(metadata)

    primary = normalize_tags(subjects + people + location)
    secondary = normalize_tags(stored + objects + background + prediction + face + gps)
    return primary + [tag for tag in secondary if tag not in primary]


def build_semantic_text(filename: str, metadata: Dict) -> str:
    subject_tags = parse_json_list(metadata.get('subjectTags', '[]'))
    location = location_tags(metadata)
    semantic_candidates = set(normalize_tags(subject_tags + location))

    if not semantic_candidates:
        semantic_candidates = set(effective_tags(metadata))

    text_parts = []
    for tag in sorted(semantic_candidates):
        text_parts.append(tag)

    for field in [filename, metadata.get('caption'), metadata.get('ocrText'), metadata.get('address'), metadata.get('locationCity'), metadata.get('locationCountry')]:
        text = str(field or '').strip()
        if not text:
            continue
        for token in _normalize_token(text).split(' '):
            if token and token in semantic_candidates:
                text_parts.append(token)

    return ' '.join(dict.fromkeys(text_parts))[:10000]


def build_semantic_layers(filename: str, metadata: Dict) -> Dict[str, List[str]]:
    subject_tags = parse_json_list(metadata.get('subjectTags', '[]'))
    if not subject_tags:
        subject_tags = effective_tags(metadata)
    background_tags = parse_json_list(metadata.get('backgroundTags', '[]'))
    weak_payload = []
    if isinstance(metadata.get('weakTags'), str):
        try:
            parsed_weak = json.loads(metadata.get('weakTags', '[]') or '[]')
            weak_payload = parsed_weak if isinstance(parsed_weak, list) else []
        except Exception:
            weak_payload = []
    weak_tags = [
        str(item.get('tag') or '').strip()
        for item in weak_payload
        if isinstance(item, dict)
    ]
    return {
        'subjects': normalize_tags(subject_tags),
        'locations': location_tags(metadata),
        'backgrounds': normalize_tags(background_tags),
        'weak': normalize_tags(weak_tags),
    }


def parse_search_query(query: str) -> Dict[str, List[str]]:
    clean = _normalize_token(query)
    if not clean:
        return {
            'subject': [],
            'location': [],
            'all': [],
            'expanded': [],
            'modifiers': [],
            'required_object': [],
            'exact_phrases': [],
        }

    split = re.split(r'\b(?:in|at|near|from)\b', clean, maxsplit=1)
    if len(split) == 2:
        subject_tokens = [token for token in split[0].split(' ') if token]
        location_part = re.split(r'\b(?:by|with|wearing|holding|beside|next to)\b', split[1], maxsplit=1)[0]
        location_tokens = [token for token in location_part.split(' ') if token]
    else:
        subject_tokens = [token for token in clean.split(' ') if token]
        location_tokens = []

    if not subject_tokens and location_tokens:
        subject_tokens = list(location_tokens)

    all_tokens = [token for token in clean.split(' ') if token and token not in SEARCH_STOP_WORDS]
    if not all_tokens:
        all_tokens = [token for token in clean.split(' ') if token]

    modifiers: List[str] = []
    required_object: List[str] = []
    exact_phrases: List[str] = []
    searchable_terms = [token for token in all_tokens if token not in MODIFIER_FILLER_WORDS]

    if len(all_tokens) >= 3 and all_tokens[1] in MODIFIER_FILLER_WORDS and all_tokens[2] in VISUAL_MODIFIERS:
        modifiers = [all_tokens[2]]
        required_object = [all_tokens[0]]
    elif len(searchable_terms) >= 2:
        for idx in range(len(searchable_terms) - 1):
            if searchable_terms[idx] in VISUAL_MODIFIERS and searchable_terms[idx + 1] not in VISUAL_MODIFIERS:
                modifiers = [searchable_terms[idx]]
                required_object = [searchable_terms[idx + 1]]
                break
        if not required_object:
            found_modifiers = [token for token in searchable_terms[:-1] if token in VISUAL_MODIFIERS]
            if found_modifiers and searchable_terms[-1] not in VISUAL_MODIFIERS:
                modifiers = found_modifiers[:1]
                required_object = [searchable_terms[-1]]

    if modifiers and required_object:
        modifier = modifiers[0]
        obj = required_object[0]
        exact_phrases = [
            f'{modifier} {obj}',
            f'wearing {modifier} {obj}',
            f'{obj} color {modifier}',
            f'{obj} colour {modifier}',
        ]

    expanded_terms = expand_search_terms(all_tokens)

    return {
        'subject': subject_tokens,
        'location': location_tokens,
        'all': all_tokens,
        'expanded': expanded_terms,
        'modifiers': modifiers,
        'required_object': required_object,
        'exact_phrases': exact_phrases,
    }


def _token_variants(token: str) -> List[str]:
    if not token:
        return []
    variants = [token]
    if token.endswith('ies') and len(token) > 4:
        variants.append(f'{token[:-3]}y')
    elif token.endswith('es') and len(token) > 3:
        if token[:-2].endswith(('s', 'x', 'z', 'ch', 'sh', 'o')):
            variants.append(token[:-2])
        else:
            variants.append(token[:-1])
    elif token.endswith('s') and len(token) > 3 and not token.endswith('ss'):
        variants.append(token[:-1])

    bases = list(variants)
    for base in bases:
        if base.endswith('y') and len(base) > 2:
            variants.append(f'{base[:-1]}ies')
        elif base.endswith(('s', 'x', 'z', 'ch', 'sh')):
            variants.append(f'{base}es')
        else:
            variants.append(f'{base}s')
    return list(dict.fromkeys(variants))


def expand_search_terms(tokens: List[str]) -> List[str]:
    expanded: List[str] = []
    seen = set(tokens)
    for token in tokens:
        for variant in _token_variants(token):
            normalized_variant = _normalize_token(variant)
            if normalized_variant and normalized_variant not in seen:
                expanded.append(normalized_variant)
                seen.add(normalized_variant)
        for related in SEMANTIC_TERM_EXPANSIONS.get(token, [])[:MAX_EXPANDED_TERMS_PER_TOKEN]:
            for part in _normalize_token(related).split(' '):
                if part and part not in seen:
                    expanded.append(part)
                    seen.add(part)
    return expanded


def build_expanded_query_text(query: str, tokens: Dict[str, List[str]] = None) -> str:
    tokens = tokens or parse_search_query(query)
    clean = _normalize_token(query)
    expanded = tokens.get('expanded', [])
    if not expanded:
        return clean or query
    return ' '.join([clean, *expanded]).strip()


def _contains_term(text: str, token: str) -> bool:
    return any(re.search(rf'\b{re.escape(variant)}\b', text) for variant in _token_variants(token))


def _contains_related_term(text: str, token: str) -> bool:
    if not token:
        return False
    if _contains_term(text, token):
        return True
    if len(token) < 4:
        return False
    return bool(re.search(rf'\b{re.escape(token)}[a-z0-9]*\b', text))


def lexical_search_score(tokens: Dict[str, List[str]], filename: str, metadata: Dict, exif_data: Dict[str, str]) -> float:
    tags = effective_tags(metadata)
    filename_text = _normalize_token(filename)
    semantic_text = _normalize_token(build_semantic_text(filename, metadata))
    subject_text = ' '.join([filename_text, semantic_text, _normalize_token(exif_data.get('Model', ''))])
    location_text = _normalize_token(' '.join([
        str(metadata.get('address', '')),
        str(metadata.get('locationCity', '')),
        str(metadata.get('locationCountry', '')),
    ]))
    location_text_with_tags = _normalize_token(' '.join([
        location_text,
        ' '.join(tags),
    ]))

    subject_tokens = tokens.get('subject', [])
    location_tokens = tokens.get('location', [])
    all_tokens = tokens.get('all', [])
    expanded_tokens = tokens.get('expanded', [])
    modifiers = tokens.get('modifiers', [])
    required_object = tokens.get('required_object', [])
    exact_phrases = tokens.get('exact_phrases', [])

    if not all_tokens:
        return 0.0

    if location_tokens and not all(_contains_related_term(location_text_with_tags, token) for token in location_tokens):
        return 0.0

    score = 0.0
    if required_object:
        object_token = required_object[0]
        object_in_tags = any(variant in tags for variant in _token_variants(object_token))
        object_in_text = _contains_related_term(subject_text, object_token)
        if not object_in_tags and not object_in_text:
            return 0.0
        score += 6.0 if object_in_tags else 3.5

    modifier_matches = 0
    for modifier in modifiers:
        modifier_in_tags = modifier in tags
        modifier_in_text = _contains_related_term(subject_text, modifier)
        if modifier_in_tags or modifier_in_text:
            modifier_matches += 1
            score += 3.0 if modifier_in_tags else 1.5

    if modifiers and modifier_matches == len(modifiers):
        score += 12.0

    for phrase in exact_phrases:
        normalized_phrase = _normalize_token(phrase)
        if normalized_phrase and normalized_phrase in subject_text:
            score += 5.0

    for token in all_tokens:
        if token in tags:
            score += 3.0
        elif _contains_related_term(subject_text, token):
            score += 1.5
        if _contains_related_term(semantic_text, token):
            score += 2.0
        if _contains_related_term(location_text_with_tags, token):
            score += 2.5

    expanded_score = 0.0
    for token in expanded_tokens:
        if token in tags:
            expanded_score += 1.0
        elif _contains_related_term(subject_text, token):
            expanded_score += 0.7
        if _contains_related_term(semantic_text, token):
            expanded_score += 0.8
        if _contains_related_term(location_text_with_tags, token):
            expanded_score += 0.8
    score += min(expanded_score, 5.0)

    if subject_tokens and location_tokens:
        score += 3.0
    return score


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    n = min(len(vec_a), len(vec_b))
    if n == 0:
        return 0.0

    a = vec_a[:n]
    b = vec_b[:n]
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
