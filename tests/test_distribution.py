"""The Claude Code plugin, its marketplace and the MCP Registry entry stay in step
with the package: same version, same router skill, the right commands."""
from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "lockkeeper"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class DistributionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

    def test_every_manifest_carries_the_package_version(self) -> None:
        marketplace = load(ROOT / ".claude-plugin" / "marketplace.json")
        server = load(ROOT / "server.json")
        self.assertEqual(load(PLUGIN / ".claude-plugin" / "plugin.json")["version"], self.version)
        self.assertEqual([plugin["version"] for plugin in marketplace["plugins"]], [self.version])
        self.assertEqual(server["version"], self.version)
        self.assertEqual([package["version"] for package in server["packages"]], [self.version])

    def test_the_plugin_ships_the_same_router_skill(self) -> None:
        self.assertEqual(
            (PLUGIN / "skills" / "capability-router" / "SKILL.md").read_bytes(),
            (ROOT / "skills" / "capability-router" / "SKILL.md").read_bytes(),
        )

    def test_the_plugin_routes_prompts_and_serves_mcp(self) -> None:
        hook = load(PLUGIN / "hooks" / "hooks.json")["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn("lockkeeper route-hook", hook)
        self.assertTrue(hook.rstrip().endswith("|| true"), "a missing command must never block a prompt")
        server = load(PLUGIN / ".mcp.json")["mcpServers"]["lockkeeper"]
        self.assertEqual((server["command"], server["args"][0]), ("lockkeeper", "mcp"))
        marketplace = load(ROOT / ".claude-plugin" / "marketplace.json")
        self.assertEqual(marketplace["plugins"][0]["source"], "./plugins/lockkeeper")

    def test_the_readme_proves_ownership_to_the_mcp_registry(self) -> None:
        name = load(ROOT / "server.json")["name"]
        self.assertIn(f"mcp-name: {name}`", (ROOT / "README.md").read_text(encoding="utf-8"))
        package = load(ROOT / "server.json")["packages"][0]
        self.assertEqual((package["registryType"], package["identifier"]), ("pypi", "lockkeeper"))


if __name__ == "__main__":
    unittest.main()
