"""Opt-in telemetry: off by default, anonymous, never blocks or breaks a command."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import telemetry  # noqa: E402

PROMPT = "migrate the secret-project-omega auth module in /home/alice/src"


class _Collector(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        type(self).received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


class TelemetryTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="lockkeeper-telemetry-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        clean = {key: value for key, value in os.environ.items() if key not in {
            "CI", "DO_NOT_TRACK", telemetry.SWITCH_ENV, telemetry.ENDPOINT_ENV, "XDG_CONFIG_HOME", "XDG_STATE_HOME",
        }}
        clean.update({"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(telemetry.cli(list(argv)), 0)
        return buffer.getvalue()

    def route(self) -> None:
        telemetry.record(["route", "--runtime", "claude", PROMPT], 0.25, False, 1234)

    def test_off_by_default_and_records_nothing(self) -> None:
        self.assertFalse(telemetry.enabled())
        self.route()
        self.assertFalse(telemetry.spool_path().exists())
        self.assertFalse(telemetry.settings_path().exists())

    def test_on_records_only_anonymous_counts(self) -> None:
        self.cli("on")
        self.route()
        telemetry.record(["audit", "/home/alice/skills/x"], 0.02, True)
        payload = telemetry.payloads()[0]
        self.assertEqual(payload["commands"]["route"], {"count": 1, "failed": 0, "latency": {"100-300ms": 1}})
        self.assertEqual(payload["commands"]["audit"]["failed"], 1)
        self.assertEqual(payload["runtimes"], {"claude": 1})
        self.assertEqual(payload["registry_size"], "1k-10k")
        self.assertEqual(len(payload["install_id"]), 32)
        serialized = json.dumps(payload)
        for secret in ("secret-project-omega", "alice", "/home", "auth module", "skills/x"):
            self.assertNotIn(secret, serialized)

    def test_unknown_commands_and_runtimes_are_not_passed_through(self) -> None:
        self.assertEqual(telemetry.command_name(["my-private-verb"]), "other")
        self.assertEqual(telemetry.command_name(["bundle"]), "route")
        self.assertEqual(telemetry.runtime_name(["route", "--runtime=my-harness"]), "other")
        self.assertEqual(telemetry.runtime_name(["route", "--runtime", "codex"]), "codex")

    def test_environment_always_wins(self) -> None:
        self.cli("on")
        for key, value in (("DO_NOT_TRACK", "1"), (telemetry.SWITCH_ENV, "off"), ("CI", "true")):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}):
                self.assertFalse(telemetry.enabled())
                self.assertIn("off", self.cli("status").splitlines()[0])

    def test_off_deletes_the_summary_and_the_install_id(self) -> None:
        self.cli("on")
        self.route()
        self.cli("off")
        self.assertFalse(telemetry.spool_path().exists())
        self.assertNotIn("install_id", telemetry.settings())
        self.assertFalse(telemetry.enabled())

    def test_endpoints_must_be_https_unless_loopback(self) -> None:
        telemetry.check_endpoint("https://collector.example/v1")
        telemetry.check_endpoint("http://127.0.0.1:9/v1")
        for bad in ("http://collector.example/v1", "ftp://x/y", "https://user:pw@collector.example/"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                telemetry.check_endpoint(bad)

    def test_nothing_is_sent_without_an_endpoint(self) -> None:
        self.cli("on")
        self.route()
        with mock.patch.object(telemetry.urllib.request, "urlopen") as urlopen:
            self.assertEqual(telemetry.flush(), 0)
        urlopen.assert_not_called()

    def test_complete_days_are_sent_once_and_route_never_waits_on_the_network(self) -> None:
        _Collector.received = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.cli("on", "--endpoint", f"http://127.0.0.1:{server.server_port}/collect")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        telemetry._write_json(
            telemetry.spool_path(), {"days": {yesterday: {"commands": {"route": {"count": 3}}, "runtimes": {}}}}
        )
        self.route()  # route never flushes
        self.assertEqual(_Collector.received, [])
        telemetry.record(["rebuild"], 1.0, False)  # a maintenance command sends complete days
        self.assertEqual([item["day"] for item in _Collector.received], [yesterday])
        self.assertEqual(list(telemetry._read_json(telemetry.spool_path())["days"]), [date.today().isoformat()])

    def test_a_dead_endpoint_never_raises(self) -> None:
        self.cli("on", "--endpoint", "http://127.0.0.1:9/collect")
        self.route()
        self.assertEqual(telemetry.flush(), 0)
        with mock.patch.object(telemetry, "_write_json", side_effect=OSError("disk full")):
            telemetry.record(["route"], 0.1, False)  # must not raise


class _Terminal(io.StringIO):
    """A stream that says whether it is a terminal, like sys.stdin/sys.stdout do."""

    def __init__(self, text: str = "", tty: bool = True) -> None:
        super().__init__(text)
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class AskOnceTest(TelemetryTest):
    def ask(self, answers: str, *, tty: bool = True, environ: dict | None = None) -> tuple[object, str]:
        stdout = _Terminal(tty=tty)
        return telemetry.ask_once(_Terminal(answers, tty=tty), stdout, environ), stdout.getvalue()

    def test_enter_means_yes_and_the_question_is_asked_only_once(self) -> None:
        choice, shown = self.ask("\n")
        self.assertIs(choice, True)
        self.assertIn("Never prompts, skill names, file paths or code", shown)
        self.assertIn("[Y/n]", shown)
        self.assertTrue(telemetry.enabled())
        self.assertEqual(self.ask("n\n"), (None, ""), "an answered question is never asked again")
        self.assertTrue(telemetry.enabled())

    def test_no_turns_it_off_and_is_remembered(self) -> None:
        self.assertIs(self.ask("No\n")[0], False)
        self.assertTrue(telemetry.decided())
        self.assertFalse(telemetry.enabled())
        self.assertEqual(self.ask("\n"), (None, ""))
        self.assertFalse(telemetry.enabled())

    def test_it_never_asks_outside_an_interactive_terminal(self) -> None:
        self.assertEqual(self.ask("y\n", tty=False), (None, ""))
        stdout = _Terminal()
        self.assertIsNone(telemetry.ask_once(_Terminal("y\n", tty=False), stdout))
        self.assertIsNone(telemetry.ask_once(_Terminal("y\n"), _Terminal(tty=False)))
        self.assertEqual(stdout.getvalue(), "")
        self.assertFalse(telemetry.decided())

    def test_do_not_track_ci_and_the_switch_suppress_the_question(self) -> None:
        for environ in ({"DO_NOT_TRACK": "1"}, {"CI": "true"}, {telemetry.SWITCH_ENV: "0"}):
            with self.subTest(environ=environ):
                self.assertEqual(self.ask("y\n", environ=environ), (None, ""))
        self.assertFalse(telemetry.decided())

    def test_no_answer_leaves_the_question_for_next_time(self) -> None:
        self.assertIsNone(self.ask("")[0])
        self.assertFalse(telemetry.decided())

    def test_unclear_answers_are_asked_again_then_count_as_no(self) -> None:
        self.assertIs(self.ask("maybe\nyes\n")[0], True)
        self.cli("off")
        settings = telemetry.settings_path()
        settings.unlink()
        self.assertIs(self.ask("a\nb\nc\n")[0], False)
        self.assertFalse(telemetry.enabled())

    def test_setup_commands_ask_and_other_commands_do_not(self) -> None:
        import route_hook

        with (
            mock.patch.object(telemetry, "ask_once") as ask,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(route_hook.setup_cli(["show", "claude"]), 0)
            self.assertEqual(route_hook.setup_cli(["remove", "claude"]), 0)
            ask.assert_not_called()
            self.assertEqual(route_hook.setup_cli(["install", "claude"]), 0)
            ask.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
