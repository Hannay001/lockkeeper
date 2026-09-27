"""Opt-in, anonymous usage telemetry for Lockkeeper.

OFF by default. Nothing is recorded, stored or sent until a user says yes:
`lockkeeper init` and `lockkeeper hooks install` ask once, in an interactive
terminal only (ask_once), and `lockkeeper telemetry on|off` decides at any time.
DO_NOT_TRACK=1, LOCKKEEPER_TELEMETRY=0 or a CI environment always force it off
and suppress the question. See docs/TELEMETRY.md.

What is collected, as ONE aggregate per day -- never per-command events:

  * a random install id (uuid4; not derived from the machine or the user),
  * Lockkeeper version, Python major.minor, OS family,
  * per command (route, search, audit, rebuild, ...): how many runs, how many
    failed, and a latency histogram in coarse buckets,
  * which --runtime values were used (claude, codex, ...),
  * the registry size as a bucket ("1k-10k").

What is never collected: prompts or task text, capability names, file paths,
hostnames, user names, environment variables, error messages.

Sending needs an endpoint (LOCKKEEPER_TELEMETRY_ENDPOINT or `telemetry on
--endpoint URL`); with none configured, enabled telemetry only keeps the local
daily summary that `lockkeeper telemetry show` prints. Complete days are sent
once, best effort, with a short timeout, during maintenance commands (rebuild,
doctor, check) or `telemetry flush` -- never while an agent waits on a route.
Failures are silent and never affect the command. Standard library only.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1
# Deliberately unset: the maintainers configure a collector before anything can
# be sent. An endpoint may also come from the environment or `telemetry on`.
DEFAULT_ENDPOINT = ""
ENDPOINT_ENV = "LOCKKEEPER_TELEMETRY_ENDPOINT"
SWITCH_ENV = "LOCKKEEPER_TELEMETRY"
SEND_TIMEOUT_SECONDS = 2.0
MAX_PENDING_DAYS = 14
DOCS = "docs/TELEMETRY.md"

COMMANDS = frozenset(
    {
        "route", "bundle", "search", "audit", "hook", "rebuild", "reindex", "check",
        "doctor", "init", "snapshot-runtimes", "export-csv", "link", "mcp", "route-hook", "hooks",
    }
)
RUNTIMES = frozenset({"claude", "codex", "hermes", "jcode", "shared"})
# Sending (at most once a day, 2 s timeout) rides only on maintenance commands, never
# on route/search/hook, where an agent is waiting.
FLUSH_COMMANDS = frozenset({"rebuild", "doctor", "check", "init", "reindex", "snapshot-runtimes"})
LATENCY_BUCKETS = ((100, "<100ms"), (300, "100-300ms"), (1000, "300ms-1s"), (3000, "1-3s"), (10000, "3-10s"))
SIZE_BUCKETS = ((100, "<100"), (1000, "100-1k"), (10000, "1k-10k"), (100000, "10k-100k"))


# ---------------------------------------------------------------- locations


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "lockkeeper"


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "lockkeeper"


def settings_path() -> Path:
    return _config_dir() / "telemetry.json"


def spool_path() -> Path:
    return _state_dir() / "telemetry-pending.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- switches


def forced_off_reason(environ: Optional[dict[str, str]] = None) -> str:
    """Why telemetry is off regardless of settings, or ""."""
    env = os.environ if environ is None else environ
    if env.get("DO_NOT_TRACK", "").strip().lower() not in ("", "0", "false", "no"):
        return "DO_NOT_TRACK is set"
    if env.get(SWITCH_ENV, "").strip().lower() in ("0", "off", "false", "no"):
        return f"{SWITCH_ENV}={env[SWITCH_ENV]}"
    if env.get("CI", "").strip().lower() not in ("", "0", "false", "no"):
        return "running in CI"
    return ""


def settings() -> dict[str, Any]:
    return _read_json(settings_path())


def decided() -> bool:
    """True once the user has chosen, either way (the prompt, `telemetry on` or `off`)."""
    return isinstance(settings().get("enabled"), bool)


def enabled() -> bool:
    return settings().get("enabled") is True and not forced_off_reason()


def endpoint() -> str:
    return (os.environ.get(ENDPOINT_ENV) or settings().get("endpoint") or DEFAULT_ENDPOINT).strip()


def check_endpoint(url: str) -> str:
    """https only, except loopback (for testing a local collector)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"telemetry endpoint must be an http(s) URL: {url}")
    if parsed.username or parsed.password:
        raise ValueError("telemetry endpoint must not embed credentials")
    host = parsed.hostname
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if parsed.scheme != "https" and not loopback:
        raise ValueError("telemetry endpoint must use https://")
    return url


# ---------------------------------------------------------------- recording


def version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version as package_version

        return package_version("lockkeeper")
    except (ImportError, PackageNotFoundError):
        pass
    try:
        import tomllib

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        return str(tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"])
    except (OSError, KeyError, ValueError):
        return "unknown"


def _bucket(value: float, buckets: tuple[tuple[int, str], ...], last: str) -> str:
    return next((label for limit, label in buckets if value < limit), last)


def command_name(argv: list[str]) -> str:
    """The subcommand, only if it is a known one; anything else is "other"."""
    token = next((item for item in argv if not item.startswith("-")), "")
    if token == "bundle":
        token = "route"
    return token if token in COMMANDS else "other"


def runtime_name(argv: list[str]) -> str:
    for index, item in enumerate(argv):
        value = item.split("=", 1)[1] if item.startswith("--runtime=") else (
            argv[index + 1] if item == "--runtime" and index + 1 < len(argv) else None
        )
        if value is not None:
            return value if value in RUNTIMES else "other"
    return ""


def size_bucket(capabilities: Optional[int]) -> str:
    if capabilities is None:
        return ""
    return _bucket(capabilities, SIZE_BUCKETS, "100k+")


def record(argv: list[str], seconds: float, failed: bool, capabilities: Optional[int] = None) -> None:
    """Add one command run to today's local summary. Never raises; a no-op when off."""
    try:
        if not enabled():
            return
        spool = _read_json(spool_path())
        today = date.today().isoformat()
        day = spool.setdefault("days", {}).setdefault(today, {"commands": {}, "runtimes": {}})
        command = command_name(argv)
        entry = day["commands"].setdefault(command, {"count": 0, "failed": 0, "latency": {}})
        entry["count"] += 1
        entry["failed"] += int(bool(failed))
        latency = _bucket(seconds * 1000, LATENCY_BUCKETS, "10s+")
        entry["latency"][latency] = entry["latency"].get(latency, 0) + 1
        runtime = runtime_name(argv)
        if runtime:
            day["runtimes"][runtime] = day["runtimes"].get(runtime, 0) + 1
        bucket = size_bucket(capabilities)
        if bucket:
            day["registry_size"] = bucket
        # Keep a bounded backlog when nothing is being sent.
        for stale in sorted(spool["days"])[:-MAX_PENDING_DAYS]:
            del spool["days"][stale]
        _write_json(spool_path(), spool)
        if command in FLUSH_COMMANDS:
            flush(complete_days_only=True)
    except Exception:  # noqa: BLE001 - telemetry must never affect a command
        return


def payloads(complete_days_only: bool = False) -> list[dict[str, Any]]:
    """Exactly what would be sent: one object per pending day."""
    current = settings()
    install_id = current.get("install_id", "")
    today = date.today().isoformat()
    result = []
    for day, summary in sorted(_read_json(spool_path()).get("days", {}).items()):
        if complete_days_only and day >= today:
            continue
        result.append(
            {
                "schema": SCHEMA_VERSION,
                "install_id": install_id,
                "day": day,
                "version": version(),
                "python": ".".join(platform.python_version_tuple()[:2]),
                "os": sys.platform if sys.platform in ("linux", "darwin", "win32") else "other",
                **summary,
            }
        )
    return result


def flush(complete_days_only: bool = False) -> int:
    """Send pending days to the endpoint; return how many were sent. Never raises."""
    try:
        target = endpoint()
        if not enabled() or not target:
            return 0
        check_endpoint(target)
        sent_days = []
        for payload in payloads(complete_days_only):
            request = urllib.request.Request(  # noqa: S310 - endpoint validated above
                target,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": f"lockkeeper/{payload['version']}"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:  # noqa: S310
                if not 200 <= response.status < 300:
                    break
            sent_days.append(payload["day"])
        if sent_days:
            spool = _read_json(spool_path())
            for day in sent_days:
                spool.get("days", {}).pop(day, None)
            _write_json(spool_path(), spool)
        return len(sent_days)
    except Exception:  # noqa: BLE001 - best effort by design
        return 0


# ---------------------------------------------------------------- choosing


def turn_on(collector: str = "") -> None:
    current = settings()
    if collector:
        current["endpoint"] = check_endpoint(collector.strip())
    current.update(
        {
            "enabled": True,
            "install_id": current.get("install_id") or uuid.uuid4().hex,
            "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _write_json(settings_path(), current)


def turn_off() -> None:
    _write_json(
        settings_path(),
        {"enabled": False, "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    )
    spool_path().unlink(missing_ok=True)


ASK_TEXT = f"""
Help improve Lockkeeper?
  Lockkeeper can share anonymous daily counts: which commands ran and how long they
  took, which agents you route for, your index size as a range, and the Lockkeeper,
  Python and OS versions. Never prompts, skill names, file paths or code. See exactly
  what with `lockkeeper telemetry show`; stop any time with `lockkeeper telemetry off`.
  Details: https://github.com/Hannay001/lockkeeper/blob/main/{DOCS}"""
ASK_QUESTION = "Share anonymous usage counts? [Y/n] "


def ask_once(stdin: Any = None, stdout: Any = None, environ: Optional[dict[str, str]] = None) -> Optional[bool]:
    """Ask whether to share anonymous usage counts, once per machine.

    Only in an interactive terminal (stdin and stdout both a TTY), never when
    DO_NOT_TRACK, LOCKKEEPER_TELEMETRY or CI already decide, and never again once
    answered either way. Enter means yes. Returns the choice, or None when it
    didn't ask or got no answer (end of input, Ctrl-C), which leaves the question
    for next time. It never raises: setup must not fail over telemetry.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        if forced_off_reason(environ) or decided() or not (stdin.isatty() and stdout.isatty()):
            return None
        print(ASK_TEXT, file=stdout)
        choice: Optional[bool] = None
        for _attempt in range(3):
            stdout.write(ASK_QUESTION)
            stdout.flush()
            line = stdin.readline()
            if not line:
                print(file=stdout)
                return None
            answer = line.strip().lower()
            if answer in ("", "y", "yes"):
                choice = True
                break
            if answer in ("n", "no"):
                choice = False
                break
        if choice is None:
            choice = False
        if choice:
            turn_on()
            print("telemetry: on -- thank you. Stop any time with `lockkeeper telemetry off`.", file=stdout)
        else:
            turn_off()
            print("telemetry: off. You won't be asked again; `lockkeeper telemetry on` changes it.", file=stdout)
        return choice
    except (KeyboardInterrupt, OSError, ValueError):
        return None


# ---------------------------------------------------------------- CLI


def status_lines() -> list[str]:
    current = settings()
    forced = forced_off_reason()
    state = "on" if current.get("enabled") is True else "off"
    if forced and state == "on":
        state = f"off ({forced}; your setting is on)"
    target = endpoint()
    lines = [
        f"telemetry: {state}",
        f"endpoint: {target or 'not configured -- nothing is sent'}",
        f"install id: {current.get('install_id') or '(none)'}",
        f"pending days: {len(_read_json(spool_path()).get('days', {}))}",
        f"what is collected: {DOCS} (see it with `lockkeeper telemetry show`)",
    ]
    return lines


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lockkeeper telemetry",
        description="Opt-in, anonymous daily usage counts. Off unless you turn it on.",
    )
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("status", help="show whether telemetry is on and where it would be sent")
    on = sub.add_parser("on", help="share anonymous daily usage counts")
    on.add_argument("--endpoint", help="collector URL (https)")
    sub.add_parser("off", help="stop, and delete the local summary and install id")
    sub.add_parser("show", help="print exactly what would be sent")
    sub.add_parser("flush", help="send pending days now (including today)")
    args = parser.parse_args(argv)
    action = args.action or "status"
    if action == "on":
        turn_on(args.endpoint or "")
        print("telemetry: on -- thank you. Anonymous daily counts only; never prompts, names or paths.")
        print("\n".join(status_lines()[1:]))
        return 0
    if action == "off":
        turn_off()
        print("telemetry: off. The local summary and install id were deleted.")
        return 0
    if action == "show":
        print(json.dumps(payloads(), indent=2, sort_keys=True))
        return 0
    if action == "flush":
        if not enabled():
            print("telemetry: off -- nothing to send")
            return 0
        if not endpoint():
            print("telemetry: no endpoint configured -- nothing was sent")
            return 0
        print(f"sent {flush()} day(s)")
        return 0
    print("\n".join(status_lines()))
    return 0


def timed(argv: list[str]) -> "_Timer":
    return _Timer(argv)


class _Timer:
    """with telemetry.timed(argv) as run: ...; run.failed / run.capabilities may be set."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.failed = False
        self.capabilities: Optional[int] = None
        self._started = 0.0

    def __enter__(self) -> "_Timer":
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        failed = self.failed or (exc_type is not None and not (exc_type is SystemExit and not getattr(exc, "code", 0)))
        record(self.argv, time.perf_counter() - self._started, failed, self.capabilities)
