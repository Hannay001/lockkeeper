#!/usr/bin/env python3
"""System binding for cap: discover installed agent harnesses, bind, report.

`lockkeeper init`  — scan the machine, write config/local.toml bindings.
`lockkeeper doctor` — show what was found and whether routing is healthy.

Harness detection is deliberately generic so future runtimes work without a
cap release: anything under $HOME that looks like an agent harness (a dot-dir
containing skills/, or plugins/cache) is picked up by the generic scanner,
in addition to the well-known names below. Standard-library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HOME = Path.home()

# (name, home dot-dirs relative to $HOME, CLI binary, notes)
KNOWN_HARNESS_SPECS = [
    ("claude", [".claude"], "claude"),
    ("codex", [".codex"], "codex"),
    ("jcode", [".jcode"], "jcode"),
    ("hermes", [".hermes"], "hermes"),
    ("cursor", [".cursor"], "cursor-agent"),
    ("opencode", [".opencode", ".config/opencode"], "opencode"),
    ("gemini", [".gemini"], "gemini"),
    ("cline", [".cline", ".config/cline"], None),
    ("windsurf", [".windsurf", ".codeium/windsurf"], None),
    ("copilot", [".copilot"], "copilot"),
]

GENERIC_SKILL_DIR_MARKERS = ("skills",)
SKIP_HOME_DIRS = {
    ".Trash", ".cache", ".cargo", ".config", ".docker", ".dropbox",
    ".git", ".gnupg", ".local", ".npm", ".nvm", ".pyenv", ".rustup",
    ".ssh", ".ssl", ".vscode", ".zsh", ".oh-my-zsh", ".keras", ".matplotlib",
}


@dataclass
class Harness:
    name: str
    home_dir: Path
    binary: Optional[str]
    known: bool
    skill_roots: list[Path] = field(default_factory=list)
    plugin_caches: list[Path] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return self.home_dir.is_dir() or bool(self.skill_roots) or bool(self.plugin_caches)


def _which(binary: str) -> Optional[str]:
    import shutil

    return shutil.which(binary)


def _skills_roots_under(harness_home: Path) -> list[Path]:
    roots = []
    direct = harness_home / "skills"
    if direct.is_dir():
        roots.append(direct)
    # nested layouts like .codeium/windsurf/skills or <home>/plugins/*/skills
    for marker in GENERIC_SKILL_DIR_MARKERS:
        for found in harness_home.glob(f"*/{marker}"):
            if found.is_dir() and found not in roots:
                roots.append(found)
    return roots


def _plugin_caches_under(harness_home: Path) -> list[Path]:
    caches = []
    for pattern in ("plugins/cache", "plugins"):
        candidate = harness_home / pattern
        if candidate.is_dir():
            caches.append(candidate)
    return caches


def _detect_known(spec) -> Harness:
    name, dot_dirs, binary = spec
    for dot_dir in dot_dirs:
        harness_home = HOME / dot_dir
        if harness_home.exists():
            resolved_binary = _which(binary) if binary else None
            return Harness(
                name=name,
                home_dir=harness_home,
                binary=resolved_binary,
                known=True,
                skill_roots=_skills_roots_under(harness_home),
                plugin_caches=_plugin_caches_under(harness_home),
            )
    return Harness(name=name, home_dir=HOME / dot_dirs[0], binary=binary, known=False)


def detect_harnesses() -> list[Harness]:
    """All known harnesses plus any unknown ones the generic scanner finds."""
    harnesses: dict[str, Harness] = {}
    seen_homes: set[Path] = set()
    for spec in KNOWN_HARNESS_SPECS:
        harness = _detect_known(spec)
        harnesses[harness.name] = harness
        if harness.present:
            seen_homes.add(harness.home_dir.resolve(strict=False))
            for root in (*harness.skill_roots, *harness.plugin_caches):
                seen_homes.add(root.resolve(strict=False))

    already_named = {h.name for h in harnesses.values()}
    try:
        home_entries = sorted(HOME.iterdir())
    except OSError:
        home_entries = []
    for entry in home_entries:
        # Only hidden directories can be harness homes; plain files and
        # unreadable entries are skipped instead of probed.
        try:
            if not entry.name.startswith(".") or not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name in SKIP_HOME_DIRS or entry.resolve(strict=False) in seen_homes:
            continue
        if entry.name.lstrip(".").split("-")[0] in already_named:
            continue
        skill_roots = _skills_roots_under(entry)
        plugin_caches = _plugin_caches_under(entry)
        if not skill_roots and not plugin_caches:
            continue
        alias = entry.name.lstrip(".")
        harnesses[f"{alias} (auto-detected)"] = Harness(
            name=alias,
            home_dir=entry,
            binary=None,
            known=False,
            skill_roots=[root for root in skill_roots],
            plugin_caches=plugin_caches,
        )
    return list(harnesses.values())


def selected_skill_roots(selection: Optional[set[str]]) -> list[str]:
    """Portable skill-root strings for the chosen harnesses (all if selection is None)."""
    roots: list[str] = []
    for harness in detect_harnesses():
        if not harness.present:
            continue
        if selection is not None and harness.name.lower() not in selection:
            continue
        for root in harness.skill_roots:
            text = str(root)
            if text not in roots:
                roots.append(text)
    return roots


def cmd_init(args: argparse.Namespace) -> int:
    harnesses = detect_harnesses()
    present = [h for h in harnesses if h.present]
    if args.runtimes:
        selection = {name.strip().lower() for name in args.runtimes.split(",") if name.strip()}
        unknown = sorted(
            name for name in selection if name not in {h.name.lower() for h in present}
        )
        if unknown and not args.force:
            print(
                "status: error\nsummary: requested runtime(s) not detected on this machine: "
                + ", ".join(unknown)
            )
            return 1
    else:
        selection = None  # bind everything found

    roots = selected_skill_roots(selection)
    local_path = _repo_root() / "config" / "local.toml"
    surface_line = f"surface_roots = [{', '.join(_toml_str(p) for p in _surface_candidates())}]"
    roots_line = "extra_skill_roots = [" + ", ".join(_toml_str(r) for r in roots) + "]"
    try:
        content = _merge_bindings(
            local_path.read_text(encoding="utf-8") if local_path.is_file() else "",
            {"project": ("surface_roots", surface_line), "extensions": ("extra_skill_roots", roots_line)},
        )
    except ValueError as error:
        print(f"status: error\nsummary: {local_path} was left unchanged: {error}")
        return 1
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(content, encoding="utf-8")

    print("status: success")
    print(f"bound skill roots ({len(roots)}):")
    for root in roots:
        print(f"  - {root}")
    print(f"bindings written: {local_path}")
    print("next: lockkeeper rebuild && lockkeeper doctor")
    _ask_about_telemetry()
    return 0


def _ask_about_telemetry() -> None:
    """Setup is the one moment to ask, once and only in a terminal (telemetry.ask_once)."""
    try:
        import telemetry
    except ImportError:
        return
    telemetry.ask_once()


def _surface_candidates() -> list[str]:
    candidates = []
    for harness in detect_harnesses():
        if harness.present:
            candidates.append(str(harness.home_dir))
    return candidates


LOCAL_HEADER = (
    "# Written by `lockkeeper init` — machine-local bindings; safe to delete.\n"
    "# This file is git-ignored and never leaves this machine. `init` rewrites only\n"
    "# project.surface_roots and extensions.extra_skill_roots; other keys are kept.\n"
)


def _merge_bindings(existing: str, managed: dict[str, tuple[str, str]]) -> str:
    """Replace only the keys `init` owns, keeping every other line of local.toml.

    `init` runs on every ./install.sh. It used to rewrite the whole file, which
    silently discarded anything else an operator had configured there (legacy
    MCP names, resource corpora, decision-provider settings). Raises ValueError
    when the existing file is not valid TOML or the result would not be, so a
    hand-edited file is never clobbered.
    """
    import re
    import tomllib

    if existing.strip():
        try:
            tomllib.loads(existing)
        except tomllib.TOMLDecodeError as error:
            raise ValueError(f"existing file is not valid TOML ({error})") from error
    lines = existing.splitlines() if existing.strip() else LOCAL_HEADER.rstrip("\n").splitlines()
    for table, (key, replacement) in managed.items():
        header = next(
            (index for index, line in enumerate(lines) if line.strip() == f"[{table}]"),
            None,
        )
        if header is None:
            lines.extend(["", f"[{table}]", replacement])
            continue
        end = next(
            (index for index in range(header + 1, len(lines)) if lines[index].lstrip().startswith("[")
             and re.match(r"^\s*\[[^\]]", lines[index]) and "=" not in lines[index].split("#", 1)[0]),
            len(lines),
        )
        key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        start = next((index for index in range(header + 1, end) if key_pattern.match(lines[index])), None)
        if start is None:
            lines.insert(header + 1, replacement)
            continue
        stop = start + 1
        value = lines[start].split("=", 1)[1].split("#", 1)[0].strip()
        if value.startswith("[") and value.count("[") > value.count("]"):
            # A hand-written multi-line array: drop its continuation lines too.
            depth = value.count("[") - value.count("]")
            while stop < end and depth > 0:
                fragment = lines[stop].split("#", 1)[0]
                depth += fragment.count("[") - fragment.count("]")
                stop += 1
        lines[start:stop] = [replacement]
    merged = "\n".join(lines).rstrip("\n") + "\n"
    try:
        tomllib.loads(merged)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"merged bindings would not be valid TOML ({error})") from error
    return merged


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def cmd_doctor(args: argparse.Namespace) -> int:
    harnesses = detect_harnesses()
    print("lockkeeper doctor")
    print(f"python: {sys.version.split()[0]} (requires >= 3.11)")
    print("")
    print("harnesses:")
    any_present = False
    for harness in harnesses:
        if harness.present:
            any_present = True
            cli = harness.binary or "not on PATH"
            skills = sum(1 for root in harness.skill_roots for _ in root.glob("*/SKILL.md"))
            label = harness.name
            print(f"  [x] {label:<12} {str(harness.home_dir):<40} skills={skills:<4} cli={cli}")
        else:
            print(f"  [ ] {harness.name:<12} not detected")
    if not any_present:
        print("  (no harnesses found — cap still works standalone with `lockkeeper audit`)")

    output_dir, config_problem = _registry_output_dir()
    registry_manifest = output_dir / "manifest.json"
    if config_problem:
        print("")
        print(f"router config: {config_problem}")
    if registry_manifest.is_file():
        try:
            data = json.loads(registry_manifest.read_text(encoding="utf-8"))
            counts = data.get("counts", {})
            print("")
            print("registry:")
            print(f"  location:     {output_dir}")
            print(f"  capabilities: {counts.get('capabilities', 0):,}")
            print(f"  rebuilt at:   {data.get('generated_at', 'unknown')}")
            print(f"  fingerprint:  {data.get('fingerprint', '')[:16]}")
            print(f"  freshness:    {_registry_freshness(output_dir)}")
        except (OSError, json.JSONDecodeError):
            pass
    else:
        print("")
        print(f"registry: not built yet at {output_dir} — run `lockkeeper snapshot-runtimes && lockkeeper rebuild`")

    local_config = _repo_root() / "config" / "local.toml"
    print("")
    if local_config.is_file():
        print("binding: config/local.toml present (machine-local)")
    else:
        print("binding: none yet — run `lockkeeper init` to bind detected harnesses")
    try:
        import telemetry
    except ImportError:
        return 0
    if telemetry.enabled():
        print("telemetry: on (anonymous daily counts; `lockkeeper telemetry show` prints them)")
    else:
        print(
            "telemetry: off — to help improve Lockkeeper, `lockkeeper telemetry on` shares anonymous "
            "daily usage counts, never prompts, names or paths (docs/TELEMETRY.md)"
        )
    return 0


def _registry_output_dir() -> tuple[Path, Optional[str]]:
    """The configured registry output, falling back to the default when config is broken.

    doctor used to read ~/.agents/capabilities unconditionally, so a configured
    output_dir always reported "not built yet". It must still run when the
    router configuration itself is what is broken.
    """
    default = HOME / ".agents" / "capabilities"
    try:
        import capability_registry as registry
    except Exception as error:  # noqa: BLE001 - doctor reports, never crashes
        return default, f"router could not load ({type(error).__name__}: {error})"
    if registry.STARTUP_CONFIG_ERROR is not None:
        return default, f"invalid ({registry.STARTUP_CONFIG_ERROR})"
    return registry.ROUTER_CONFIG.output_dir, None


def _registry_freshness(output_dir: Path) -> str:
    try:
        import capability_registry as registry

        registry.assert_registry_fresh(output_dir, deep=False)
    except RuntimeError as error:
        text = registry.redact_sensitive_text(error, 200)
        if registry.auto_refreshable_staleness(error):
            return f"stale, repaired automatically by the next route/search ({text})"
        return f"needs attention: {text}"
    except Exception as error:  # noqa: BLE001 - doctor reports, never crashes
        return f"unknown ({type(error).__name__})"
    return "fresh"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cap-setup", description="Bind cap to this machine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Detect harnesses and write local bindings")
    init_parser.add_argument(
        "--runtimes",
        help="Comma-separated subset to bind (default: everything detected)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Write bindings even when a requested runtime was not detected",
    )

    subparsers.add_parser("doctor", help="Show detected harnesses and routing health")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
