"""One-time offline generator for backend/data/common_word_embeddings.npz.

This is NOT run by the running service -- it's a dev-time data-generation
script (run once, or re-run only if the vocabulary or CLIP checkpoint
changes), the same way Apple's on-device NLEmbedding ships as a precomputed,
static word-vector table rather than something devices compute at runtime
(see WWDC19 "Advances in Natural Language Framework"). The output lets
search_photos() (backend role, which never loads real CLIP -- see
docs/ipworker-architecture.md) look up a *fixed* vocabulary word's embedding
via a plain numpy array index, with zero live model inference, and compare
it against a user's ipworker-computed tag-embedding cache. Query words
outside this fixed vocabulary simply get no expansion (graceful
degradation), same as Apple's own fixed-taxonomy tagger degrading to its own
NLEmbedding fallback only for words the tagger's vocabulary knows about.

Usage: run from backend/ with the dev tooling venv active (needs
torch + open_clip, same checkpoint vision_utils.py uses):
    python scripts/generate_common_word_embeddings.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import open_clip
import torch

from search_utils import _normalize_token

# Matches vision_utils.py's _load_model() defaults exactly -- the checkpoint
# must be identical for these vectors to share a space with query/tag
# embeddings computed at runtime by ipworker.
MODEL_NAME = os.getenv('OPENCLIP_MODEL', 'ViT-B-32-quickgelu')
MODEL_PRETRAINED = os.getenv('OPENCLIP_PRETRAINED', 'openai')

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'common_word_embeddings.npz')

# A bounded, curated vocabulary of common photo-search-relevant nouns/
# adjectives/verbs -- mirrors Apple ANSA's own "fixed taxonomy" approach
# (see machinelearning.apple.com/research/on-device-scene-analysis) rather
# than trying to embed an entire dictionary (most of which -- "aardwolf",
# "zymurgy" -- no one would ever search a photo library for). Organized by
# category for maintainability; duplicates across categories are fine, the
# script dedupes before encoding.
VOCABULARY = {
    'animals': [
        'dog', 'puppy', 'cat', 'kitten', 'bird', 'fish', 'horse', 'cow', 'pig',
        'sheep', 'goat', 'chicken', 'duck', 'rabbit', 'squirrel', 'deer', 'fox',
        'bear', 'wolf', 'lion', 'tiger', 'elephant', 'monkey', 'giraffe',
        'zebra', 'penguin', 'owl', 'eagle', 'parrot', 'butterfly', 'bee',
        'spider', 'snake', 'turtle', 'frog', 'dolphin', 'whale', 'shark',
        'seal', 'otter', 'hamster', 'guinea pig', 'lizard', 'crab', 'lobster',
        'jellyfish', 'octopus', 'pony', 'donkey', 'llama', 'camel', 'kangaroo',
        'koala', 'panda', 'raccoon', 'hedgehog', 'peacock', 'flamingo', 'swan',
        'goose', 'pigeon', 'crow', 'hawk', 'seagull',
    ],
    'nature_outdoor': [
        'tree', 'forest', 'mountain', 'hill', 'valley', 'river', 'lake',
        'ocean', 'sea', 'beach', 'sand', 'desert', 'field', 'meadow', 'garden',
        'park', 'flower', 'rose', 'sunflower', 'tulip', 'daisy', 'grass',
        'leaf', 'branch', 'root', 'sky', 'cloud', 'sun', 'moon', 'star',
        'rainbow', 'sunset', 'sunrise', 'thunderstorm', 'lightning', 'rain',
        'snow', 'ice', 'fog', 'wind', 'waterfall', 'stream', 'pond', 'island',
        'cliff', 'cave', 'rock', 'stone', 'volcano', 'glacier', 'jungle',
        'wetland', 'swamp', 'prairie', 'canyon', 'reef', 'coral', 'shell',
        'pebble', 'dune', 'meadowland', 'orchard', 'vineyard', 'farm', 'barn',
    ],
    'food': [
        'pizza', 'burger', 'sandwich', 'salad', 'soup', 'pasta', 'noodles',
        'rice', 'bread', 'cake', 'cookie', 'donut', 'pie', 'pancake', 'waffle',
        'cheese', 'egg', 'bacon', 'sausage', 'steak', 'chicken breast', 'fish fillet',
        'shrimp', 'sushi', 'taco', 'burrito', 'curry', 'dumpling', 'pretzel',
        'popcorn', 'chocolate', 'candy', 'ice cream', 'coffee', 'tea', 'juice',
        'wine', 'beer', 'cocktail', 'apple', 'banana', 'orange', 'grape',
        'strawberry', 'watermelon', 'pineapple', 'mango', 'lemon', 'peach',
        'cherry', 'avocado', 'tomato', 'potato', 'carrot', 'broccoli',
        'onion', 'pepper', 'mushroom', 'corn', 'cucumber', 'lettuce', 'garlic',
    ],
    'vehicles': [
        'car', 'truck', 'bus', 'motorcycle', 'bicycle', 'scooter', 'van',
        'train', 'subway', 'tram', 'airplane', 'helicopter', 'boat', 'ship',
        'yacht', 'canoe', 'kayak', 'sailboat', 'ferry', 'taxi', 'ambulance',
        'firetruck', 'tractor', 'jeep', 'convertible', 'sports car', 'suv',
        'skateboard', 'rollerblades', 'wheelchair', 'stroller', 'wagon',
        'trailer', 'caravan', 'rocket', 'submarine', 'hot air balloon',
    ],
    'buildings_architecture': [
        'house', 'building', 'skyscraper', 'apartment', 'castle', 'palace',
        'church', 'temple', 'mosque', 'cathedral', 'tower', 'bridge', 'wall',
        'fence', 'gate', 'door', 'window', 'roof', 'chimney', 'staircase',
        'balcony', 'porch', 'garage', 'shed', 'warehouse', 'factory',
        'stadium', 'arena', 'museum', 'library', 'school', 'hospital',
        'hotel', 'restaurant', 'cafe', 'shop', 'market', 'mall', 'airport',
        'station', 'lighthouse', 'windmill', 'cabin', 'cottage', 'mansion',
        'skyline', 'monument', 'statue', 'fountain', 'plaza', 'street',
        'alley', 'sidewalk', 'road', 'highway', 'tunnel', 'pier', 'dock',
        'harbor', 'marina',
    ],
    'clothing': [
        'shirt', 'tshirt', 'dress', 'skirt', 'pants', 'jeans', 'shorts',
        'jacket', 'coat', 'sweater', 'hoodie', 'suit', 'tie', 'scarf',
        'hat', 'cap', 'helmet', 'gloves', 'socks', 'shoes', 'boots',
        'sandals', 'sneakers', 'heels', 'belt', 'bag', 'backpack', 'purse',
        'wallet', 'sunglasses', 'watch', 'jewelry', 'necklace', 'bracelet',
        'ring', 'earrings', 'crown', 'costume', 'uniform', 'apron', 'robe',
        'pajamas', 'swimsuit', 'bikini', 'wedding dress', 'tuxedo', 'veil',
    ],
    'furniture_household': [
        'chair', 'table', 'sofa', 'couch', 'bed', 'desk', 'shelf', 'bookshelf',
        'cabinet', 'drawer', 'wardrobe', 'mirror', 'lamp', 'candle',
        'clock', 'vase', 'pillow', 'blanket', 'curtain', 'rug', 'carpet',
        'painting', 'picture frame', 'television', 'computer', 'laptop',
        'phone', 'camera', 'keyboard', 'mouse', 'printer', 'refrigerator',
        'oven', 'stove', 'microwave', 'sink', 'bathtub', 'shower', 'toilet',
        'towel', 'plate', 'bowl', 'cup', 'mug', 'glass', 'bottle', 'jar',
        'basket', 'box', 'suitcase', 'umbrella', 'toy', 'doll', 'puzzle',
        'book', 'newspaper', 'magazine', 'letter', 'envelope', 'pen',
        'pencil', 'notebook',
    ],
    'sports_activities': [
        'soccer', 'football', 'basketball', 'baseball', 'tennis', 'golf',
        'volleyball', 'hockey', 'rugby', 'cricket', 'swimming', 'diving',
        'surfing', 'sailing', 'skiing', 'snowboarding', 'skating',
        'skateboarding', 'climbing', 'hiking', 'running', 'jogging',
        'cycling', 'yoga', 'gym', 'weightlifting', 'boxing', 'wrestling',
        'martial arts', 'dancing', 'gymnastics', 'archery', 'fishing',
        'hunting', 'camping', 'picnic', 'barbecue', 'fireworks', 'parade',
        'concert', 'festival', 'carnival', 'circus', 'theater', 'movie',
        'game', 'chess', 'cards', 'painting activity', 'drawing', 'photography',
        'singing', 'playing guitar', 'playing piano', 'reading',
    ],
    'people_events': [
        'baby', 'child', 'kid', 'teenager', 'adult', 'family', 'couple',
        'friends', 'crowd', 'wedding', 'birthday', 'party', 'graduation',
        'funeral', 'reunion', 'anniversary', 'christmas', 'halloween',
        'thanksgiving', 'easter', 'holiday', 'vacation', 'trip', 'travel',
        'selfie', 'portrait', 'group photo', 'smiling', 'laughing', 'crying',
        'sleeping', 'dancing people', 'hugging', 'kissing', 'waving',
        'pointing', 'jumping', 'sitting', 'standing', 'walking',
    ],
    'weather_time': [
        'winter', 'summer', 'spring', 'autumn', 'fall', 'morning',
        'afternoon', 'evening', 'night', 'midnight', 'dawn', 'dusk',
        'twilight', 'storm', 'hurricane', 'tornado', 'drought', 'flood',
        'heatwave', 'frost', 'hail', 'blizzard',
    ],
    'documents_text': [
        'receipt', 'invoice', 'passport', 'license', 'certificate', 'ticket',
        'menu', 'sign', 'poster', 'billboard', 'label', 'document', 'form',
        'contract', 'card', 'badge', 'stamp', 'coin', 'money', 'currency',
        'map', 'chart', 'graph', 'screenshot', 'whiteboard', 'blackboard',
    ],
}


def main() -> None:
    words = sorted({_normalize_token(w) for group in VOCABULARY.values() for w in group if _normalize_token(w)})
    print(f'Encoding {len(words)} vocabulary words with {MODEL_NAME}/{MODEL_PRETRAINED}...')

    model, _, _ = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=MODEL_PRETRAINED)
    model.eval()
    tokenizer = open_clip.get_tokenizer(MODEL_NAME)

    tokens = tokenizer(words)
    with torch.no_grad():
        features = model.encode_text(tokens)
        features = features / features.norm(dim=-1, keepdim=True)
    embeddings = features.cpu().numpy().astype(np.float32)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    np.savez_compressed(
        OUTPUT_PATH,
        words=np.asarray(words),
        embeddings=embeddings,
        model_name=np.asarray([MODEL_NAME]),
        model_pretrained=np.asarray([MODEL_PRETRAINED]),
    )
    print(f'Wrote {len(words)} word embeddings ({embeddings.shape[1]}d) to {OUTPUT_PATH}')


if __name__ == '__main__':
    main()
