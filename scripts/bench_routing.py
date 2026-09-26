#!/usr/bin/env python3
"""Benchmark Lockkeeper routing on a public skill-routing benchmark.

Uses SkillRouter Eval Core (huggingface.co/datasets/pipizhao/SkillRouter-Eval-Core,
from the SkillRouter paper, arXiv:2603.22455): 87 SkillsBench tasks with
ground-truth skills and graded relevance, and a pool of real SKILL.md files
collected from public repositories. The "hard" tier (79,141 skills) adds 780
LLM-written lookalike distractors to the "easy" tier (78,361).

`prepare` materializes a sample of the pool as real skill folders in an
ISOLATED home directory and rebuilds the registry there; `run` routes every
scored task through the same code path as `lockkeeper route` and reports
ranking quality and latency, optionally with a decision provider.

    python3 scripts/bench_routing.py prepare --home /tmp/lk-bench-26k --size 26000
    python3 scripts/bench_routing.py run --home /tmp/lk-bench-26k
    python3 scripts/bench_routing.py run --home /tmp/lk-bench-26k --provider-json \
        '{"provider": "sidecar", "backend": "cross-encoder"}' --json report.json

Scoring follows the benchmark's protocol: tasks marked generic_only are
excluded (75 of 87 remain), matches are by skill id, never by name, and nDCG@10
uses the graded relevance labels. Every ground-truth skill is always included
in a sample; the rest of the sample is drawn with a fixed seed.

Standard-library only; a decision provider may run elsewhere.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Optional

SCRIPTS = Path(__file__).resolve().parent
DATASET = "pipizhao/SkillRouter-Eval-Core"
# Pinned so every run scores against the same bytes.
DATASET_REVISION = "20a03920e7f08d76b67af367350d25ef4468198e"
TIERS = ("easy", "hard")
MARKER = ".lockkeeper-bench"
RANK_CUTOFFS = (1, 3, 5, 10, 20, 50)
DEFAULT_DATA = Path.home() / ".cache" / "lockkeeper-bench" / "skillrouter-eval-core"


# ---------------------------------------------------------------- dataset


def _fetch(relative: str, destination: Path) -> None:
    url = f"https://huggingface.co/datasets/{DATASET}/resolve/{DATASET_REVISION}/{relative}"
    headers = {"User-Agent": "lockkeeper-bench"}
    token = os.environ.get("HF_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as handle:  # noqa: S310
        shutil.copyfileobj(response, handle)
    partial.replace(destination)


def ensure_dataset(data: Path, tier: str) -> dict[str, Any]:
    """Download the pinned benchmark files that are not already in `data`."""
    manifest_path = data / "manifest.json"
    if not manifest_path.is_file():
        _fetch("manifest.json", manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    wanted = [manifest["tasks"]["path"], manifest["relevance"]["path"]]
    wanted += [part["path"] for part in manifest[tier]["parts"]]
    for relative in wanted:
        if not (data / relative).is_file():
            print(f"downloading {relative}", file=sys.stderr)
            _fetch(relative, data / relative)
    return manifest


def iter_pool(data: Path, manifest: dict[str, Any], tier: str) -> Iterator[dict[str, Any]]:
    for part in manifest[tier]["parts"]:
        with gzip.open(data / part["path"], "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def load_labels(data: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    relevance = json.loads((data / manifest["relevance"]["path"]).read_text(encoding="utf-8"))
    labels = []
    for line in (data / manifest["tasks"]["path"]).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        task = json.loads(line)
        judged = relevance.get(task["task_id"])
        if judged is None:
            continue
        labels.append(
            {
                "task_id": task["task_id"],
                "query": task["instruction_text"],
                "task_type": judged["task_type"],
                "gt": list(judged["gt_skill_ids"]),
                "relevance": {key: int(value) for key, value in judged["relevance"].items()},
            }
        )
    return labels


# ---------------------------------------------------------------- prepare


def skill_folder(skill_id: str) -> str:
    # Opaque folder names: the id carries labels ("gt/", "distractor/") that must
    # not leak into anything the router can read.
    return "s" + hashlib.sha256(skill_id.encode("utf-8")).hexdigest()[:16]


def one_line(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def skill_markdown(row: dict[str, Any]) -> str:
    name = one_line(row.get("name"), 200) or skill_folder(row["skill_id"])
    description = one_line(row.get("description"), 1024)
    return (
        "---\n"
        f"name: {json.dumps(name, ensure_ascii=False)}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n\n"
        f"{str(row.get('body') or '').strip()}\n"
    )


def bench_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    env.pop("CAPABILITY_ROUTER_CONFIG", None)
    return env


def claim_home(home: Path) -> None:
    """Refuse to write skills into a directory that is not a benchmark home."""
    if home.exists() and any(home.iterdir()) and not (home / MARKER).is_dir():
        raise SystemExit(f"{home} is not empty and was not created by this benchmark; refusing to touch it")
    (home / MARKER).mkdir(parents=True, exist_ok=True)


def prepare(args: argparse.Namespace) -> int:
    home = args.home.expanduser().resolve()
    claim_home(home)
    manifest = ensure_dataset(args.data, args.tier)
    labels = load_labels(args.data, manifest)
    pool = {row["skill_id"]: row for row in iter_pool(args.data, manifest, args.tier)}
    required = sorted(
        {skill for label in labels for skill in label["gt"]}
        | {skill for skill in pool if skill.startswith("distractor/")}
    )
    missing = [skill for skill in required if skill not in pool]
    if missing:
        raise SystemExit(f"{len(missing)} labeled skills are missing from the {args.tier} pool: {missing[:5]}")
    others = sorted(set(pool) - set(required))
    size = len(pool) if args.size <= 0 else args.size
    if size < len(required):
        raise SystemExit(f"--size must be at least {len(required)} (every labeled skill and distractor)")
    chosen = required + random.Random(args.seed).sample(others, min(len(others), size - len(required)))

    skills_root = home / ".agents" / "skills"
    if skills_root.exists():
        shutil.rmtree(skills_root)
    for state in (home / ".agents" / "capabilities", home / ".local" / "state" / "cap"):
        if state.exists():
            shutil.rmtree(state)
    folders: dict[str, str] = {}
    for skill_id in chosen:
        folder = skill_folder(skill_id)
        folders[folder] = skill_id
        target = skills_root / folder
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(skill_markdown(pool[skill_id]), encoding="utf-8")
    meta = {
        "dataset": DATASET,
        "revision": DATASET_REVISION,
        "tier": args.tier,
        "size": len(chosen),
        "seed": args.seed,
        "folders": folders,
    }
    (home / MARKER / "corpus.json").write_text(json.dumps(meta), encoding="utf-8")
    (home / MARKER / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    print(f"wrote {len(chosen):,} skills ({args.tier} tier, seed {args.seed}) under {skills_root}")

    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "capability_registry.py"), "rebuild"],
        env=bench_env(home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        print(proc.stdout[-2000:], proc.stderr[-2000:], sep="\n", file=sys.stderr)
        raise SystemExit(f"rebuild failed with exit code {proc.returncode}")
    meta["rebuild_seconds"] = round(elapsed, 2)
    (home / MARKER / "corpus.json").write_text(json.dumps(meta), encoding="utf-8")
    print(f"rebuild: {elapsed:.1f}s")
    return 0


# ---------------------------------------------------------------- metrics


def score_ranking(ids: list[Optional[str]], label: dict[str, Any]) -> dict[str, float]:
    gt = set(label["gt"])
    graded = label["relevance"]
    metrics: dict[str, float] = {}
    for cutoff in RANK_CUTOFFS:
        metrics[f"recall@{cutoff}"] = len(gt & set(ids[:cutoff])) / len(gt)
    metrics["hit@1"] = float(bool(ids) and ids[0] in gt)
    metrics["mrr"] = next((1.0 / rank for rank, skill in enumerate(ids, start=1) if skill in gt), 0.0)
    dcg = sum((2 ** graded.get(skill, 0) - 1) / math.log2(rank + 2) for rank, skill in enumerate(ids[:10]))
    # Ideal ranking over the ground-truth skills; the graded "degraded/" variants
    # are not part of either public pool, so they cannot be retrieved.
    ideal = sorted((graded.get(skill, 3) for skill in gt), reverse=True)[:10]
    idcg = sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(ideal))
    metrics["ndcg@10"] = dcg / idcg if idcg else 0.0
    return metrics


def score_bundle(ids: list[Optional[str]], label: dict[str, Any]) -> dict[str, float]:
    gt = set(label["gt"])
    hits = len(gt & set(ids))
    return {"bundle_recall": hits / len(gt), "bundle_precision": hits / len(ids) if ids else 0.0}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def mean_metrics(rows: list[dict[str, Any]], side: str) -> dict[str, float]:
    present = [row[side] for row in rows if side in row]
    if not present:
        return {}
    return {key: round(statistics.fmean(item[key] for item in present), 4) for key in present[0]}


# ---------------------------------------------------------------- run


def run(args: argparse.Namespace) -> int:
    home = args.home.expanduser().resolve()
    if not (home / MARKER / "corpus.json").is_file():
        raise SystemExit(f"{home} has no prepared corpus; run `prepare` first")
    corpus = json.loads((home / MARKER / "corpus.json").read_text(encoding="utf-8"))
    labels = json.loads((home / MARKER / "labels.json").read_text(encoding="utf-8"))
    if not args.all_tasks:
        labels = [label for label in labels if label["task_type"] != "generic_only"]
    if args.limit:
        labels = labels[: args.limit]
    folders: dict[str, str] = corpus["folders"]

    # The registry resolves every path from HOME at import time.
    for key, value in bench_env(home).items():
        os.environ[key] = value
    os.environ.pop("CAPABILITY_ROUTER_CONFIG", None)
    sys.path.insert(0, str(SCRIPTS))
    import capability_registry as registry  # noqa: E402
    import decision_provider as dp  # noqa: E402

    registry.ensure_router_config_valid()
    settings = dp.parse_settings(json.loads(args.provider_json)) if args.provider_json else dp.DecisionSettings()
    if args.provider_timeout:
        # Offline evaluation of slow models on CPU may exceed the 120 s a live route allows.
        settings = replace(settings, timeout_seconds=args.provider_timeout)
    output = registry.ROUTER_CONFIG.output_dir
    started = time.perf_counter()
    records = registry.ensure_query_registry_fresh(output) or registry.load_registry(output, verify_sources=False)
    load_seconds = time.perf_counter() - started
    provider = (
        dp.make_provider(settings, output=output, sidecar_script=registry.decision_sidecar_script())
        if settings.provider
        else None
    )

    def skill_of(record: dict[str, Any]) -> Optional[str]:
        """Skill id of a registry record or a routed bundle item (which carries load_path)."""
        if record.get("type") != "skill":
            return None
        path = record.get("source_path") or record.get("load_path") or ""
        return folders.get(Path(path).parent.name)

    def ids_of(pairs: list[tuple[float, dict[str, Any]]]) -> list[Optional[str]]:
        return [skill_of(record) for _score, record in pairs]

    reusable: dict[str, dict[str, Any]] = {}
    if args.resume and args.json and args.json.is_file():
        # Keep tasks that completed without a provider error; re-run the rest.
        previous = json.loads(args.json.read_text(encoding="utf-8"))
        reusable = {row["task_id"]: row for row in previous["rows"] if not row.get("provider_error")}

    rows: list[dict[str, Any]] = []
    for index, label in enumerate(labels, start=1):
        if label["task_id"] in reusable:
            rows.append(reusable[label["task_id"]])
            continue
        # Route exactly what `lockkeeper route` would: long prompts are clipped, not refused.
        query = registry.focus_query(label["query"])[0] if hasattr(registry, "focus_query") else label["query"]
        began = time.perf_counter()
        ranked = registry.ranked_records(records, query, args.runtime, output)
        rank_ms = (time.perf_counter() - began) * 1000
        ranked_ids = ids_of(ranked)
        present = {skill for skill in ranked_ids if skill}
        row: dict[str, Any] = {
            "task_id": label["task_id"],
            "task_type": label["task_type"],
            "gt": label["gt"],
            "rank_ms": round(rank_ms, 1),
            # Ground-truth skills absent from the ranking entirely: never matched,
            # or hidden because another skill with the same name ranked higher.
            "gt_unranked": sorted(set(label["gt"]) - present),
            "baseline": score_ranking(ranked_ids, label),
            "baseline_top5": [skill for skill in ranked_ids[:5]],
        }
        if not args.skip_baseline_route:
            began = time.perf_counter()
            routed = registry.bundle(records, query, args.runtime, "", args.max, output, verify_sources=True)
            row["route_ms"] = round((time.perf_counter() - began) * 1000, 1)
            row["baseline_bundle"] = score_bundle([skill_of(item) for item in routed["bundle"]], label)
        if provider is not None:
            decision = dp.DecisionRun(provider, dp.with_mode(settings, "rerank"), query)
            scores = decision(ranked, registry.record_source_is_trusted)
            blended = dp.blend(ranked, scores, settings.weight)
            judged = decision.outcome.scores if decision.outcome and not decision.outcome.error else {}
            # The provider's own order over its shortlist, then everything else as ranked.
            shortlist = [pair for pair in ranked if pair[1]["id"] in judged]
            rest = [pair for pair in ranked if pair[1]["id"] not in judged]
            pure = sorted(shortlist, key=lambda pair: -judged[pair[1]["id"]]) + rest
            # The same blend, confined to the shortlist: rows the provider never saw
            # cannot overtake the rows it judged.
            # Uses `scores`, which is empty when the run abstained, exactly like blend().
            top = max((score for score, _record in shortlist), default=0.0)
            local = (
                sorted(
                    shortlist,
                    key=lambda pair: -((1 - settings.weight) * pair[0] + settings.weight * scores[pair[1]["id"]] * top),
                )
                + rest
                if scores
                else ranked
            )
            decided = registry.bundle(
                records, query, args.runtime, "", args.max, output, verify_sources=True, decision=decision
            )
            report = decision.report("rerank")
            row.update(
                {
                    "decision": score_ranking(ids_of(blended), label),
                    "decision_local": score_ranking(ids_of(local), label),
                    "provider_only": score_ranking(ids_of(pure), label),
                    # Enough to recompute any blend offline without re-running the model.
                    "shortlist": [
                        [skill_of(record), round(score, 3), judged[record["id"]]] for score, record in shortlist
                    ],
                    "ranked_top100": [[skill_of(record), round(score, 3)] for score, record in ranked[:100]],
                    "decision_bundle": score_bundle([skill_of(item) for item in decided["bundle"]], label),
                    "shortlist_recall": len(set(label["gt"]) & {skill_of(r) for _s, r in shortlist})
                    / len(label["gt"]),
                    "provider_ms": report["latency_ms"],
                    "provider_error": report["error"],
                    "applied": report["applied"],
                }
            )
        rows.append(row)
        if not args.quiet:
            line = f"[{index}/{len(labels)}] {label['task_id'][:40]:<40} rank {rank_ms:7.0f} ms"
            if provider is not None:
                line += f"  provider {row['provider_ms']:7.0f} ms" + (f"  ERROR {row['provider_error']}" if row["provider_error"] else "")
            print(line, file=sys.stderr, flush=True)

    shortlist_size = settings.shortlist
    report = {
        "dataset": f"{corpus['dataset']}@{corpus['revision'][:12]}",
        "tier": corpus["tier"],
        "corpus_size": corpus["size"],
        "registry_records": len(records),
        "rebuild_seconds": corpus.get("rebuild_seconds"),
        "registry_load_seconds": round(load_seconds, 2),
        "tasks": len(rows),
        "provider": None
        if provider is None
        else {
            "provider": settings.provider,
            "backend": settings.backend if settings.provider == "sidecar" else None,
            "command": list(settings.command) if settings.provider == "command" else None,
            "model": settings.model or None,
            "shortlist": shortlist_size,
            "weight": settings.weight,
        },
        "summary": {
            side: mean_metrics(rows, side)
            for side in ("baseline", "decision", "decision_local", "provider_only", "baseline_bundle", "decision_bundle")
            if any(side in row for row in rows)
        },
        "latency_ms": {
            key: {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}
            for key in ("rank", "route")
            if (values := [row[f"{key}_ms"] for row in rows if f"{key}_ms" in row])
        },
        "gt_unranked": sum(len(row["gt_unranked"]) for row in rows),
        "gt_total": sum(len(row["gt"]) for row in rows),
        "rows": rows,
    }
    if provider is not None:
        ok = [row["provider_ms"] for row in rows if not row["provider_error"]]
        report["latency_ms"]["provider"] = {"p50": percentile(ok, 0.5), "p95": percentile(ok, 0.95)}
        report["provider_errors"] = sum(bool(row["provider_error"]) for row in rows)
        report["abstained"] = sum(not row["applied"] and not row["provider_error"] for row in rows)
        report["shortlist_recall"] = round(statistics.fmean(row["shortlist_recall"] for row in rows), 4)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print_report(report)
    return 0


def print_report(report: dict[str, Any]) -> None:
    print(
        f"{report['dataset']} {report['tier']} tier: {report['corpus_size']:,} skills "
        f"({report['registry_records']:,} registry records), {report['tasks']} tasks"
    )
    if report["rebuild_seconds"] is not None:
        print(f"rebuild {report['rebuild_seconds']:.1f}s, registry load {report['registry_load_seconds']:.1f}s")
    summary = report["summary"]
    sides = [side for side in ("baseline", "decision", "decision_local", "provider_only") if side in summary]
    metrics = ["hit@1", "mrr", "ndcg@10", *(f"recall@{cutoff}" for cutoff in RANK_CUTOFFS[1:])]
    print(f"\n{'ranking':<14}" + "".join(f"{side:>15}" for side in sides))
    for metric in metrics:
        print(f"{metric:<14}" + "".join(f"{summary[side][metric]:>15.3f}" for side in sides))
    bundles = [side for side in ("baseline_bundle", "decision_bundle") if side in summary]
    print(f"\n{'bundle':<14}" + "".join(f"{side.split('_')[0]:>15}" for side in bundles))
    for metric in ("bundle_recall", "bundle_precision"):
        print(f"{metric.split('_')[1]:<14}" + "".join(f"{summary[side][metric]:>15.3f}" for side in bundles))
    latency = report["latency_ms"]
    print(
        "\nlatency: "
        + "; ".join(
            f"{key} p50 {latency[key]['p50']:.0f} ms / p95 {latency[key]['p95']:.0f} ms"
            for key in ("rank", "route")
            if key in latency
        )
    )
    print(f"ground-truth skills never ranked: {report['gt_unranked']}/{report['gt_total']}")
    if report.get("provider"):
        print(
            f"provider: p50 {latency['provider']['p50']:.0f} ms / p95 {latency['provider']['p95']:.0f} ms, "
            f"errors {report['provider_errors']}, abstained {report['abstained']}, "
            f"shortlist recall {report['shortlist_recall']:.3f} (ceiling for any reranker)"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="download the benchmark and build an isolated corpus")
    prep.add_argument("--home", type=Path, required=True, help="isolated HOME for the corpus (created)")
    prep.add_argument("--data", type=Path, default=DEFAULT_DATA, help="dataset cache directory")
    prep.add_argument("--tier", choices=TIERS, default="hard")
    prep.add_argument("--size", type=int, default=0, help="skills in the corpus; 0 = the whole tier")
    prep.add_argument("--seed", type=int, default=7)
    go = sub.add_parser("run", help="route every task and score the results")
    go.add_argument("--home", type=Path, required=True)
    go.add_argument("--provider-json", help="a [extensions.decision] table as JSON")
    go.add_argument("--provider-timeout", type=float, default=0, help="seconds; overrides the 120 s config cap")
    go.add_argument("--runtime", default="claude")
    go.add_argument("--max", type=int, default=8, help="portfolio size (3-12)")
    go.add_argument("--limit", type=int, default=0, help="only the first N tasks")
    go.add_argument("--all-tasks", action="store_true", help="include generic_only tasks")
    go.add_argument(
        "--skip-baseline-route",
        action="store_true",
        help="skip the provider-free bundle (its ranking metrics are still reported)",
    )
    go.add_argument("--json", type=Path, help="write the full report here")
    go.add_argument(
        "--resume",
        action="store_true",
        help="reuse tasks from an existing --json report that finished without a provider error",
    )
    go.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    return prepare(args) if args.command == "prepare" else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
