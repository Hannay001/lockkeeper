"""`lockkeeper route-hook`: adds routing context to a prompt, never blocks one."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402
import route_hook  # noqa: E402


def skill(name: str, description: str) -> dict:
    return {
        "id": f"skill:{name}",
        "type": "skill",
        "name": name,
        "description": description,
        "category": "software-engineering",
        "status": "active",
        "runtimes": ["claude"],
        "source_path": f"/skills/{name}/SKILL.md",
        "registration_count": 1,
        "owner": "",
        "keywords": "",
    }


RECORDS = [
    skill("pcap-analysis", "Analyze network packet captures (PCAP) and compute traffic statistics"),
    skill("pdf-tables", "Extract tables from PDF reports"),
    skill("git-commit", "Write conventional git commit messages"),
]


class RouteHookTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="lockkeeper-hook-")
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        for patcher in (
            mock.patch.object(route_hook, "load_records", return_value=list(RECORDS)),
            mock.patch.object(registry, "record_source_is_trusted", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def hook(self, stdin: str, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(buffer):
            code = route_hook.run(list(argv), self.output)
        return code, buffer.getvalue()

    def test_a_task_prompt_gets_its_capabilities_as_context(self) -> None:
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "count TCP flows in capture.pcap"})
        code, out = self.hook(payload)
        self.assertEqual(code, 0)
        context = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "UserPromptSubmit")
        self.assertIn("skill pcap-analysis [primary]", context["additionalContext"])
        self.assertNotIn("git-commit", context["additionalContext"])

    def test_text_format_prints_the_context_itself(self) -> None:
        code, out = self.hook(json.dumps({"prompt": "extract tables from pdf reports"}), "--format", "text")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Lockkeeper matched this request"))

    def test_nothing_is_added_when_there_is_nothing_to_route(self) -> None:
        for prompt in ("thanks!", "/clear", "", "ok go", "tell me a joke about otters"):
            with self.subTest(prompt=prompt):
                code, out = self.hook(json.dumps({"prompt": prompt}))
                self.assertEqual((code, out), (0, ""))

    def test_errors_never_block_the_prompt(self) -> None:
        with mock.patch.object(registry, "bundle", side_effect=RuntimeError("boom")):
            code, out = self.hook(json.dumps({"prompt": "count TCP flows in capture.pcap"}))
        self.assertEqual((code, out), (0, ""))
        with mock.patch.object(route_hook, "load_records", return_value=None):
            self.assertEqual(self.hook(json.dumps({"prompt": "count TCP flows in capture.pcap"})), (0, ""))


class HookSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="lockkeeper-settings-")
        self.addCleanup(directory.cleanup)
        self.settings = Path(directory.name) / ".claude" / "settings.json"
        self.settings.parent.mkdir()

    def test_install_is_idempotent_and_keeps_everything_else(self) -> None:
        original = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}]}}
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(route_hook.install(self.settings, firewall=True), ["UserPromptSubmit", "PreToolUse"])
        self.assertEqual(route_hook.install(self.settings, firewall=True), [])
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"]["Stop"], original["hooks"]["Stop"])
        prompt_hook = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(prompt_hook["type"], "command")
        self.assertIn("route-hook", prompt_hook["command"])
        self.assertEqual(data["hooks"]["PreToolUse"][0]["matcher"], "*")
        self.assertEqual(json.loads(self.settings.with_name("settings.json.lockkeeper-backup").read_text()), original)

    def test_the_firewall_is_opt_in(self) -> None:
        self.assertEqual(route_hook.install(self.settings, firewall=False), ["UserPromptSubmit"])
        self.assertNotIn("PreToolUse", json.loads(self.settings.read_text(encoding="utf-8"))["hooks"])

    def test_remove_takes_out_only_lockkeeper(self) -> None:
        other = {"hooks": [{"type": "command", "command": "my-own-hook"}]}
        self.settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [other]}}), encoding="utf-8")
        route_hook.install(self.settings, firewall=True)
        self.assertEqual(route_hook.remove(self.settings), ["UserPromptSubmit", "PreToolUse"])
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), {"hooks": {"UserPromptSubmit": [other]}})

    def test_an_unreadable_settings_file_is_never_overwritten(self) -> None:
        self.settings.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            route_hook.install(self.settings, firewall=False)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{ not json")


class FreshInstallTest(unittest.TestCase):
    def test_no_index_yet_means_no_context_and_no_rebuild(self) -> None:
        """The first prompts after install must not wait on building the index."""
        with tempfile.TemporaryDirectory(prefix="lockkeeper-hook-home-") as home:
            environment = {
                **{key: value for key, value in os.environ.items() if key != "CAPABILITY_ROUTER_CONFIG"},
                "HOME": home,
                "USERPROFILE": home,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "capability_registry.py"), "route-hook"],
                input=json.dumps({"prompt": "count TCP flows in capture.pcap"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                timeout=120,
                check=False,
            )
            self.assertEqual((proc.returncode, proc.stdout), (0, ""), proc.stderr[-1000:])
            self.assertFalse((Path(home) / ".agents" / "capabilities" / "registry.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
