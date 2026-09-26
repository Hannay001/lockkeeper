#!/usr/bin/env python3
"""Measure a decision provider against Lockkeeper's own ranking on YOUR corpus.

Published decision-model benchmarks (Banking77, typed-decisions, ...) say little
about agent capability routing. This harness answers the only question that
matters before turning a provider from shadow into rerank: does it route your
real tasks better?

Input: a JSONL file, one labeled task per line:

    {"query": "audit our payment webhook for race conditions",
     "relevant": ["payment-race-auditor", "security-reviewer"],
     "runtime": "claude"}

`relevant` lists capability NAMES that a good route should include. Label
30-300 real tasks from your own history; include vague ones and ones where the
current router is wrong.

Usage:

    python3 scripts/eval_decision.py labels.jsonl
    python3 scripts/eval_decision.py labels.jsonl --provider-json \
        '{"provider": "sidecar", "backend": "cross-encoder"}'
    python3 scripts/eval_decision.py labels.jsonl --json > report.json

Reports, for the baseline and the provider-reranked ranking: Recall@K of the
routed bundle, MRR and Recall@10 over the ranked list, how often the provider
changed the bundle, provider errors, and latency p50/p95.

Standard-library only; the provider itself may run elsewhere.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capability_registry as registry  # noqa: E402
import decision_provider as dp  # noqa: E402


def load_labels(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("query"), str) or not isinstance(row.get("relevant"), list):
            raise SystemExit(f"{path}:{number}: expected {{\"query\": str, \"relevant\": [names]}}")
        rows.append(row)
    if not rows:
        raise SystemExit(f"{path}: no labeled tasks")
    return rows


def reciprocal_rank(names: list[str], relevant: set[str]) -> float:
    return next((1.0 / rank for rank, name in enumerate(names, start=1) if name in relevant), 0.0)


def recall(names: list[str], relevant: set[str]) -> float:
    return len(relevant & set(names)) / len(relevant) if relevant else 0.0


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def evaluate(labels: list[dict[str, Any]], settings: dp.DecisionSettings, max_count: int) -> dict[str, Any]:
    output = registry.ROUTER_CONFIG.output_dir
    records = registry.ensure_query_registry_fresh(output) or registry.load_registry(output, verify_sources=False)
    provider = dp.make_provider(settings, output=output, sidecar_script=registry.decision_sidecar_script())
    rows = []
    for label in labels:
        query, runtime = label["query"], label.get("runtime", "claude")
        relevant = {str(name).lower() for name in label["relevant"]}
        ranked = registry.ranked_records(records, query, runtime, output)
        run = dp.DecisionRun(provider, dp.with_mode(settings, "rerank"), query)
        scores = run(ranked, registry.record_source_is_trusted)
        reranked = dp.blend(ranked, scores, settings.weight)
        baseline_bundle = registry.bundle(records, query, runtime, "", max_count, output, verify_sources=True)
        decided_bundle = registry.bundle(
            records, query, runtime, "", max_count, output, verify_sources=True, decision=run
        )

        def names(pairs):
            return [record["name"].lower() for _score, record in pairs]

        base_names, new_names = names(ranked), names(reranked)
        base_routed = [item["name"].lower() for item in baseline_bundle["bundle"]]
        new_routed = [item["name"].lower() for item in decided_bundle["bundle"]]
        report = run.report("rerank")
        rows.append(
            {
                "query": query,
                "baseline": {
                    "bundle_recall": recall(base_routed, relevant),
                    "mrr": reciprocal_rank(base_names, relevant),
                    "recall_at_10": recall(base_names[:10], relevant),
                },
                "decision": {
                    "bundle_recall": recall(new_routed, relevant),
                    "mrr": reciprocal_rank(new_names, relevant),
                    "recall_at_10": recall(new_names[:10], relevant),
                },
                "bundle_changed": base_routed != new_routed,
                "error": report["error"],
                "applied": report["applied"],
                "latency_ms": report["latency_ms"],
                "clarification": report["clarification"],
            }
        )

    def mean(side: str, metric: str) -> float:
        return round(statistics.fmean(row[side][metric] for row in rows), 4)

    latencies = [row["latency_ms"] for row in rows if not row["error"]]
    return {
        "provider": settings.provider,
        "backend": settings.backend if settings.provider == "sidecar" else None,
        "model": settings.model or None,
        "tasks": len(rows),
        "summary": {
            side: {metric: mean(side, metric) for metric in ("bundle_recall", "mrr", "recall_at_10")}
            for side in ("baseline", "decision")
        },
        "bundles_changed": sum(row["bundle_changed"] for row in rows),
        "provider_errors": sum(bool(row["error"]) for row in rows),
        "abstained": sum(not row["applied"] and not row["error"] for row in rows),
        "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95)},
        "rows": rows,
    }


def print_report(report: dict[str, Any]) -> None:
    label = report["provider"] + (f"/{report['backend']}" if report["backend"] else "")
    print(f"decision provider: {label}  model: {report['model'] or 'default'}  tasks: {report['tasks']}")
    print(f"{'metric':<16}{'baseline':>10}{'decision':>10}{'delta':>9}")
    for metric in ("bundle_recall", "mrr", "recall_at_10"):
        base = report["summary"]["baseline"][metric]
        new = report["summary"]["decision"][metric]
        print(f"{metric:<16}{base:>10.3f}{new:>10.3f}{new - base:>+9.3f}")
    print(
        f"\nbundles changed: {report['bundles_changed']}/{report['tasks']}  "
        f"provider errors: {report['provider_errors']}  abstained (flat scores): {report['abstained']}\n"
        f"provider latency: p50 {report['latency_ms']['p50']:.0f} ms, p95 {report['latency_ms']['p95']:.0f} ms"
    )
    worse = [row for row in report["rows"] if row["decision"]["bundle_recall"] < row["baseline"]["bundle_recall"]]
    if worse:
        print(f"\ntasks the provider made worse ({len(worse)}):")
        for row in worse[:10]:
            print(f"  - {row['query'][:90]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a decision provider on labeled routing tasks.")
    parser.add_argument("labels", type=Path, help="JSONL of {query, relevant[, runtime]}")
    parser.add_argument("--provider-json", help="override [extensions.decision] for this run (a JSON object)")
    parser.add_argument("--max", type=int, default=10, help="bundle size (3-20)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    registry.ensure_router_config_valid()
    raw = json.loads(args.provider_json) if args.provider_json else registry.ROUTER_CONFIG.get_extension("decision")
    try:
        settings = dp.parse_settings(raw)
    except dp.DecisionConfigError as error:
        raise SystemExit(f"status: error\nsummary: {error}") from error
    if not settings.provider:
        raise SystemExit(
            "status: error\nsummary: no decision provider configured; pass --provider-json or set "
            "[extensions.decision] (see decision/README.md)"
        )
    report = evaluate(load_labels(args.labels), settings, args.max)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
