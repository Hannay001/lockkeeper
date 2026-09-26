"""Lockkeeper as an MCP server: `lockkeeper mcp`.

Any MCP client (Claude Code, Codex, Cursor, Windsurf, Cline, ...) can then ask
Lockkeeper, from inside a session, which of the installed capabilities fit the
task at hand, and audit a skill before trusting it:

  route   the bounded, lane-structured bundle `lockkeeper route` returns
  search  ranked capabilities for keywords
  audit   the prompt-injection firewall on a file or folder

The server keeps the registry and its lexical index loaded between calls, so a
route inside a session costs the ranking alone; the registry is re-validated
(and self-heals, as on the CLI) at most every REVALIDATE_SECONDS.

Transport: MCP stdio -- newline-delimited JSON-RPC 2.0 on stdin/stdout. stdout
carries protocol messages only; everything else the router prints goes to
stderr. Standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

import capability_registry as registry
import telemetry

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
REVALIDATE_SECONDS = 30.0
RUNTIMES = ["claude", "codex", "hermes", "jcode", "shared"]
SERVER_INSTRUCTIONS = (
    "Lockkeeper indexes every skill, MCP server, plugin, tool, agent and command installed on this "
    "machine. At the start of a non-trivial task, call `route` with the task in the user's words and "
    "load only the capabilities it returns (read the SKILL.md files at their load_path); do not read "
    "whole skill directories. Call `audit` on any skill or plugin folder before trusting or "
    "installing it."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "route",
        "title": "Route a task to the right capabilities",
        "description": (
            "Pick the few installed skills, MCP servers, plugins, tools, agents and commands that fit a "
            "task, before starting it. Returns a bounded bundle (10 by default) grouped by role (primary, "
            "context, integration, execution, verification, support), each with why it was chosen and "
            "how to use it: for skills, the exact SKILL.md path to read. Use this instead of scanning "
            "skill lists; it keeps the context small."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task or user prompt, in plain words."},
                "runtime": {
                    "type": "string",
                    "enum": RUNTIMES,
                    "description": "The agent that will use the bundle (defaults to the server's --runtime).",
                },
                "max": {
                    "type": "integer",
                    "minimum": 3,
                    "maximum": 20,
                    "description": "Bundle size (default: the configured bundle_size, 10 unless changed).",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "search",
        "title": "Search installed capabilities",
        "description": "Rank installed capabilities for keywords; returns names, types, scores and load paths.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords or a short task description."},
                "runtime": {"type": "string", "enum": RUNTIMES},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Results (default 10)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "audit",
        "title": "Audit a skill, plugin or config for prompt injection",
        "description": (
            "Scan a file or folder (a skill, plugin, MCP config, script) for instruction overrides, "
            "exfiltration, obfuscated execution, destructive commands and hidden text. Returns a verdict "
            "(clean, suspect, hostile) with findings. Run it before trusting or installing a capability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory to scan."},
                "recursive": {"type": "boolean", "description": "Scan a directory recursively (default true)."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


class ToolError(ValueError):
    """A bad tool call: reported to the client as an isError result, not a protocol error."""


class LockkeeperTools:
    def __init__(self, runtime: str, project: str, output: Path) -> None:
        self.runtime = runtime
        self.project = project
        self.output = output
        self._records: Optional[list[dict[str, Any]]] = None
        self._checked_at = 0.0

    def records(self) -> list[dict[str, Any]]:
        """The registry, re-validated (and self-healed) at most every REVALIDATE_SECONDS."""
        now = time.monotonic()
        if self._records is None or now - self._checked_at > REVALIDATE_SECONDS:
            records = registry.ensure_query_registry_fresh(self.output)
            self._records = records if records is not None else registry.load_registry(self.output, verify_sources=False)
            self._checked_at = now
        return self._records

    def _runtime(self, arguments: dict[str, Any]) -> str:
        runtime = arguments.get("runtime") or self.runtime
        if runtime not in RUNTIMES:
            raise ToolError(f"runtime must be one of {', '.join(RUNTIMES)}")
        return runtime

    @staticmethod
    def _text(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _integer(arguments: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
        value = arguments.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ToolError(f"{key} must be an integer from {low} to {high}")
        return value

    def route(self, arguments: dict[str, Any]) -> str:
        query, clipped = registry.focus_query(self._text(arguments, "task")[: registry.MAX_QUERY_INPUT_CHARS])
        if not query:
            raise ToolError("task must contain at least one word")
        runtime = self._runtime(arguments)
        max_count = self._integer(arguments, "max", registry.configured_bundle_size(), *registry.BUNDLE_SIZE_RANGE)
        result = registry.route_with_decision(
            self.records(),
            query,
            runtime,
            self.project,
            max_count,
            self.output,
            settings=registry.decision_settings(None),
            verify_sources=True,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            registry.emit_bundle(result, False)
        note = "note: the task was long; routed on its opening.\n" if clipped else ""
        return note + buffer.getvalue()

    def search(self, arguments: dict[str, Any]) -> str:
        query, _clipped = registry.focus_query(self._text(arguments, "query")[: registry.MAX_QUERY_INPUT_CHARS])
        if not query:
            raise ToolError("query must contain at least one word")
        limit = self._integer(arguments, "limit", 10, 1, 50)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            registry.emit_search(
                self.records(), query, self._runtime(arguments), limit, False, self.output, verify_sources=True
            )
        return buffer.getvalue()

    def audit(self, arguments: dict[str, Any]) -> str:
        import cap_audit

        target = Path(self._text(arguments, "path")).expanduser()
        if not target.exists():
            raise ToolError(f"no such file or directory: {target}")
        recursive = arguments.get("recursive", True)
        if not isinstance(recursive, bool):
            raise ToolError("recursive must be true or false")
        # --strict makes the exit code carry the verdict (0 clean, 1 suspect, 2 hostile).
        argv = [str(target), "--strict", *(["--recursive"] if recursive and target.is_dir() else [])]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cap_audit.main(argv)
        verdict = {0: "clean", 1: "suspect", 2: "hostile"}.get(code, f"exit {code}")
        return f"verdict: {verdict}\n{buffer.getvalue()}"

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        handler: Optional[Callable[[dict[str, Any]], str]] = {
            "route": self.route,
            "search": self.search,
            "audit": self.audit,
        }.get(name)
        if handler is None:
            raise ToolError(f"unknown tool: {name}")
        started = time.perf_counter()
        failed = True
        try:
            text = handler(arguments)
            failed = False
            return text
        finally:
            argv = [name, *(["--runtime", str(arguments.get("runtime") or self.runtime)] if name != "audit" else [])]
            telemetry.record(argv, time.perf_counter() - started, failed)


def _response(message_id: Any, result: Any = None, error: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        reply["error"] = error
    else:
        reply["result"] = result
    return reply


def handle(message: Any, tools: LockkeeperTools) -> Optional[dict[str, Any]]:
    """One JSON-RPC message in, its response out (None for notifications)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _response(message.get("id") if isinstance(message, dict) else None,
                         error={"code": -32600, "message": "invalid request"})
    method, message_id = message["method"], message.get("id")
    is_notification = "id" not in message
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return None if is_notification else _response(message_id, error={"code": -32602, "message": "params must be an object"})
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return _response(
            message_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "lockkeeper", "title": "Lockkeeper", "version": telemetry.version()},
                "instructions": SERVER_INSTRUCTIONS,
            },
        )
    if is_notification:
        return None  # notifications/initialized, notifications/cancelled, ...
    if method == "ping":
        return _response(message_id, {})
    if method == "tools/list":
        return _response(message_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _response(message_id, error={"code": -32602, "message": "tools/call needs a name and an arguments object"})
        try:
            text = tools.call(name, arguments)
            return _response(message_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except ToolError as error:
            return _response(message_id, {"content": [{"type": "text", "text": f"error: {error}"}], "isError": True})
        except (OSError, RuntimeError, ValueError) as error:
            detail = registry.redact_sensitive_text(error)
            return _response(message_id, {"content": [{"type": "text", "text": f"error: {detail}"}], "isError": True})
    return _response(message_id, error={"code": -32601, "message": f"method not found: {method}"})


def serve(stdin: TextIO, protocol_out: TextIO, tools: LockkeeperTools) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: Optional[dict[str, Any]] = _response(None, error={"code": -32700, "message": "parse error"})
        else:
            reply = handle(message, tools)
        if reply is not None:
            protocol_out.write(json.dumps(reply, ensure_ascii=False) + "\n")
            protocol_out.flush()


def main(argv: Optional[list[str]] = None, project: str = "") -> int:
    parser = argparse.ArgumentParser(prog="lockkeeper mcp", description="Serve Lockkeeper over MCP (stdio).")
    parser.add_argument("--runtime", choices=RUNTIMES, default="claude", help="default runtime for route and search")
    args = parser.parse_args(argv)
    registry.ensure_router_config_valid()
    protocol_out = sys.stdout
    # Everything the router prints from here on is diagnostics: keep stdout for protocol messages.
    sys.stdout = sys.stderr
    try:
        serve(sys.stdin, protocol_out, LockkeeperTools(args.runtime, project, registry.ROUTER_CONFIG.output_dir))
    finally:
        sys.stdout = protocol_out
    return 0
