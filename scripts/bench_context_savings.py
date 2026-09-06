#!/usr/bin/env python3
"""Reproducible context-savings benchmark for the README proof.

Runs `bundle(..., estimate_savings=True)` for a fixed set of representative
tasks against the registry already built on this machine, and prints a table
of how much capability context each route keeps out of the prompt.

The honest headline is the INVARIANT, not one big number: the routed bundle
stays small and roughly flat no matter how large your installed library is,
so a bigger toolbox does not mean a bigger prompt. Token figures are estimates
(source-body bytes // BODY_BYTES_PER_TOKEN); see capability_registry.py.

Usage:
    lockkeeper rebuild                 # build the index first
    python3 scripts/bench_context_savings.py [--runtime claude] [--json]

Standard-library only.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capability_registry as registry  # noqa: E402

# Representative, everyday coding tasks. Deliberately generic so the benchmark
# reflects normal routing, not cherry-picked queries.
BENCHMARK_TASKS = (
    "migrate the auth module to the new token API",
    "audit our payment webhook for race conditions",
    "write unit tests for a python data pipeline",
    "review a react component for accessibility issues",
    "debug a failing CI build on github actions",
    "add rate limiting to a REST endpoint",
)


def run(runtime: str) -> dict:
    output = registry.ROUTER_CONFIG.output_dir
    records = registry.load_registry(output)
    rows = []
    skipped = []
    for task in BENCHMARK_TASKS:
        result = registry.bundle(records, task, runtime, "", 8, output, estimate_savings=True)
        # A sparse or freshly built index legitimately routes nothing for some
        # tasks; bundle() then returns a warning without a savings block. Skip
        # those instead of crashing the whole benchmark with a KeyError.
        savings = result.get("savings")
        if savings is None:
            skipped.append(task)
            continue
        rows.append(
            {
                "task": task,
                "eligible_capabilities": savings["eligible_capabilities"],
                "selected_capabilities": savings["selected_capabilities"],
                "selected_body_tokens": savings["selected_body_tokens"],
                "eligible_body_tokens": savings["eligible_body_tokens"],
                "avoided_token_fraction": savings.get("avoided_token_fraction", 0.0),
            }
        )
    if not rows:
        raise SystemExit(
            "status: error\n"
            "summary: no benchmark task routed a capability; the index looks empty\n"
            "next_actions: run `lockkeeper snapshot-runtimes && lockkeeper rebuild` "
            "after installing skills, then retry"
        )
    selected_tokens = [row["selected_body_tokens"] for row in rows]
    return {
        "runtime": runtime,
        "bytes_per_token": registry.BODY_BYTES_PER_TOKEN,
        "eligible_capabilities": rows[0]["eligible_capabilities"],
        "eligible_body_tokens": rows[0]["eligible_body_tokens"],
        "median_selected_body_tokens": int(statistics.median(selected_tokens)),
        "max_selected_body_tokens": max(selected_tokens),
        "skipped_tasks": skipped,
        "rows": rows,
    }


def print_table(report: dict) -> None:
    print(
        f"registry: {report['eligible_capabilities']:,} eligible capabilities "
        f"(~{report['eligible_body_tokens']:,} body tokens if all loaded), "
        f"runtime={report['runtime']}, est. @ {report['bytes_per_token']} B/tok\n"
    )
    header = f"{'task':45} {'loaded':>6} {'tokens in context':>18} {'kept out of context':>20}"
    print(header)
    print("-" * len(header))
    for row in report["rows"]:
        print(
            f"{row['task'][:45]:45} "
            f"{row['selected_capabilities']:>6} "
            f"{row['selected_body_tokens']:>17,}t "
            f"{row['avoided_token_fraction'] * 100:>18.2f}%"
        )
    print(
        f"\nAcross these tasks the prompt carried a median of "
        f"{report['median_selected_body_tokens']:,} skill-body tokens "
        f"(max {report['max_selected_body_tokens']:,}) instead of "
        f"~{report['eligible_body_tokens']:,}. The routed bundle stays flat as the "
        f"library grows."
    )
    if report.get("skipped_tasks"):
        print(
            f"\nskipped {len(report['skipped_tasks'])} task(s) with no eligible capability "
            f"in this index: {', '.join(report['skipped_tasks'])}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Context-savings benchmark for the README proof.")
    parser.add_argument("--runtime", default="claude", help="Runtime perspective to route from")
    parser.add_argument("--json", action="store_true", help="Emit the raw report as JSON")
    args = parser.parse_args(argv)

    report = run(args.runtime)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
