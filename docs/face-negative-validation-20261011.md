# Face negative validation — 2026-10-11

**Release gate failed.** Experimental recovery accepts additional animal heads as
human faces. It is now disabled by default; no deployment or library reprocessing
was performed. The existing pipeline also accepts some animal heads.

## Corpus and review

The suite contains 580 cases, including **328 distinct real original photos**.

| Group | Cases | Preparation |
| --- | ---: | --- |
| Real scenes | 234 | Reviewed selection from 420 Places365 validation images |
| Real pets | 94 | Reviewed selection from 96 Oxford-IIIT cat/dog images, covering 34 sampled breeds |
| Rotated copies | 64 | 40 scenes and 24 pets, rotated 90 degrees |
| Blurred/compressed copies | 64 | Same parents, Gaussian blur and JPEG quality 25 |
| Low-resolution copies | 64 | Same parents, reduced to at most 96 pixels and resized |
| Procedural fixtures | 48 | Outlets, vehicle fronts, repeated spots/text and noise |
| Earlier user background crops | 12 | Clothing, hair, skin and scenery |

Scene and pet images were visually reviewed **before inference**. Images with
visible human faces, human faces depicted in posters/artwork, or ambiguous people
were excluded conservatively. All accepted-output originals were subsequently
reviewed at larger size with detector boxes; failures are animal heads, not
overlooked human faces. Derivatives inherit their parent's negative label and
are counted separately, not as independent photos.

Sources: [Places365 development kit](https://github.com/zhoubolei/places_devkit),
[Oxford-IIIT Pet Dataset](https://www.robots.ox.ac.uk/~vgg/data/pets/).
Public image content stays outside the repository. Download prefixes and selected
archive member names remain local for provenance.

## Paired results

An accepted false face means the complete processing path produced an embedding
that passed the backend storage quality gate, not merely an intermediate detector
box. Runs use the same images and models. The baseline is a saved copy of the
deployed processor; recovery was explicitly enabled for candidate evaluation.

| Group | Cases | Baseline: accepted false faces | Recovery enabled: accepted false faces |
| --- | ---: | ---: | ---: |
| Real scenes | 234 | 0 | 0 |
| Real pets | 94 | 14 | 17 |
| Rotated copies | 64 | 1 | 2 |
| Blurred/compressed copies | 64 | 2 | 4 |
| Low-resolution copies | 64 | 3 | 4 |
| Procedural fixtures | 48 | 0 | 0 |
| Background crops | 12 | 0 | 0 |
| **Total** | **580** | **20** | **27** |

Each accepted case produced one quality-accepted embedding. Recovery adds **seven
false-positive cases**: three distinct original pet photos and four transformed
pet images. No baseline false positive disappeared. Both negative-test commands
correctly returned exit code 1. There were **zero inference/runtime errors**;
unavailable models were not counted as successful negative outcomes.

The original pet photos newly accepted by recovery are:

| File | Baseline outcome | Recovery confidence |
| --- | --- | ---: |
| `american_bulldog_174.jpg` | Candidate failed landmark extraction | 0.696459 |
| `miniature_pinscher_119.jpg` | No detector hit | 0.667564 |
| `german_shorthaired_20.jpg` | No detector hit | 0.638121 |

The miniature pinscher recovery box is over an ear, yet it yielded a
landmark-aligned embedding. Detector confidence and landmark geometry are
insufficient to establish that a crop is a human face. Transformed additions are
`lowres-42.jpg`, `blurred-47.jpg`, `blurred-52.jpg` and `rotated-62.jpg`; their
parent filenames are in the manifest.

Intermediate boxes were also recorded separately. Among the 234 scene originals,
the baseline emitted candidates in four images and recovery-enabled processing
in 15; all were rejected downstream. One procedural outlet produced a recovery
candidate, also rejected downstream. Zero accepted faces in these groups does
not mean zero intermediate false detector boxes.

## Safety change and positive checks

`IPWORKER_FACE_RECOVERY_ENABLED` defaults to `0`. The current processor with
recovery disabled was replayed on all 580 cases: its accepted outputs and
float32 embedding fingerprints match the baseline exactly. This prevents new
recovery false positives but leaves the baseline's 20 negative-case acceptances.

The runner's `--enable-recovery` flag enables the experiment for local evaluation;
it is not a release recommendation. The 146-photo positive replay was repeated
with the experiment enabled. It still recovers 65 of 69 previously empty photos,
and all 77 existing usable results retain identical boxes, confidence, alignment
and float32 embedding fingerprints. That recall improvement cannot justify
enabling recovery while the negative gate fails. Four landmark-zero photos and
45 partial landmark outcomes remain unresolved.

The targeted regression suite passed **205 tests**, including a new check that
disabled recovery never invokes the fallback. Passing those tests is separate
from the failed real-image negative gate.

## Reproduction and limits

No Azure face rows, people, queues, photo metadata or deployments were changed.
Model libraries match the worker's pinned versions; inference ran on macOS,
not a deployed Linux revision. Live quality settings were minimum confidence
0.30, low-confidence cutoff 0.36, maximum low-confidence area ratio 0.08 and
maximum low-confidence side ratio 0.42.

Assets and reports are in `/tmp/photostore-face-validation/broad-negative`:

- `review.json`: accepted/excluded labels and source image checksums.
- `manifest-all.json`: all cases, source members, parent links and groups.
- `baseline-all.json`, `candidate-all.json`, `default-all.json`: per-case results,
  detector boxes, embedding fingerprints, errors and configuration.
- `summary.json`: paired aggregates and positive preservation checks.
- `accepted-negatives.jpg`: larger review of the 17 accepted original pet images.

Example candidate negative gate from the repository root:

```bash
FACE_MIN_STORE_CONFIDENCE=0.30 FACE_LOW_CONFIDENCE_REJECT_BELOW=0.36 \
  .venv-photostore-tools/bin/python backend/scripts/replay_face_pipeline.py \
  --manifest /tmp/photostore-face-validation/broad-negative/manifest-all.json \
  --models frontend/public/models/browser-ai/models \
  --landmarker /tmp/photostore-face-landmarker.task \
  --enable-recovery --expect-empty \
  --output /tmp/photostore-face-validation/broad-negative/candidate-all.json
```

The runner returns nonzero for any accepted face or inference error. Reports
omit raw embedding vectors and image content. These curated, pet-heavy samples
are not a random sample of production uploads; no production false-positive rate
or guarantee of zero false positives can be inferred.

Before enabling recovery, validate a human-versus-animal check against both
negative cohorts and human-face positives, including mixed human/pet photos.
Use a separate held-out set for further tuning. Existing animal-head false
positives also need remediation; the default-off switch prevents the experimental
path from adding more.
