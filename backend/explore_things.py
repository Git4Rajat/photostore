"""What Explore's "Things" shelf may show, and how Places are keyed.

The shelf is the top 24 tags by photo count, and with a ~10k-label zero-shot
vocabulary the top of that list filled with junk: adjectives ("Petite"),
verbs ("Ate"), abstractions ("Pricing", "Dharma"), occupations ("Producer",
"Student"), places and nationalities ("Thailand", "Icelandic") and one
stemmer casualty ("Calculu"). A thing on this shelf should be something you
can point at in a photo -- an object, an animal, food, a kind of scene, or
an activity -- so membership is an explicit allowlist of those, plus the
usual exclusions (sensitive/unpleasant labels, anything geographic, and a
confidence floor where the row carries per-tag confidence).

Both Explore builders (app.ExploreAccumulator for the raw stream and
summary_incremental.explore_memberships for the search database) call into
here so they can never drift apart; routes/explore.py applies the same
predicate to an already-stored summary so a shelf built before this module
existed is cleaned on read, without a rebuild.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

import place_names
from search_utils import (
    ACTION_TAGS, ANIMAL_TAGS, FLOWER_TAGS, GENERIC_LOW_VALUE_TAGS, GENERIC_TAG_MIN_CONFIDENCE,
    PLANT_TAGS, SCENE_TAGS, WEATHER_TAGS, _normalize_token, is_sensitive_auto_tag_label,
    parse_json_list, parse_tags,
)

# A tag with a recorded AI confidence below this never makes the shelf. Same
# bar as GENERIC_TAG_MIN_CONFIDENCE: a merely-present background guess is not
# something to browse by. Tags without recorded confidence (user tags, rows
# written before tagMetadata existed, reduced search rows) are kept.
EXPLORE_THING_MIN_CONFIDENCE = float(os.getenv('EXPLORE_THING_MIN_CONFIDENCE', str(GENERIC_TAG_MIN_CONFIDENCE)))

_CONCRETE_THINGS = {
    # Vehicles and transport
    'airplane', 'aircraft', 'jet', 'helicopter', 'rocket', 'balloon', 'hot air balloon', 'bicycle', 'bike',
    'motorcycle', 'scooter', 'skateboard', 'car', 'truck', 'bus', 'tram', 'train', 'railway', 'subway',
    'taxi', 'van', 'jeep', 'tractor', 'boat', 'ship', 'sailboat', 'yacht', 'canoe', 'kayak', 'ferry',
    'cruise', 'submarine', 'wheel', 'tire', 'engine', 'convertible', 'limousine', 'ambulance', 'firetruck',
    'stroller', 'wheelbarrow', 'cart', 'sled', 'sleigh', 'cable car', 'gondola', 'rickshaw', 'tuk tuk',
    # Buildings, structures, rooms
    'architecture', 'building', 'skyscraper', 'tower', 'bridge', 'castle', 'palace', 'fort', 'fortress',
    'ruin', 'ruins', 'temple', 'church', 'cathedral', 'mosque', 'pagoda', 'shrine', 'monastery', 'chapel',
    'museum', 'gallery', 'library', 'school', 'university', 'stadium', 'arena', 'theater', 'theatre',
    'cinema', 'hotel', 'restaurant', 'cafe', 'bar', 'pub', 'shop', 'store', 'mall', 'market', 'bazaar',
    'supermarket', 'bakery', 'airport', 'station', 'harbor', 'harbour', 'port', 'marina', 'pier', 'dock',
    'lighthouse', 'windmill', 'barn', 'farm', 'greenhouse', 'cabin', 'cottage', 'house', 'home', 'villa',
    'apartment', 'balcony', 'porch', 'patio', 'terrace', 'rooftop', 'roof', 'chimney', 'garage', 'shed',
    'tent', 'camp', 'campsite', 'playground', 'park', 'garden', 'backyard', 'fountain', 'statue',
    'monument', 'memorial', 'sculpture', 'arch', 'gate', 'door', 'window', 'stairs', 'staircase',
    'elevator', 'escalator', 'hallway', 'corridor', 'kitchen', 'bathroom', 'bedroom', 'living room',
    'office', 'classroom', 'hospital', 'gym', 'pool', 'swimming pool', 'sauna', 'spa', 'zoo', 'aquarium',
    'amusement park', 'carousel', 'ferris wheel', 'roller coaster', 'tunnel', 'road', 'highway', 'street',
    'alley', 'sidewalk', 'crosswalk', 'parking', 'fence', 'wall', 'brick', 'graffiti', 'mural', 'sign',
    'billboard', 'streetlight', 'lamp post', 'bench', 'tower block', 'skyline', 'cityscape', 'city',
    'town', 'village', 'downtown', 'plaza', 'square', 'courtyard', 'canal', 'pyramid', 'dam',
    # Nature and landscape
    'landscape', 'nature', 'mountain', 'mountains', 'hill', 'valley', 'canyon', 'cliff', 'rock', 'boulder',
    'cave', 'volcano', 'glacier', 'iceberg', 'desert', 'dune', 'sand', 'beach', 'coast', 'shore', 'island',
    'ocean', 'sea', 'wave', 'waves', 'surf', 'lake', 'pond', 'river', 'stream', 'creek', 'waterfall',
    'rapids', 'hot spring', 'geyser', 'forest', 'woods', 'jungle', 'rainforest', 'tree', 'trees', 'pine',
    'palm', 'palmtree', 'oak', 'birch', 'bamboo', 'meadow', 'field', 'farmland', 'vineyard', 'orchard',
    'prairie', 'savanna', 'swamp', 'marsh', 'wetland', 'reef', 'coral', 'moss', 'fern', 'bush', 'hedge',
    'grass', 'lawn', 'path', 'trail', 'sky', 'cloud', 'clouds', 'sun', 'sunrise', 'sunset', 'dawn',
    'dusk', 'twilight', 'night', 'night sky', 'nightsky', 'moon', 'star', 'stars', 'milky way', 'galaxy',
    'aurora', 'northern lights', 'rainbow', 'lightning', 'storm', 'thunderstorm', 'rain', 'snow',
    'snowfall', 'blizzard', 'ice', 'frost', 'fog', 'mist', 'wind', 'hail', 'sunshine', 'sunny', 'sunbeam',
    'reflection', 'shadow', 'silhouette', 'horizon', 'panorama', 'aerial', 'autumn', 'fall', 'winter',
    'spring', 'summer', 'foliage', 'leaf', 'leaves', 'flower', 'flowers', 'blossom', 'cherry blossom',
    'rose', 'tulip', 'sunflower', 'daisy', 'lily', 'orchid', 'lavender', 'lotus', 'poppy', 'dandelion',
    'hibiscus', 'cactus', 'succulent', 'mushroom', 'plant', 'houseplant', 'bonsai', 'seed', 'pumpkin',
    # Animals
    'animal', 'animals', 'wildlife', 'pet', 'dog', 'puppy', 'cat', 'kitten', 'bird', 'birds', 'duck',
    'goose', 'swan', 'seagull', 'gull', 'pigeon', 'sparrow', 'parrot', 'owl', 'eagle', 'hawk', 'falcon',
    'crow', 'raven', 'robin', 'flamingo', 'peacock', 'penguin', 'ostrich', 'chicken', 'rooster', 'hen',
    'turkey', 'horse', 'pony', 'donkey', 'cow', 'cattle', 'calf', 'bull', 'sheep', 'lamb', 'goat', 'pig',
    'piglet', 'deer', 'moose', 'elk', 'reindeer', 'fox', 'wolf', 'bear', 'panda', 'koala', 'kangaroo',
    'rabbit', 'bunny', 'hamster', 'guinea pig', 'squirrel', 'hedgehog', 'mouse', 'otter', 'seal', 'walrus',
    'dolphin', 'whale', 'shark', 'fish', 'goldfish', 'jellyfish', 'starfish', 'crab', 'lobster', 'shrimp',
    'octopus', 'turtle', 'tortoise', 'frog', 'toad', 'lizard', 'gecko', 'snake', 'crocodile', 'alligator',
    'elephant', 'giraffe', 'zebra', 'lion', 'tiger', 'leopard', 'cheetah', 'monkey', 'gorilla', 'chimpanzee',
    'camel', 'llama', 'alpaca', 'hippo', 'rhino', 'buffalo', 'bison', 'butterfly', 'moth', 'bee',
    'bumblebee', 'ladybug', 'dragonfly', 'beetle', 'ant', 'spider', 'snail', 'insect', 'livestock',
    # Food and drink
    'food', 'meal', 'breakfast', 'brunch', 'lunch', 'dinner', 'dessert', 'snack', 'buffet', 'feast',
    'picnic', 'barbecue', 'bbq', 'grill', 'pizza', 'pasta', 'spaghetti', 'noodle', 'noodles', 'ramen',
    'sushi', 'rice', 'curry', 'biryani', 'dumpling', 'dumplings', 'taco', 'burrito', 'burger', 'hamburger',
    'sandwich', 'hot dog', 'fries', 'chips', 'salad', 'soup', 'stew', 'steak', 'meat', 'sausage', 'bacon',
    'egg', 'eggs', 'omelette', 'pancake', 'pancakes', 'waffle', 'toast', 'bread', 'baguette', 'croissant',
    'bagel', 'pretzel', 'cheese', 'butter', 'yogurt', 'cereal', 'porridge', 'cake', 'cupcake', 'muffin',
    'pie', 'tart', 'cookie', 'cookies', 'biscuit', 'brownie', 'donut', 'doughnut', 'pastry', 'chocolate',
    'candy', 'sweets', 'ice cream', 'gelato', 'popsicle', 'popcorn', 'fruit', 'fruits', 'apple', 'banana',
    'orange', 'lemon', 'lime', 'grape', 'grapes', 'strawberry', 'strawberries', 'raspberry', 'blueberry',
    'berry', 'berries', 'cherry', 'cherries', 'peach', 'pear', 'plum', 'mango', 'pineapple', 'melon',
    'watermelon', 'coconut', 'kiwi', 'avocado', 'tomato', 'potato', 'carrot', 'onion', 'garlic', 'pepper',
    'chili', 'cucumber', 'lettuce', 'cabbage', 'broccoli', 'corn', 'bean', 'beans', 'vegetable',
    'vegetables', 'herb', 'spice', 'spices', 'nut', 'nuts', 'almond', 'peanut', 'honey', 'jam', 'sauce',
    'coffee', 'espresso', 'latte', 'cappuccino', 'tea', 'juice', 'smoothie', 'milkshake', 'lemonade',
    'soda', 'water', 'beer', 'wine', 'champagne', 'cocktail', 'whisky', 'whiskey', 'milk', 'drink',
    'drinks', 'cup', 'mug', 'glass', 'bottle', 'plate', 'bowl', 'tray', 'cutlery', 'fork', 'spoon',
    'knife', 'chopsticks', 'teapot', 'kettle', 'oven', 'stove', 'fridge', 'microwave', 'blender',
    # Objects and belongings
    'toy', 'toys', 'doll', 'teddy bear', 'teddy', 'lego', 'puzzle', 'board game', 'chess', 'cards', 'dice',
    'ball', 'football', 'soccer ball', 'basketball', 'tennis ball', 'baseball', 'volleyball', 'frisbee',
    'kite', 'balloons', 'bubble', 'bubbles', 'book', 'books', 'notebook', 'magazine', 'newspaper', 'map',
    'globe', 'pen', 'pencil', 'crayon', 'paint', 'painting', 'drawing', 'canvas', 'easel', 'brush',
    'camera', 'lens', 'tripod', 'phone', 'smartphone', 'laptop', 'computer', 'keyboard', 'monitor',
    'screen', 'television', 'tv', 'headphones', 'speaker', 'microphone', 'radio', 'drone', 'robot',
    'clock', 'watch', 'calendar', 'lamp', 'lantern', 'candle', 'candles', 'flashlight', 'torch',
    'fireplace', 'campfire', 'bonfire', 'fire', 'flame', 'fireworks', 'sparkler', 'chair', 'armchair',
    'sofa', 'couch', 'table', 'desk', 'bed', 'pillow', 'blanket', 'curtain', 'carpet', 'rug', 'mirror',
    'shelf', 'bookshelf', 'cabinet', 'drawer', 'wardrobe', 'closet', 'vase', 'pot', 'basket', 'box',
    'bag', 'backpack', 'suitcase', 'luggage', 'handbag', 'purse', 'wallet', 'umbrella', 'hat', 'cap',
    'helmet', 'scarf', 'gloves', 'mittens', 'sunglasses', 'glasses', 'jacket', 'coat', 'dress', 'gown',
    'suit', 'tie', 'shirt', 't shirt', 'sweater', 'hoodie', 'jeans', 'shorts', 'skirt', 'uniform',
    'costume', 'mask', 'crown', 'tiara', 'jewelry', 'jewellery', 'necklace', 'bracelet', 'ring',
    'earrings', 'shoe', 'shoes', 'boots', 'sneakers', 'sandals', 'socks', 'guitar', 'piano', 'violin',
    'drum', 'drums', 'flute', 'trumpet', 'saxophone', 'ukulele', 'harp', 'instrument', 'gift', 'present',
    'presents', 'ribbon', 'bow', 'flag', 'banner', 'poster', 'trophy', 'medal', 'certificate', 'diploma',
    'badge', 'key', 'keys', 'lock', 'tool', 'tools', 'hammer', 'saw', 'drill', 'ladder', 'rope', 'bucket',
    'shovel', 'rake', 'hose', 'sprinkler', 'lawnmower', 'wheelchair', 'crutches', 'bandage', 'stethoscope',
    'coin', 'coins', 'money', 'ticket', 'passport', 'envelope', 'letter', 'card', 'stamp', 'souvenir',
    'ornament', 'decoration', 'decorations', 'garland', 'wreath', 'christmas tree', 'snowman', 'pumpkin',
    'jack o lantern', 'easter egg', 'menorah', 'rangoli', 'diya', 'lantern', 'pinata', 'confetti',
    'streamer', 'bunting', 'tablecloth', 'napkin', 'apron', 'towel', 'soap', 'toothbrush', 'bathtub',
    'shower', 'sink', 'toilet', 'crib', 'cradle', 'pacifier', 'bib', 'diaper', 'swing', 'slide', 'seesaw',
    'sandbox', 'trampoline', 'treehouse', 'hammock', 'bench', 'gazebo', 'pergola', 'umbrella', 'parasol',
    'deckchair', 'sunbed', 'surfboard', 'paddleboard', 'snorkel', 'wetsuit', 'skis', 'snowboard', 'sled',
    'ice skates', 'skates', 'helmet', 'goggles', 'racket', 'bat', 'golf club', 'golf ball', 'bowling',
    'dartboard', 'fishing rod', 'net', 'anchor', 'sail', 'oar', 'paddle', 'life jacket', 'binoculars',
    'telescope', 'microscope', 'compass', 'tent', 'sleeping bag', 'backpack', 'cooler', 'thermos',
    # Scenes, events and activities
    'portrait', 'selfie', 'group photo', 'family photo', 'wedding', 'bride', 'groom', 'engagement',
    'honeymoon', 'anniversary', 'birthday', 'birthday cake', 'party', 'celebration', 'ceremony',
    'graduation', 'prom', 'reunion', 'festival', 'carnival', 'parade', 'fair', 'funfair', 'circus',
    'concert', 'gig', 'performance', 'show', 'play', 'musical', 'opera', 'ballet', 'dance', 'dancing',
    'singing', 'karaoke', 'band', 'orchestra', 'choir', 'dj', 'nightlife', 'christmas', 'xmas',
    'new year', 'easter', 'halloween', 'thanksgiving', 'diwali', 'holi', 'eid', 'hanukkah', 'midsummer',
    'holiday', 'vacation', 'travel', 'trip', 'road trip', 'sightseeing', 'tour', 'tourism', 'adventure',
    'camping', 'hiking', 'trekking', 'climbing', 'mountaineering', 'cycling', 'biking', 'running',
    'jogging', 'marathon', 'race', 'walking', 'stroll', 'swimming', 'diving', 'snorkeling', 'surfing',
    'sailing', 'rowing', 'kayaking', 'canoeing', 'rafting', 'fishing', 'boating', 'skiing', 'snowboarding',
    'sledding', 'skating', 'ice skating', 'skateboarding', 'rollerblading', 'horse riding', 'riding',
    'golf', 'tennis', 'badminton', 'cricket', 'soccer', 'football', 'rugby', 'basketball', 'baseball',
    'volleyball', 'hockey', 'table tennis', 'bowling', 'boxing', 'karate', 'judo', 'wrestling', 'fencing',
    'archery', 'gymnastics', 'yoga', 'pilates', 'workout', 'exercise', 'fitness', 'stretching',
    'meditation', 'cooking', 'baking', 'grilling', 'gardening', 'planting', 'harvest', 'shopping',
    'reading', 'studying', 'drawing', 'painting', 'crafting', 'knitting', 'sewing', 'pottery', 'woodwork',
    'photography', 'gaming', 'video game', 'playing', 'playtime', 'eating', 'dining', 'toast', 'cheers',
    'sleeping', 'nap', 'cuddle', 'hug', 'kiss', 'laughing', 'smile', 'smiling', 'crying', 'bath time',
    'bedtime', 'storytime', 'homework', 'science fair', 'field trip', 'school play', 'recital', 'sports day',
    'match', 'game', 'tournament', 'competition', 'medal ceremony', 'podium', 'finish line', 'award',
    'exhibition', 'convention', 'conference', 'workshop', 'lecture', 'meeting', 'presentation',
    'interview', 'protest', 'rally', 'march', 'flea market', 'yard sale', 'street food', 'food truck',
    'tea party', 'picnic', 'barbecue', 'bonfire', 'fireworks', 'sunbathing', 'sunset watching',
    'stargazing', 'birdwatching', 'whale watching', 'safari', 'cruise', 'flight', 'takeoff', 'landing',
    'ride', 'carousel', 'zipline', 'bungee', 'paragliding', 'skydiving', 'hot air balloon', 'snow day',
    'beach day', 'pool party', 'water park', 'splash', 'sandcastle', 'snowball', 'snow angel',
    'autumn leaves', 'cherry blossom', 'bloom', 'harvest', 'vintage', 'macro', 'close up', 'abstract',
    'black and white', 'monochrome', 'long exposure', 'bokeh', 'drone shot', 'aerial view', 'street photo',
    'street photography', 'architecture photo', 'interior', 'exterior', 'indoor', 'outdoor', 'indoors',
    'outdoors', 'underwater', 'nighttime', 'daytime', 'golden hour', 'blue hour',
}

EXPLORE_THING_LABELS = frozenset(
    _normalize_token(label)
    for label in (
        _CONCRETE_THINGS | ANIMAL_TAGS | PLANT_TAGS | FLOWER_TAGS | ACTION_TAGS | SCENE_TAGS | WEATHER_TAGS
    )
    if _normalize_token(label) and ' ' not in _normalize_token(label)
)


def is_explore_thing_label(label: str) -> bool:
    """True when ``label`` is something the Things shelf may show: a concrete
    object, animal, food, scene or activity -- never a place, nationality,
    occupation, bare adjective/verb, abstraction, generic low-value word or a
    sensitive/unpleasant label."""
    normalized = _normalize_token(label)
    if not normalized or ' ' in normalized:
        return False
    if normalized in GENERIC_LOW_VALUE_TAGS:
        return False
    if is_sensitive_auto_tag_label(normalized) or place_names.is_geographic_label(normalized):
        return False
    return normalized in EXPLORE_THING_LABELS


def _tag_confidence(row: Dict) -> Dict[str, Tuple[Optional[float], str]]:
    raw = row.get('tagMetadata')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or '[]')
        except Exception:
            raw = []
    out: Dict[str, Tuple[Optional[float], str]] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        tag = _normalize_token(str(item.get('tag') or item.get('label') or ''))
        if not tag:
            continue
        try:
            confidence: Optional[float] = float(item.get('confidence'))
        except Exception:
            confidence = None
        out[tag] = (confidence, str(item.get('source') or ''))
    return out


def explore_thing_tags(row: Dict) -> List[str]:
    """The row's tags that belong on the Things shelf, in stored order, each
    once. Reads the same columns both Explore builders always read (tags +
    objects); tagMetadata, when present, supplies the confidence floor."""
    confidence = _tag_confidence(row)
    terms = parse_tags(row.get('tags', '[]')) + parse_json_list(row.get('objects', '[]'))
    out: List[str] = []
    for term in dict.fromkeys(terms):
        if not is_explore_thing_label(term):
            continue
        found = confidence.get(term)
        if found is not None:
            score, source = found
            if source not in ('user', 'stored') and score is not None and score < EXPLORE_THING_MIN_CONFIDENCE:
                continue
        out.append(term)
    return out


def normalize_key(value: str) -> str:
    """The grouping key shared by both Explore builders (an ASCII-folded,
    lower-cased, alphanumeric-only phrase)."""
    folded = place_names.ascii_fold(value)
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', folded.lower())).strip()


def title(value: str) -> str:
    cleaned = re.sub(r'\s+', ' ', str(value or '').replace('_', ' ')).strip()
    return cleaned.title() if cleaned.islower() else cleaned


def explore_place(row: Dict) -> Optional[Tuple[str, str]]:
    """(key, label) for the row's place card, or None without a place. The
    label is "City, Country" in local spelling ("Malmö, Sweden") and the key
    is its fold, so "Malmoe"/"Malmo"/"Malmö" rows land in ONE group; rows with
    only an address or only coordinates keep the old fallbacks."""
    city = str(row.get('locationCity') or '').strip()
    country = str(row.get('locationCountry') or '').strip()
    address = str(row.get('address') or '').strip()
    latitude = str(row.get('latitude') or '').strip()
    longitude = str(row.get('longitude') or '').strip()
    label = place_names.place_label(city, country) if (city or country) else address
    if not label and latitude and longitude:
        label = f'{latitude[:8]}, {longitude[:8]}'
    key = normalize_key(label)
    if not key:
        return None
    return key, title(label)


def place_keys_for_label(label: str) -> List[str]:
    """Every grouping key a stored summary might have used for this place
    label: the current (local-spelling) key first, then the key of the label
    exactly as given, so a filter still finds groups built before the local
    spelling existed ("malmoe sweden")."""
    text = str(label or '').strip()
    parts = [part.strip() for part in text.split(',')]
    keys = [normalize_key(text)]
    if len(parts) >= 2:
        city, country = parts[0], parts[-1]
        keys.insert(0, normalize_key(place_names.place_label(city, country)))
        for variant in place_names.place_name_variants(city, country):
            keys.append(normalize_key(f'{variant}, {country}'))
    return [k for k in dict.fromkeys(keys) if k]


def clean_summary(summary: Dict) -> Dict:
    """A stored Explore summary with junk Things dropped and Place labels in
    local spelling: what /explore serves, so a summary built before these
    rules is corrected on read rather than rebuilt."""
    places = []
    for item in summary.get('places') or []:
        label = str(item.get('label') or '')
        parts = [part.strip() for part in label.split(',')]
        if len(parts) >= 2:
            label = title(place_names.place_label(parts[0], parts[-1]))
        places.append(dict(item, label=label))
    things = [item for item in summary.get('things') or [] if is_explore_thing_label(str(item.get('label') or ''))]
    return {'places': places, 'things': things}
