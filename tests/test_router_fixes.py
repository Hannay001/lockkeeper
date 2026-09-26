"""Regression tests for router correctness fixes found in the September 2026 audit."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402

from tests.test_registry_freshness import FreshnessFixture  # noqa: E402


def record(name: str, description: str, category: str = "software-engineering", **extra) -> dict:
    return {
        "id": f"skill:{name}",
        "type": "skill",
        "name": name,
        "description": description,
        "category": category,
        "status": "active",
        "runtimes": ["claude"],
        "source_path": "",
        "registration_count": 1,
        "owner": "",
        **extra,
    }


class BoundRootDedupTest(FreshnessFixture):
    def test_init_binding_a_builtin_root_does_not_walk_it_twice(self) -> None:
        config = replace(
            registry.ROUTER_CONFIG,
            extensions=(("extra_skill_roots", [str(self.skills), str(self.temp / "extra")]),),
        )
        registry.configure_router(config)
        bound = [(runtime, root) for runtime, root, kind in registry.SKILL_ROOTS if kind == "bound-skill-root"]
        self.assertEqual(bound, [("bound-1", self.temp / "extra")])

        registry.rebuild(self.output, quiet=True)
        runtimes = {
            runtime
            for row in registry.load_registry(self.output)
            if row["type"] == "skill"
            for runtime in row["runtimes"]
        }
        self.assertEqual(runtimes, {"shared"})


class FallbackPrimaryTest(unittest.TestCase):
    def test_a_testing_only_pool_still_gets_a_primary_in_lane_order(self) -> None:
        records = [record("security-audit", "audit payment webhook race conditions", "testing-security")]
        with (
            mock.patch.object(registry, "semantic_hits", return_value={}),
            mock.patch.object(registry, "load_aliases", return_value={}),
            mock.patch.object(registry, "registry_manifest_fingerprint", return_value="fp"),
        ):
            result = registry.bundle(
                records, "audit our payment webhook for race conditions", "claude", "", 8, Path("/nonexistent")
            )
        lanes = [(item["lane"], item["name"]) for item in result["bundle"]]
        self.assertIn(("primary", "security-audit"), lanes)
        self.assertEqual(lanes[0][0], "primary", "the fallback primary must sort into lane order")


class SemanticBatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        registry._SEMANTIC_CACHE.clear()
        self.addCleanup(registry._SEMANTIC_CACHE.clear)

    def test_a_multi_intent_route_starts_the_sidecar_once(self) -> None:
        records = [
            record("outreach-writer", "draft investor outreach email"),
            record("webhook-auditor", "check payment webhook race conditions"),
        ]
        calls: list[list[str]] = []

        def sidecar(output, queries, fingerprint, runtime):
            calls.append(list(queries))
            return {query: {} for query in queries}

        with (
            mock.patch.object(registry, "_semantic_query_sidecar", side_effect=sidecar),
            mock.patch.object(registry, "load_aliases", return_value={}),
            mock.patch.object(registry, "registry_manifest_fingerprint", return_value="fp"),
        ):
            registry.bundle(
                records,
                "draft the investor outreach email and check the payment webhook for race conditions",
                "claude",
                "",
                8,
                Path("/nonexistent-batch"),
            )
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(len(calls[0]), 3, "the task and both intents share one sidecar call")

    def test_batch_protocol_round_trip(self) -> None:
        output = Path("/nonexistent-protocol")
        payload = {"results": [{"hits": [{"id": "skill:a", "cos": 0.8}]}, {"hits": []}]}
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")
        with (
            mock.patch.object(registry, "semantic_sidecar_script", return_value=Path(__file__)),
            mock.patch.object(registry, "load_json", return_value={"schema_version": registry.SEMANTIC_SCHEMA_VERSION}),
            mock.patch.object(registry, "semantic_interpreter", return_value=Path(__file__)),
            mock.patch.object(registry.subprocess, "run", return_value=completed) as run,
        ):
            hits = registry.semantic_hits_many(output, ["first", "second", "first"], "", "")
            again = registry.semantic_hits(output, "second", "", "")

        self.assertEqual(hits, {"first": {"skill:a": 0.8}, "second": {}})
        self.assertEqual(again, {})
        run.assert_called_once()
        self.assertIn("--batch", run.call_args.args[0])
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"queries": ["first", "second"]})


if __name__ == "__main__":
    unittest.main()
