"""Bundle size: set per call, by LOCKKEEPER_BUNDLE_SIZE, or by bundle_size in a config
file (10 when nothing is set), and filled only with capabilities that still fit."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402
import mcp_server  # noqa: E402
import route_hook  # noqa: E402
import router_config  # noqa: E402

from tests.test_registry_freshness import FreshnessFixture  # noqa: E402


def skill(name: str, description: str) -> dict:
    return {
        "id": f"skill:{name}",
        "type": "skill",
        "name": name,
        "description": description,
        "category": "software-engineering",
        "status": "active",
        "runtimes": ["claude"],
        "source_path": "",
        "registration_count": 1,
        "owner": "",
    }


def route(records: list[dict], query: str, size: int) -> list[str]:
    with (
        mock.patch.object(registry, "semantic_hits", return_value={}),
        mock.patch.object(registry, "load_aliases", return_value={}),
        mock.patch.object(registry, "registry_manifest_fingerprint", return_value="fp"),
    ):
        result = registry.bundle(records, query, "claude", "", size, Path("/nonexistent-bundle-size"))
    return [item["name"] for item in result["bundle"]]


class BundleSizeSettingTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="lockkeeper-bundle-size-")
        self.addCleanup(directory.cleanup)
        self.temp = Path(directory.name)

    def load(self, toml: str) -> router_config.RouterConfig:
        path = self.temp / "explicit.toml"
        path.write_text(toml, encoding="utf-8")
        with mock.patch.dict(os.environ, {"CAPABILITY_ROUTER_CONFIG": str(path)}):
            return router_config.load_router_config(
                script_path=self.temp / "router" / "scripts" / "capability_registry.py",
                include_repository=False,
            )

    def test_the_default_is_ten(self) -> None:
        self.assertEqual(router_config.resolve_bundle_size(self.load(""), {}), 10)

    def test_a_config_file_sets_the_size(self) -> None:
        self.assertEqual(router_config.resolve_bundle_size(self.load("bundle_size = 14\n"), {}), 14)

    def test_out_of_range_or_non_integer_sizes_are_rejected(self) -> None:
        for value in ("2", "21", '"12"', "true", "7.5"):
            with self.subTest(value=value), self.assertRaisesRegex(router_config.RouterConfigError, "bundle_size"):
                self.load(f"bundle_size = {value}\n")

    def test_the_environment_overrides_the_config_file(self) -> None:
        config = self.load("bundle_size = 14\n")
        self.assertEqual(router_config.resolve_bundle_size(config, {"LOCKKEEPER_BUNDLE_SIZE": " 6 "}), 6)

    def test_a_bad_environment_value_is_an_error_not_a_silent_default(self) -> None:
        config = self.load("")
        for value in ("50", "ten", "-3"):
            with self.subTest(value=value), self.assertRaisesRegex(router_config.RouterConfigError, "LOCKKEEPER"):
                router_config.resolve_bundle_size(config, {"LOCKKEEPER_BUNDLE_SIZE": value})


class BundleFillTest(unittest.TestCase):
    RELEVANT = [
        skill(f"webhook-{topic}", f"payment webhook {topic} for stripe events")
        for topic in ("retries", "signatures", "idempotency", "replay", "logging", "testing", "queues", "alerts")
    ]
    UNRELATED = [skill(f"unrelated-{index}", f"spreadsheet chart formatting number {index}") for index in range(20)]

    def test_a_bigger_size_returns_more_of_the_fitting_capabilities(self) -> None:
        records = [*self.RELEVANT, *self.UNRELATED]
        small = route(records, "payment webhook for stripe events", 4)
        large = route(records, "payment webhook for stripe events", 10)
        self.assertEqual(len(small), 4)
        self.assertEqual(len(large), 8, large)
        self.assertTrue(all(name.startswith("webhook-") for name in large), large)

    def test_the_size_is_a_ceiling_not_a_quota(self) -> None:
        records = [*self.RELEVANT[:2], *self.UNRELATED]
        names = route(records, "payment webhook for stripe events", 10)
        self.assertLessEqual(len(names), 4, "weak matches must not pad a narrow task up to the size")
        self.assertFalse([name for name in names if name.startswith("unrelated-")], names)

    def test_the_size_caps_the_bundle(self) -> None:
        self.assertEqual(len(route([*self.RELEVANT, *self.UNRELATED], "payment webhook for stripe events", 5)), 5)


class SurfacesUseTheSettingTest(unittest.TestCase):
    def run_cli(self, arguments: list[str], size: str) -> subprocess.CompletedProcess[str]:
        """The real CLI in a child process, with routing stubbed to report the size it got."""
        script = textwrap.dedent(
            f"""
            import sys
            import capability_registry as registry

            registry.ensure_query_registry_fresh = lambda *args, **kwargs: []
            registry.emit_bundle = lambda result, as_json: None

            def route_with_decision(records, query, runtime, project, max_count, output, **kwargs):
                print(f"size={{max_count}}")
                return {{}}

            registry.route_with_decision = route_with_decision
            sys.argv = ["lockkeeper", *{arguments!r}]
            raise SystemExit(registry.main())
            """
        )
        home = tempfile.mkdtemp(prefix="lockkeeper-bundle-size-home-")
        self.addCleanup(shutil.rmtree, home, True)
        environment = {**os.environ, "HOME": home, "USERPROFILE": home, "LOCKKEEPER_BUNDLE_SIZE": size}
        environment.pop("CAPABILITY_ROUTER_CONFIG", None)
        environment["PYTHONPATH"] = str(SCRIPTS_DIR)
        return subprocess.run(
            [sys.executable, "-c", script], env=environment, text=True, capture_output=True, check=False
        )

    def test_the_cli_defaults_to_the_setting_and_rejects_sizes_out_of_range(self) -> None:
        default = self.run_cli(["route", "payment", "webhook"], "7")
        self.assertIn("size=7", default.stdout, default.stderr)
        explicit = self.run_cli(["route", "--max", "12", "payment", "webhook"], "7")
        self.assertIn("size=12", explicit.stdout, explicit.stderr)
        too_big = self.run_cli(["route", "--max", "21", "payment", "webhook"], "7")
        self.assertNotIn("size=", too_big.stdout)
        self.assertIn("between 3 and 20", too_big.stdout + too_big.stderr)

    def test_the_hook_uses_the_setting(self) -> None:
        seen: list[int] = []

        def fake_bundle(records, query, runtime, project, max_count, output, **kwargs):
            seen.append(max_count)
            return {"bundle": []}

        payload = json.dumps({"prompt": "audit the payment webhook retries and signatures"})
        with (
            mock.patch.object(route_hook, "load_records", return_value=[skill("x", "y")]),
            mock.patch.object(registry, "bundle", side_effect=fake_bundle),
            mock.patch.object(sys, "stdin", io.StringIO(payload)),
            mock.patch.dict(os.environ, {"LOCKKEEPER_BUNDLE_SIZE": "9"}),
        ):
            route_hook.run([], Path("/nonexistent-hook"))
        self.assertEqual(seen, [9])

    def test_the_mcp_route_tool_uses_the_setting_and_allows_up_to_twenty(self) -> None:
        schema = next(tool for tool in mcp_server.TOOLS if tool["name"] == "route")["inputSchema"]
        self.assertEqual(schema["properties"]["max"]["maximum"], 20)
        seen: list[int] = []

        def fake_route(records, query, runtime, project, max_count, output, **kwargs):
            seen.append(max_count)
            return {"bundle": []}

        tools = mcp_server.LockkeeperTools("claude", "", Path("/nonexistent-mcp"))
        with (
            mock.patch.object(tools, "records", return_value=[]),
            mock.patch.object(registry, "route_with_decision", side_effect=fake_route),
            mock.patch.object(registry, "emit_bundle"),
            mock.patch.object(registry, "decision_settings", return_value=None),
            mock.patch.dict(os.environ, {"LOCKKEEPER_BUNDLE_SIZE": "11"}),
        ):
            tools.route({"task": "payment webhook"})
            tools.route({"task": "payment webhook", "max": 20})
        self.assertEqual(seen, [11, 20])


class BundleSizeFreshnessTest(FreshnessFixture):
    def test_changing_the_bundle_size_does_not_make_the_index_stale(self) -> None:
        config_file = self.temp / "router-config.toml"
        registry.configure_router(
            replace(registry.ROUTER_CONFIG, active_config_paths=(*registry.ROUTER_CONFIG.active_config_paths, config_file))
        )
        config_file.write_text('output_dir = "~/a"\nbundle_size = 10\n', encoding="utf-8")
        before = registry.authoritative_input_fingerprint()

        config_file.write_text('output_dir = "~/a"\nbundle_size = 16\n', encoding="utf-8")
        self.assertEqual(registry.authoritative_input_fingerprint(), before)

        config_file.write_text('output_dir = "~/b"\nbundle_size = 16\n', encoding="utf-8")
        self.assertNotEqual(registry.authoritative_input_fingerprint(), before, "other keys still count")


if __name__ == "__main__":
    unittest.main()
