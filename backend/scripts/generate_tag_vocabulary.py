"""One-time offline generator that expands backend/../frontend's CLIP zero-shot
tag vocabulary (frontend/public/models/browser-ai/vocab/tag-vocabulary.v1.json)
from ~283 curated words to ~10,000 common nouns.

NOT run by the running service -- a dev-time data-generation script, same
category as generate_common_word_embeddings.py. Re-run only if the target
vocabulary size or source lists change.

Candidate nouns come from WordNet noun lemmas (nltk), ranked by real-world
frequency via wordfreq.zipf_frequency so the result skews toward words a
photo-tagger would plausibly need ("jet", "kayak", "lantern") rather than
WordNet's long tail of obscure/technical synsets ("aardwolf", "zymurgy") --
same reasoning generate_common_word_embeddings.py's docstring gives for using
a curated list instead of a full dictionary, just automated at 10k scale
instead of hand-curated at ~500.

New additions are restricted to single-word nouns. WordNet's multi-word noun
*phrases* ("abandoned person", "absolute majority", "jazz group") vastly
outnumber genuinely common single words at the same frequency band -- letting
them compete for slots pushed plainly common words like "jet" (a top-3,200
single word) past the 10k cutoff in an earlier version of this script.
Multi-word entries from the existing curated 283-word list (e.g. "night sky")
are kept as-is since those were hand-picked, just not used as a source for
new ones.

A curated profanity/slur blocklist is applied before truncation: this app
auto-tags family/wedding/kid photos, so a frequency-ranked word list pulling
in an offensive word as a real photo tag is a genuine product-safety risk,
not just a quality nitpick.

Usage (from backend/, with nltk + wordfreq installed -- see
requirements-tools.txt or `pip install nltk wordfreq`):
    python scripts/generate_tag_vocabulary.py [--target 10000]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search_utils import _normalize_token  # noqa: E402

VOCAB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    '..', 'frontend', 'public', 'models', 'browser-ai', 'vocab', 'tag-vocabulary.v1.json',
)

# Small curated blocklist of profanity/slurs to exclude from an auto-generated,
# frequency-ranked word list before it ever reaches a family photo library.
# Deliberately not exhaustive -- a full slur list isn't necessary here, this
# only needs to catch the common cases a frequency-based noun list would pull
# in (WordNet includes plenty of vulgar/offensive nouns as ordinary lemmas).
PROFANITY_BLOCKLIST = {
    'nigger', 'nigga', 'chink', 'spic', 'kike', 'gook', 'wetback', 'faggot',
    'fag', 'dyke', 'tranny', 'retard', 'cripple', 'whore', 'slut', 'bitch',
    'cunt', 'pussy', 'dick', 'cock', 'penis', 'vagina', 'boob', 'tit',
    'asshole', 'ass', 'bastard', 'shit', 'crap', 'damn', 'hell', 'piss',
    'rape', 'rapist', 'molester', 'pedophile', 'incest', 'porn', 'sex',
    'orgasm', 'semen', 'sperm', 'masturbation', 'genitalia', 'scrotum',
    'testicle', 'nipple', 'buttock', 'anus', 'turd', 'douchebag', 'douche',
    'skank', 'hooker', 'prostitute', 'stripper', 'junkie', 'crackhead',
    'druggie', 'terrorist', 'suicide', 'corpse', 'cadaver', 'gore',
}

MIN_LABEL_LENGTH = 2
MAX_LABEL_LENGTH = 24


def _load_existing_labels() -> list[str]:
    try:
        with open(VOCAB_PATH, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        return [str(label) for label in (payload.get('labels') or []) if str(label or '').strip()]
    except Exception:
        return []


def _candidate_nouns() -> set[str]:
    import nltk
    for resource in ('corpora/wordnet', 'corpora/omw-1.4'):
        try:
            nltk.data.find(resource)
        except LookupError:
            nltk.download(resource.split('/')[-1], quiet=True)
    from nltk.corpus import wordnet as wn

    candidates: set[str] = set()
    for synset in wn.all_synsets('n'):
        for lemma in synset.lemma_names():
            word = lemma.replace('_', ' ').lower()
            if not word.replace(' ', '').replace('-', '').isalpha():
                continue
            if len(word) < MIN_LABEL_LENGTH or len(word) > MAX_LABEL_LENGTH:
                continue
            if ' ' in word or '-' in word:
                # Multi-word phrases (see module docstring) -- and hyphenated
                # lemmas like "A-bomb"/"well-being" would look single-word
                # here but _normalize_token() later splits on the hyphen and
                # rejoins with a space ("a bomb"), turning them back into the
                # same multi-word junk this filter exists to keep out.
                continue
            candidates.add(word)
    return candidates


def build_vocabulary(target: int) -> list[str]:
    import wordfreq

    existing = {_normalize_token(label) for label in _load_existing_labels() if _normalize_token(label)}
    candidates = _candidate_nouns() | existing

    blocked = {w for w in candidates if any(bad in w.split(' ') for bad in PROFANITY_BLOCKLIST)}
    candidates -= blocked

    ranked = sorted(
        candidates,
        key=lambda word: (word not in existing, -wordfreq.zipf_frequency(word, 'en'), word),
    )

    selected: list[str] = []
    seen: set[str] = set()
    for word in ranked:
        normalized = _normalize_token(word)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        selected.append(normalized)
        if len(selected) >= target:
            break

    missing_existing = existing - seen
    if missing_existing:
        selected.extend(sorted(missing_existing))

    return sorted(selected)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', type=int, default=10000)
    args = parser.parse_args()

    existing = _load_existing_labels()
    labels = build_vocabulary(args.target)
    print(f'Existing vocabulary: {len(existing)} labels')
    print(f'Generated vocabulary: {len(labels)} labels (target {args.target})')

    missing_from_new = set(_normalize_token(w) for w in existing) - set(labels)
    if missing_from_new:
        print(f'WARNING: {len(missing_from_new)} existing labels dropped: {sorted(missing_from_new)[:20]}')

    payload = {
        'version': 'clip-vit-base-patch32:openai:vocab-v2',
        'labels': labels,
    }
    with open(VOCAB_PATH, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(',', ':'))
    print(f'Wrote {len(labels)} labels to {VOCAB_PATH}')


if __name__ == '__main__':
    main()
