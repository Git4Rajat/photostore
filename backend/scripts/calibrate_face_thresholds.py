#!/usr/bin/env python3
"""Measure the real face-embedding similarity distribution to calibrate clustering.

Read-only. Pulls clusterable face embeddings for one user out of Azure Table
Storage and reports where same-person vs different-person pairs *actually* sit in
cosine-similarity space, so the DBSCAN ``eps`` and the match/assign thresholds can
be set from data instead of guessed. Makes no writes.

Signals produced:
  * all-pairs similarity distribution        -- the background (mostly different-person) mass
  * per-face nearest-neighbour similarity     -- where each face's closest match sits (~ same-person)
  * SAME-PHOTO pair similarities              -- near-certain different-person negatives => false-merge ceiling
  * top cross-photo pairs                      -- likely same-person; eyeball them to sanity-check
  * fragmentation under the current eps vs a data-derived eps

Auth: DefaultAzureCredential (uses your ``az login``). The account must have
key-auth disabled or not; either way this uses AAD, so your identity needs the
``Storage Table Data Reader`` role on the account.

Usage:
  STORAGE_ACCOUNT_NAME=ownphotostoreywgttvrae27 \
    python scripts/calibrate_face_thresholds.py owner
  # options: --table photofaces --version <embeddingVersion> --top 30 --suspicious 0.60
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

try:
    import numpy as np
except Exception:  # pragma: no cover
    print("ERROR: numpy is required (pip install numpy)", file=sys.stderr)
    raise SystemExit(2)

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential


# ---- field parsing (mirrors backend/app.py so we analyse what clustering sees) ----

def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _embedding(face) -> list:
    try:
        emb = json.loads(face.get("embedding", "[]") or "[]")
        return emb if isinstance(emb, list) else []
    except Exception:
        return []


def _version(face) -> str:
    return str(face.get("embeddingVersion") or face.get("modelTaxonomyVersion") or "").strip()


def _is_rejected(face) -> bool:
    return _coerce_bool(face.get("rejected", False)) or str(face.get("reviewStatus") or "").lower() == "rejected"


def _is_confirmed(face) -> bool:
    return _coerce_bool(face.get("confirmedByUser", False)) or str(face.get("reviewStatus") or "").lower() == "confirmed"


def _is_clusterable(face, suspicious_conf: float) -> bool:
    if _is_rejected(face):
        return False
    if _is_confirmed(face):
        return True
    if str(face.get("reviewStatus") or "").lower() == "suspicious":
        return False
    conf = face.get("confidence")
    if conf is None or str(conf).strip() == "":
        return True
    try:
        return float(conf) >= suspicious_conf
    except Exception:
        return False


# ---- reporting helpers ----

def _pct(arr, ps):
    if len(arr) == 0:
        return {p: float("nan") for p in ps}
    return {p: float(np.percentile(arr, p)) for p in ps}


def _hist(arr, lo=0.0, hi=1.0, bins=20, width=48, label="value"):
    if len(arr) == 0:
        print(f"  (no {label} samples)")
        return
    counts, edges = np.histogram(arr, bins=bins, range=(lo, hi))
    peak = max(1, counts.max())
    print(f"  {label}  (n={len(arr)})   [each bar scaled to peak]")
    for i in range(bins):
        bar = "#" * int(round(width * counts[i] / peak))
        print(f"   {edges[i]:+.3f}..{edges[i+1]:+.3f} | {counts[i]:6d} {bar}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("user_id", help="library / user partition key (single-owner deploys use 'owner')")
    ap.add_argument("--account", default=os.getenv("STORAGE_ACCOUNT_NAME", ""), help="storage account name")
    ap.add_argument("--table", default=os.getenv("FACE_TABLE", "photofaces"))
    ap.add_argument("--version", default="", help="restrict to one embeddingVersion (default: the dominant one)")
    ap.add_argument("--suspicious", type=float, default=float(os.getenv("SUSPICIOUS_FACE_CONFIDENCE", "0.60")))
    ap.add_argument("--top", type=int, default=30, help="how many top cross-photo pairs to print")
    ap.add_argument("--eps", type=float, default=0.03, help="current DBSCAN eps to compare against")
    args = ap.parse_args()

    if not args.account:
        print("ERROR: pass --account or set STORAGE_ACCOUNT_NAME", file=sys.stderr)
        return 2

    endpoint = f"https://{args.account}.table.core.windows.net"
    cred = DefaultAzureCredential()
    svc = TableServiceClient(endpoint=endpoint, credential=cred)
    table = svc.get_table_client(args.table)

    pk = args.user_id.replace("'", "''")
    select = ["RowKey", "embedding", "embeddingVersion", "modelTaxonomyVersion",
              "filename", "confidence", "rejected", "reviewStatus", "confirmedByUser", "personId"]
    rows = list(table.query_entities(f"PartitionKey eq '{pk}'", select=select))
    print(f"Fetched {len(rows)} face rows for user '{args.user_id}' from {args.account}/{args.table}\n")
    if not rows:
        print("No faces. Wrong account/user, or processing hasn't stored faces yet.")
        return 1

    ver_counts = Counter(_version(r) for r in rows)
    print("embeddingVersion breakdown (all rows):")
    for v, c in ver_counts.most_common():
        print(f"   {v or '(none)':40s} {c}")
    print()

    # Keep only clusterable faces with a real embedding, then focus on one version/dim.
    kept = []
    for r in rows:
        if not _is_clusterable(r, args.suspicious):
            continue
        emb = _embedding(r)
        if not emb:
            continue
        kept.append((r, emb, _version(r), len(emb)))

    target_version = args.version.strip()
    if not target_version:
        dim_ver_counts = Counter((v, d) for _, _, v, d in kept)
        if not dim_ver_counts:
            print("No clusterable faces with embeddings.")
            return 1
        (target_version, target_dim), _ = dim_ver_counts.most_common(1)[0]
    else:
        dims = Counter(d for _, _, v, d in kept if v == target_version)
        target_dim = dims.most_common(1)[0][0] if dims else 0

    group = [(r, emb) for (r, emb, v, d) in kept if v == target_version and d == target_dim]
    print(f"Analysing {len(group)} clusterable faces at version='{target_version}' dim={target_dim} "
          f"(confidence >= {args.suspicious}).\n")
    if len(group) < 2:
        print("Need >= 2 faces to compare. Nothing to analyse.")
        return 1

    face_ids = [str(r.get("RowKey")) for r, _ in group]
    filenames = [str(r.get("filename") or "") for r, _ in group]
    person_ids = [str(r.get("personId") or "") for r, _ in group]

    X = np.asarray([emb for _, emb in group], dtype=np.float64)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
    N = X.shape[0]
    S = np.clip(X @ X.T, -1.0, 1.0)
    np.fill_diagonal(S, -np.inf)  # exclude self

    n_photos = len(set(filenames))
    print(f"Faces: {N}   distinct photos: {n_photos}   faces/photo: {N / max(1, n_photos):.2f}")
    n_persons = len([p for p in set(person_ids) if p])
    singletons = sum(1 for p, c in Counter(person_ids).items() if p and c == 1)
    print(f"Current person clusters: {n_persons}   of which singletons: {singletons}\n")

    iu = np.triu_indices(N, k=1)
    all_pairs = S[iu]
    all_pairs = all_pairs[np.isfinite(all_pairs)]

    nn = S.max(axis=1)  # each face's closest neighbour
    nn = nn[np.isfinite(nn)]

    # SAME-PHOTO pairs = two faces in one image = near-certain DIFFERENT people (negatives).
    same_photo, cross_photo_idx = [], []
    for a, b in zip(*iu):
        if not np.isfinite(S[a, b]):
            continue
        if filenames[a] and filenames[a] == filenames[b]:
            same_photo.append(S[a, b])
        else:
            cross_photo_idx.append((S[a, b], a, b))
    same_photo = np.asarray(same_photo, dtype=np.float64)

    ps = [1, 5, 25, 50, 75, 90, 95, 99]

    def line(name, arr):
        q = _pct(arr, ps)
        print(f"  {name:24s} " + "  ".join(f"p{p}={q[p]:+.3f}" for p in ps))

    print("=" * 78)
    print("SIMILARITY PERCENTILES (cosine; 1.0 = identical)")
    print("=" * 78)
    line("all pairs", all_pairs)
    line("nearest-neighbour", nn)
    line("same-photo (negatives)", same_photo)
    print()

    print("ALL-PAIRS histogram (background: mostly different people)")
    _hist(all_pairs, lo=-0.1, hi=1.0, bins=22, label="cosine sim")
    print("\nNEAREST-NEIGHBOUR histogram (each face's closest match ~ same person)")
    _hist(nn, lo=-0.1, hi=1.0, bins=22, label="cosine sim")
    print()

    # ---- fragmentation: how many faces have ANY neighbour within eps ----
    def frag(eps):
        thr = 1.0 - eps  # neighbour if cosine sim >= 1 - eps
        has_nbr = int(np.count_nonzero(nn >= thr))
        return has_nbr, N - has_nbr

    neg_ceiling = float(np.percentile(same_photo, 99)) if len(same_photo) else float(np.percentile(all_pairs, 99))
    # A safe merge threshold sits above where known-different faces reach.
    suggested_match = round(min(0.97, max(0.5, neg_ceiling + 0.03)), 3)
    suggested_eps = round(max(0.05, min(0.60, 1.0 - suggested_match)), 3)

    print("=" * 78)
    print("FRAGMENTATION  (faces with >=1 neighbour within eps => can escape singleton)")
    print("=" * 78)
    for e in sorted({round(args.eps, 3), 0.05, 0.10, 0.20, 0.30, 0.40, suggested_eps}):
        withn, without = frag(e)
        tag = "  <- CURRENT" if abs(e - args.eps) < 1e-9 else ("  <- suggested" if abs(e - suggested_eps) < 1e-9 else "")
        print(f"  eps={e:.3f} (sim>= {1-e:.3f}):  {withn:5d} have a neighbour, {without:5d} forced singleton{tag}")
    print()

    print("=" * 78)
    print("DATA-DERIVED SUGGESTION")
    print("=" * 78)
    print(f"  same-photo negatives reach up to p99 = {neg_ceiling:+.3f}  (known different people)")
    print(f"  -> keep auto-merge threshold ABOVE that: suggested match/assign ~ {suggested_match:.3f}")
    print(f"  -> DBSCAN eps ~ {suggested_eps:.3f}   (vs current {args.eps:.3f})")
    print(f"  NOTE: verify with the top cross-photo pairs below before trusting these.\n")

    print("=" * 78)
    print(f"TOP {args.top} CROSS-PHOTO PAIRS (likely same person — eyeball these)")
    print("=" * 78)
    cross_photo_idx.sort(key=lambda t: t[0], reverse=True)
    for sim, a, b in cross_photo_idx[: args.top]:
        pa = person_ids[a][:8] or "-"
        pb = person_ids[b][:8] or "-"
        same_person = "SAME-cluster" if person_ids[a] and person_ids[a] == person_ids[b] else "diff-cluster"
        print(f"  sim={sim:+.3f}  {same_person:12s}  {filenames[a][:34]:34s} [{pa}]  <>  {filenames[b][:34]:34s} [{pb}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
