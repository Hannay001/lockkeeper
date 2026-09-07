#!/usr/bin/env python3
"""Semantic-retrieval sidecar for the capability router.

Runs OUT OF PROCESS, in its own pinned venv. This is not an optimization -- it is the
only workable design here:

  * The router must stay pure-stdlib. System python is 3.14 + PEP-668 externally-managed,
    so an in-process `try: import fastembed` inside the router would be permanently dead
    code (the except branch would always win). A subprocess boundary keeps the router
    dependency-free for real, not aspirationally.
  * The dot product lives here too. Scoring ~5k x 384 float32 in pure CPython is ~2M MACs
    (~1-1.5s); with numpy it is a single matmul (~3ms). Since numpy is already here for
    onnxruntime, the matmul belongs here and the router gets back a small JSON top-K.

Model: BAAI/bge-small-en-v1.5 (384d, ~67MB). RETRIEVAL-trained, and that is the whole point.

The previous choice, paraphrase-multilingual-MiniLM-L12-v2, was a SYMMETRIC/paraphrase model
and it was measurably worse than useless here: the nonsense query "xyzzy plugh frobnicate"
scored 0.461 against its nearest capability while the real query "who else is doing this
commercially" topped out at 0.341. Nonsense outranked meaning, so every real cosine fell
under the floor and the semantic term contributed exactly zero.

Routing is ASYMMETRIC -- a short colloquial QUERY must retrieve a jargon-dense DOCUMENT.
"who else is doing this commercially" is not a paraphrase of "competitive-analysis framework
for building competitive landscape decks"; it is a question ABOUT it. Paraphrase models are
trained for sentence<->sentence equivalence and do not do this. Retrieval models (BGE, E5)
are, which is why they carry an instruction prefix on the query side only.

English-only is a deliberate, verified trade. The German entrypoints route correctly by
LEXICAL scoring alone: "GmbH Gesellschaftsvertrag pruefen" hits its entrypoint at 62.1 with
no semantic help. BGE also retrieves them semantically. Four live German legal queries put
196-200 German records in a raw 200-wide top-K, with the correct capability first. That
top-K presence -- not a retired "semantic never demotes" property -- is what keeps
SEMANTIC_ABSENCE_FACTOR from taxing them. Raw top-K used to contain only ~100 distinct
capabilities because runtime twins consumed half the slots; query now filters for the target
runtime and groups (type, name) before applying top-K. Re-measure the German queries if the
model or SEMANTIC_TOPK changes.
A multilingual model that is bad at retrieval buys us less than an English one that is good.

BGE applies its instruction prefix to the QUERY ONLY -- passages are embedded raw. Prefixing
both sides would just add a constant the model has to encode away.

Verbs:
  build --registry <registry.jsonl> --out <dir>   write embeddings.bin + embeddings.json
  query --index <dir> [--topk N]                  read query on stdin, emit {"hits":[...]}

Contract with the router: any failure here (missing model, no network, bad index, timeout)
must leave the router working. It degrades to lexical-only scoring. Never raise into it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MODEL_NAME = "BAAI/bge-small-en-v1.5"
# Bump on ANY model/prefix change. cmd_query refuses an index whose schema_version or model
# does not match, so a stale index degrades to {} (lexical-only) instead of silently scoring
# this model's queries against the old model's vectors -- which would be garbage, not an error.
SCHEMA_VERSION = 2
DIM = 384

# BGE's retrieval instruction. Query side ONLY -- see module docstring.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def _load_model():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=MODEL_NAME)


def passage_text(record: dict) -> str:
    # Name carries most of the signal for a capability; description disambiguates.
    return f"{record['name']}. {record.get('description', '')}".strip()


def content_hash(text: str) -> str:
    """Stable key for a passage. If the text changes, the hash changes, and only then is the
    record re-embedded. The model name is folded in so a model swap invalidates every hash for
    free -- reusing a MiniLM vector under a BGE index would be a silent correctness bug."""
    import hashlib

    return hashlib.sha256(f"{MODEL_NAME}\x00{text}".encode("utf-8")).hexdigest()


def _load_vector_cache(out: Path) -> dict:
    """Return {id: (content_hash, normalized_vector)} from the PREVIOUS index, or {} if it is
    absent, unreadable, a different model/schema, or predates content hashing. Any doubt -> {},
    which just means a full re-embed: slower once, never wrong."""
    import numpy as np

    meta_path, bin_path = out / "embeddings.json", out / "embeddings.bin"
    if not meta_path.is_file() or not bin_path.is_file():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if meta.get("schema_version") != SCHEMA_VERSION or meta.get("model") != MODEL_NAME:
        return {}
    ids, hashes = meta.get("ids") or [], meta.get("hashes") or []
    count, dim = int(meta.get("count", 0)), int(meta.get("dim", 0))
    # No hashes => an index built before incremental support. Can't trust per-row reuse.
    if not hashes or not (len(ids) == len(hashes) == count) or dim != DIM:
        return {}
    try:
        matrix = np.frombuffer(bin_path.read_bytes(), dtype=np.float32).reshape(count, dim)
    except ValueError:
        return {}
    return {rid: (h, matrix[i]) for i, (rid, h) in enumerate(zip(ids, hashes, strict=True))}


def cmd_build(args: argparse.Namespace) -> int:
    import numpy as np

    registry = Path(args.registry)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    records = []
    with registry.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # Only embed what the router can actually rank. Records hidden by the router's
    # excluded upstream by record_is_rankable(); embedding them would be pure waste.
    rankable = [r for r in records if not _is_non_rankable(r)]
    if not rankable:
        print("status: error\nsummary: no rankable records", file=sys.stderr)
        return 1

    texts = [passage_text(r) for r in rankable]
    ids = [r["id"] for r in rankable]
    hashes = [content_hash(t) for t in texts]

    # Incremental: embedding is 80% of rebuild wall-clock (about two minutes for ~7k vectors). A rebuild
    # after changing one skill should re-embed one vector, not all of them. Reuse a cached
    # vector whenever the record's content hash is unchanged; embed only the rest.
    cache = _load_vector_cache(out)
    vectors = np.zeros((len(rankable), DIM), dtype=np.float32)
    to_embed = []
    for i, (rid, h) in enumerate(zip(ids, hashes, strict=True)):
        hit = cache.get(rid)
        if hit is not None and hit[0] == h:
            vectors[i] = hit[1]  # reuse path; cached vector was L2-normalized at its own build
        else:
            to_embed.append(i)

    if to_embed:
        model = _load_model()  # skipped entirely when nothing changed
        fresh = np.array(list(model.embed([texts[i] for i in to_embed])), dtype=np.float32)
        norms = np.linalg.norm(fresh, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        fresh /= norms
        for row, i in zip(fresh, to_embed, strict=True):
            vectors[i] = row

    (out / "embeddings.bin").write_bytes(vectors.tobytes())
    meta = {
        "schema_version": SCHEMA_VERSION,
        "model": MODEL_NAME,
        "dim": DIM,
        "count": len(rankable),
        "ids": ids,
        "hashes": hashes,
        "registry_fingerprint": args.fingerprint or "",
    }
    (out / "embeddings.json").write_text(json.dumps(meta), encoding="utf-8")
    reused = len(rankable) - len(to_embed)
    print(
        f"status: success\n"
        f"summary: {len(rankable):,} rankable capabilities "
        f"({len(to_embed):,} embedded, {reused:,} reused from cache; {DIM}d, {MODEL_NAME})\n"
        f"artifacts: {out / 'embeddings.bin'}"
    )
    return 0


def _is_non_rankable(record: dict) -> bool:
    """Per-deployment hook: True for records the router hides from ranked search.

    Reads the router's own registry metadata so the sidecar stays in sync with
    whatever ranking policy the deployment configures.
    """
    return record.get("rankable") is False


def runtime_grouped_hits(
    index_ids: list[str],
    scores,
    records: list[dict],
    runtime: str,
    topk: int,
) -> list[dict]:
    """Return top-K distinct, runtime-compatible capabilities.

    The registry can contain several rows for one logical capability, commonly a
    runtime-local row plus a shared row. Cutting the raw vector rows at K before
    filtering used to waste half of a 200-wide result on name twins. It also let
    incompatible runtimes consume slots. Group first, then cut, and propagate the
    winning group score to every eligible registration so the router can score the
    exact row it selected.
    """
    if topk <= 0:
        return []

    records_by_id = {
        str(record.get("id", "")): record
        for record in records
        if record.get("id") and record.get("name") and record.get("type")
    }
    eligible_members: dict[tuple[str, str], list[dict]] = {}
    for record in records_by_id.values():
        runtimes = record.get("runtimes") or []
        if runtime and runtime not in runtimes and "shared" not in runtimes:
            continue
        key = (str(record["type"]), str(record["name"]).lower())
        eligible_members.setdefault(key, []).append(record)

    group_scores: dict[tuple[str, str], float] = {}
    for record_id, score in zip(index_ids, scores, strict=True):
        record = records_by_id.get(str(record_id))
        if record is None:
            continue
        key = (str(record["type"]), str(record["name"]).lower())
        if key not in eligible_members:
            continue
        numeric_score = float(score)
        if key not in group_scores or numeric_score > group_scores[key]:
            group_scores[key] = numeric_score

    ordered_groups = sorted(group_scores, key=lambda key: (-group_scores[key], key))[:topk]
    hits: list[dict] = []
    for key in ordered_groups:
        cosine = round(group_scores[key], 4)
        for record in eligible_members[key]:
            hits.append(
                {
                    "id": record["id"],
                    "cos": cosine,
                    "type": record["type"],
                    "name": record["name"],
                }
            )
    return hits


def cmd_query(args: argparse.Namespace) -> int:
    import numpy as np

    index = Path(args.index)
    meta = json.loads((index / "embeddings.json").read_text(encoding="utf-8"))
    if meta.get("schema_version") != SCHEMA_VERSION or meta.get("model") != MODEL_NAME:
        print(json.dumps({"hits": []}))
        return 0

    count, dim = int(meta["count"]), int(meta["dim"])
    matrix = np.frombuffer((index / "embeddings.bin").read_bytes(), dtype=np.float32)
    matrix = matrix.reshape(count, dim)

    text = sys.stdin.read().strip()
    if not text:
        print(json.dumps({"hits": []}))
        return 0

    model = _load_model()
    vector = np.array(next(iter(model.embed([QUERY_PREFIX + text]))), dtype=np.float32)
    norm = np.linalg.norm(vector) or 1.0
    vector /= norm

    scores = matrix @ vector  # cosine: both sides are L2-normalized
    if args.registry:
        records = []
        with Path(args.registry).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        hits = runtime_grouped_hits(meta["ids"], scores, records, args.runtime, args.topk)
    else:
        topk = min(max(args.topk, 0), count)
        if topk:
            top = np.argpartition(-scores, topk - 1)[:topk]
            top = top[np.argsort(-scores[top])]
            hits = [{"id": meta["ids"][i], "cos": round(float(scores[i]), 4)} for i in top]
        else:
            hits = []
    print(json.dumps({"model": MODEL_NAME, "dim": dim, "hits": hits}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capability router semantic sidecar")
    sub = parser.add_subparsers(dest="verb", required=True)

    build = sub.add_parser("build")
    build.add_argument("--registry", required=True)
    build.add_argument("--out", required=True)
    build.add_argument("--fingerprint", default="")
    build.set_defaults(func=cmd_build)

    query = sub.add_parser("query")
    query.add_argument("--index", required=True)
    query.add_argument("--topk", type=int, default=200)
    query.add_argument("--registry", default="")
    query.add_argument("--runtime", default="")
    query.set_defaults(func=cmd_query)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
