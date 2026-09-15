"""Measure real CLIP tag cosine-similarity scores to (re)calibrate
search_utils.py's AI_TAG_MIN_CONFIDENCE / GENERIC_TAG_MIN_CONFIDENCE /
SEMANTIC_PREDICTION_TAG_MIN_SCORE and ipwork_vision.py's
PERSON_SCORE_THRESHOLD.

Read-only, local files only, no writes and no cloud calls. Runs the new
process_vision() cosine-similarity scoring (raw cosine similarity per label,
no vocab-wide softmax -- see ipwork_vision.py) against real photos passed on
the command line and prints their top-K label/score pairs, so thresholds can
be picked from the actual score distribution instead of reused from the old
softmax scale (where they meant something completely different -- softmax
probability mass vs. an independent per-label cosine similarity).

Usage (from backend/, with the ipworker venv active -- needs torch/open_clip):
    python scripts/calibrate_tag_confidence_thresholds.py photo1.jpg photo2.CR3 ...
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ipwork_vision  # noqa: E402


def main() -> None:
    paths = sys.argv[1:]
    if not paths:
        raise SystemExit('Usage: calibrate_tag_confidence_thresholds.py <photo> [photo ...]')

    for path in paths:
        with open(path, 'rb') as handle:
            image_bytes = handle.read()
        result = ipwork_vision.process_vision('calibration', os.path.basename(path), image_bytes)
        print(f'\n=== {path} ===')
        if not result or not result.get('hasData'):
            print(f'  no data: {result.get("error") if result else "unknown"}')
            continue
        for prediction in result['predictions']:
            marker = ' <- person label' if prediction['label'] in ipwork_vision.PERSON_LABELS else ''
            print(f'  {prediction["score"]:.4f}  {prediction["label"]}{marker}')


if __name__ == '__main__':
    main()
