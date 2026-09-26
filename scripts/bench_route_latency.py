#!/usr/bin/env python3
"""Route latency for long task prompts on a large synthetic registry.

Lexical ranking cost grows with query terms x registry size, so the case to
watch is a long task (~180 words, ~90 query terms) routed against tens of
thousands of capabilities. This builds a deterministic synthetic registry in
memory -- no install, no network, no semantic sidecar -- and times bundle(),
which is what `lockkeeper route` runs, from a cold ranking index per task, the
way each route process starts.

Usage:
    python3 scripts/bench_route_latency.py [--records 26000] [--tasks 7] [--json]
    python3 scripts/bench_route_latency.py --max-p50 2.5   # exit 1 when slower (CI)

Standard-library only.
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capability_registry as registry  # noqa: E402

SYLLABLES = (
    "ka", "lo", "mi", "ne", "ru", "ta", "vo", "zi", "pe", "shu", "dra", "fen", "gor", "hal",
    "jin", "kel", "mor", "nix", "pra", "qua", "sel", "tor", "ul", "vek", "wyn", "yar", "zor",
)
# Shapes real skill text has and plain words do not: symbols inside tokens,
# umlauts that fold, versions, flags.
SPECIAL_WORDS = (
    "python", "pdf", "api", "data", "csv", "json", "sql", "docker", "react", "c++", "node.js",
    "e-mail", "v1.2", "--dry-run", "größe", "kündigung", "übersicht", "straße",
)
TASK_GLUE = ("the", "a", "to", "of", "and", "with", "for", "in", "you", "then", "must", "each")


def _vocabulary(rng: random.Random, size: int) -> list[str]:
    words: dict[str, None] = dict.fromkeys(SPECIAL_WORDS)
    while len(words) < size:
        words["".join(rng.choices(SYLLABLES, k=rng.randint(2, 4)))] = None
    return list(words)


class SyntheticCorpus:
    """Zipf-distributed pseudo-vocabulary: a few words common enough to be damped, a long rare tail."""

    def __init__(self, seed: int = 20260926, vocabulary_size: int = 6000) -> None:
        self.rng = random.Random(seed)
        self.words = _vocabulary(self.rng, vocabulary_size)
        self.cumulative = list(itertools.accumulate(1.0 / (rank + 1) for rank in range(len(self.words))))

    def sample(self, count: int) -> list[str]:
        return self.rng.choices(self.words, cum_weights=self.cumulative, k=count)

    def records(self, count: int) -> list[dict]:
        categories = sorted(registry.CATEGORY_BY_SLUG)
        runtimes = (["claude"], ["claude", "codex"], ["shared"], ["codex"])
        statuses = ("active", "active", "available", "catalogued")
        rows = []
        for index in range(count):
            name = "-".join(self.sample(self.rng.randint(1, 3)))
            description = " ".join(self.sample(self.rng.randint(8, 40))).capitalize() + "."
            capability_type = "mcp" if index % 97 == 0 else "tool" if index % 89 == 0 else "skill"
            source_path = "" if capability_type in {"mcp", "tool"} else f"/home/dev/.claude/skills/{name}-{index}/SKILL.md"
            rows.append(
                {
                    "id": f"{capability_type}:synthetic-{index}",
                    "type": capability_type,
                    "name": name,
                    "description": description,
                    "category": categories[index % len(categories)],
                    "status": statuses[index % len(statuses)],
                    "runtimes": runtimes[index % len(runtimes)],
                    "source_path": source_path,
                    "registration_count": 1,
                    "owner": "",
                }
            )
        return rows

    def task(self, words: int = 180) -> str:
        """A SkillsBench-shaped task: markdown, paths, and coordinated intents."""
        parts = ["## Task\n"]
        for index, word in enumerate(self.sample(words)):
            parts.append(word)
            if index % 9 == 8:
                parts.append(self.rng.choice(TASK_GLUE))
            if index % 23 == 22:
                parts.append(self.rng.choice((".", "; then", ". Also", "\n- ", " `/root/out.json`")))
        return " ".join(parts)


def route_seconds(records: list[dict], query: str, output: Path) -> float:
    # Each `lockkeeper route` is a new process: start from a cold index.
    registry._LEXICAL_INDEXES.clear()
    registry._SEMANTIC_CACHE.clear()
    started = time.perf_counter()
    registry.bundle(records, query, "claude", "", registry.DEFAULT_BUNDLE_SIZE, output)
    return time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", type=int, default=26000)
    parser.add_argument("--tasks", type=int, default=7)
    parser.add_argument("--words", type=int, default=180)
    parser.add_argument("--max-p50", type=float, default=None, help="fail when the median route is slower")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    corpus = SyntheticCorpus()
    records = corpus.records(args.records)
    tasks = [corpus.task(args.words) for _ in range(args.tasks)]
    with tempfile.TemporaryDirectory() as directory:
        seconds = [route_seconds(records, task, Path(directory)) for task in tasks]
    report = {
        "records": len(records),
        "tasks": len(tasks),
        "task_words": args.words,
        "median_query_terms": statistics.median(len(registry.query_terms(task)) for task in tasks),
        "p50_seconds": round(statistics.median(seconds), 3),
        "max_seconds": round(max(seconds), 3),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"route over {report['records']:,} records, {report['tasks']} tasks of {args.words} words "
            f"(~{report['median_query_terms']:.0f} terms): p50 {report['p50_seconds']:.3f}s, "
            f"max {report['max_seconds']:.3f}s"
        )
    if args.max_p50 is not None and report["p50_seconds"] > args.max_p50:
        print(f"route p50 {report['p50_seconds']:.3f}s exceeds the {args.max_p50:.3f}s budget", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
