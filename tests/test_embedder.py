"""Unit tests for the optional semantic sidecar.

These tests exercise the pure grouping logic without importing fastembed or
numpy, so the dependency-free core test matrix can still cover semantic
correctness.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lockkeeper_embedder", REPOSITORY_ROOT / "embedder" / "embed.py"
)
assert SPEC is not None and SPEC.loader is not None
embedder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(embedder)


class RuntimeAwareSemanticHitsTest(unittest.TestCase):
    def test_runtime_filter_and_name_dedup_happen_before_topk(self) -> None:
        records = [
            {
                "id": "skill:alpha:claude",
                "type": "skill",
                "name": "Alpha",
                "runtimes": ["claude"],
            },
            {
                "id": "skill:alpha:shared",
                "type": "skill",
                "name": "Alpha",
                "runtimes": ["shared"],
            },
            {
                "id": "skill:codex-only",
                "type": "skill",
                "name": "Codex only",
                "runtimes": ["codex"],
            },
            {
                "id": "skill:gamma",
                "type": "skill",
                "name": "Gamma",
                "runtimes": ["claude"],
            },
        ]
        ids = [record["id"] for record in records]
        scores = [0.91, 0.90, 0.99, 0.80]

        hits = embedder.runtime_grouped_hits(ids, scores, records, "claude", topk=2)

        self.assertEqual(
            [hit["id"] for hit in hits],
            ["skill:alpha:claude", "skill:alpha:shared", "skill:gamma"],
        )
        self.assertEqual({hit["cos"] for hit in hits[:2]}, {0.91})
        self.assertNotIn("skill:codex-only", {hit["id"] for hit in hits})

    def test_topk_counts_unique_capabilities_not_registration_rows(self) -> None:
        records = []
        ids = []
        scores = []
        for index in range(3):
            for runtime, score_delta in (("claude", 0.0), ("shared", -0.001)):
                record_id = f"skill:cap-{index}:{runtime}"
                records.append(
                    {
                        "id": record_id,
                        "type": "skill",
                        "name": f"cap-{index}",
                        "runtimes": [runtime],
                    }
                )
                ids.append(record_id)
                scores.append(0.9 - index * 0.1 + score_delta)

        hits = embedder.runtime_grouped_hits(ids, scores, records, "claude", topk=2)
        unique = {(hit["type"], hit["name"].lower()) for hit in hits}

        self.assertEqual(unique, {("skill", "cap-0"), ("skill", "cap-1")})
        self.assertEqual(len(hits), 4, "both eligible runtime registrations receive the group score")


if __name__ == "__main__":
    unittest.main()
