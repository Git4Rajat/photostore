# Face outcome audit — 2026-10-04

Read-only Entra-authenticated audit of library `owner` in `photometadata`.
All 119,058 projected rows were exhausted, then the latest 10,000 active photos
were selected by parsed `uploadDate` (not filename, Table order or update time).
Selected upload interval: **2026-10-03 13:06:42.093078 to 21:21:31.685317 UTC**.
No deleted rows or missing upload dates were encountered. Second scan collected
persisted face diagnostics for every selected row. Repeat audit reproduced counts.
These are live nontransactional reads: concurrent writes can change outcomes.

| Scope/outcome | Photos |
| --- | ---: |
| Entire library, `done` | 116,158 |
| Entire library, `no_data` | 2,900 |
| Latest 10,000, `done` | 9,776 |
| Latest 10,000, `no_data` | **224 (2.24%)** |
| Latest no-data with zero detector hits | 137 |
| Latest no-data with positive detector hits | **87** |
| Positive hits, landmark failure | 67 |
| Positive hits, backend quality gate rejection | 20 |

All 224 latest no-data outcomes had ipworker provenance and no failure stage.
82 reported one raw face and five reported two. `done` is a persisted status,
not an assertion every visible face was detected or assigned correctly.

## Confirmed algorithm gap

The detector can succeed while landmark extraction, alignment or embedding loses
all candidates. The old result carried a positive `rawFaceCount` and optional
`filteredReason`, but no failure stage. Backend then published `no_data`.
Backend quality rejection likewise lost accepted candidates without making
no-data distinct from a genuine detector no-hit. Forced reconciliation also
treated those incomplete results as a replacement set, risking prior source
faces/curation. The audit confirms this misclassification for **87 photos**, not
that all 224 had the same cause.

Local fixes classify incomplete/filtered passes as failed, retain prior faces
and counts, avoid forced deletion, and record explicit bounded diagnostics.
Malformed/nonfinite model outputs fail instead of silently returning zero.
No confidence, quality, alignment or clustering thresholds were relaxed.
Existing records are not automatically repaired or requeued by these changes.

## Representative original-image checks

Five originals were downloaded read-only for local review. All contain visible
faces. Three are among detector-zero photos; two exercise landmark/quality paths.
Local bundled YOLO model: CPU only, single-thread sessions/OpenCV; original and
actual preview pipelines compared. Score cutoff **0.35** remains unchanged;
0.20 was used only as an offline diagnostic, not a production change.

| Sample | Oriented dimensions | Maximum score | Faces at 0.35 | Faces at diagnostic 0.20 |
| --- | --- | ---: | ---: | ---: |
| 109109 | 439×600 | 0.271423 | 0 | 1 |
| 109200 | 403×594 | 0.184743 | 0 | 0 |
| 109468 | 300×412 | 0.294850 | 0 | 1 |
| 109171 | 610×853 | 0.758622 | 1 | 1 |
| 109466 original | 1993×3000 | 0.368069 | 1 | 1 |
| 109466 preview | 1361×2048 | 0.356489 | 1 | 1 |

Preview preserved the first four inputs byte-for-byte. Thus preview resizing
does not explain their detector misses. Model scores below 0.35 explain these
three reproduced detector-zero cases, despite visible faces. This is a recall
limitation, not proof the other 134 have identical causes. Lowering the threshold
alone cannot recover the 0.184743 sample at 0.20 and needs false-positive and
identity-safety evaluation before rollout. Local full landmark replay was not
available because the MediaPipe task asset was not installed; no model downloaded.

Observed live ipworker: revision 0000026, image tag 20261003-205841, Running.
The local bundled-model replay is not an attestation of the deployed image's
weight checksum. No production writes, reprocessing, restart or deployment.

## Reproducibility

[backend/scripts/audit_face_outcomes.py](../backend/scripts/audit_face_outcomes.py)
provides bounded latest-upload selection, request timeouts, 15-minute audit
budget and a complete-scan requirement. It uses Azure CLI identity, no account
keys. Azure Tables have no `ORDER BY`: first-page or filename samples must not
be presented as counts of the latest uploads. JSON output contains limited
filename/diagnostic samples and should be handled as private library data.

The new instrumentation is documented in
[ipworker-performance-metrics.md](ipworker-performance-metrics.md).
Full backend validation: **1,486 passed**, 8 existing test-key warnings.
No test certifies 10,000 genuinely new photos/hour per replica.