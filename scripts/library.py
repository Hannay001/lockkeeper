"""`lockkeeper library`: keep large skill collections out of every session's context.

Agents put the name and description of every skill in the folders they load on
their own (Claude Code: ~/.claude/skills) into every session. With hundreds of
skills that catalog costs thousands of tokens before any work starts, and routing
alone can't remove it. Library mode moves skills from such a folder into
~/.agents/library/<agent>/, which no agent loads by itself but Lockkeeper indexes,
so routing (the Claude Code hook, the MCP server, `lockkeeper route`) hands the
agent only the few a task needs, with the SKILL.md path to read.

Every move is recorded in ~/.agents/library/.lockkeeper-library.json and `library
restore` puts skills back. `move` and `restore` only print a plan until --apply.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

MANIFEST_NAME = ".lockkeeper-library.json"
# Skills that must stay loadable so an agent can still reach the library.
PROTECTED = frozenset({"capability-router", "lockkeeper"})
# Per skill, the catalog line an agent carries: roughly name, description and a
# little framing. Estimated at 4 characters per token, like the rest of Lockkeeper.
CATALOG_OVERHEAD_CHARS = 20
# Re-index only once the moves are this old. A rebuild treats folders modified
# within ~20 ms of its start as possibly half-walked and marks the index stale
# (DISCOVERY_CLOCK_SLACK_NS), and the Claude Code hook never rebuilds, so a
# rebuild started right after the last move would leave prompts unrouted.
SETTLE_SECONDS = 0.1


def agent_skill_dirs() -> dict[str, Path]:
    """Folders each agent loads skills from on its own (user level)."""
    home = Path.home()
    return {
        "claude": home / ".claude" / "skills",
        "codex": home / ".codex" / "skills",
        "jcode": home / ".jcode" / "skills",
        "hermes": home / ".hermes" / "skills",
    }


def library_root() -> Path:
    return Path.home() / ".agents" / "library"


def manifest_path() -> Path:
    return library_root() / MANIFEST_NAME


# ---------------------------------------------------------------- reading skills


def _frontmatter(skill_file: Path) -> dict[str, str]:
    try:
        text = skill_file.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return {}
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
    fields: dict[str, str] = {}
    for line in (match.group(1) if match else "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in {"name", "description"}:
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


def loaded_skills(root: Path) -> list[dict[str, Any]]:
    """Skills an agent loads from `root`: each direct child folder with a SKILL.md."""
    skills = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return []
    for child in children:
        if child.name.startswith(".") or not (child / "SKILL.md").is_file():
            continue
        meta = _frontmatter(child / "SKILL.md")
        name = meta.get("name") or child.name
        description = meta.get("description", "")
        skills.append(
            {
                "folder": child.name,
                "name": name,
                "path": child,
                "tokens": (len(name) + len(description) + CATALOG_OVERHEAD_CHARS) // 4,
            }
        )
    return skills


def _protected(skill: dict[str, Any], keep: set[str]) -> bool:
    names = {skill["folder"].lower(), skill["name"].lower()}
    return bool(names & (PROTECTED | keep)) or any(name.startswith("lockkeeper") for name in names)


# ---------------------------------------------------------------- manifest


def load_manifest() -> dict[str, Any]:
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "moves": []}
    except (OSError, ValueError) as error:
        raise RuntimeError(f"{manifest_path()} is unreadable ({error}); fix or remove it before moving skills")
    if not isinstance(data, dict) or not isinstance(data.get("moves"), list):
        raise RuntimeError(f"{manifest_path()} has an unexpected shape; fix or remove it before moving skills")
    return data


def save_manifest(data: dict[str, Any]) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- routing check


def routing_ready(agent: str) -> tuple[bool, str]:
    """Whether the agent will still be offered the skills once they are moved."""
    if agent != "claude":
        return False, (
            f"Lockkeeper can't check how {agent} routes. Make sure it calls the Lockkeeper MCP server "
            "or has the capability-router skill, then pass --force"
        )
    import route_hook

    for scope in ("user", "project"):
        settings = route_hook.claude_settings_path(scope)
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        groups = (data.get("hooks") or {}).get("UserPromptSubmit") if isinstance(data, dict) else None
        if isinstance(groups, list) and any(
            route_hook._is_lockkeeper_hook(group, " route-hook") for group in groups
        ):
            return True, f"routing hook found in {settings}"
        if route_hook.plugin_enabled(settings):
            return True, f"Lockkeeper plugin enabled in {settings}"
    return False, (
        "Claude Code doesn't route through Lockkeeper yet, so moved skills would be out of reach. "
        "Install the Lockkeeper plugin or run `lockkeeper hooks install claude` first (or pass --force)"
    )


# ---------------------------------------------------------------- commands


def status(agent_filter: Optional[str]) -> int:
    moves = load_manifest()["moves"]
    print("skills each agent loads on its own (their names and descriptions go into every session):")
    for agent, root in agent_skill_dirs().items():
        if agent_filter and agent != agent_filter:
            continue
        skills = loaded_skills(root)
        in_library = sum(1 for move in moves if move.get("agent") == agent)
        tokens = sum(skill["tokens"] for skill in skills)
        if not skills and not in_library:
            continue
        print(f"  {agent:<7} {len(skills):>5} loaded (about {tokens:,} tokens per session)  "
              f"{in_library:>5} in the library   {root}")
    print(f"library: {library_root()}")
    ready, reason = routing_ready(agent_filter or "claude")
    print(f"routing for {agent_filter or 'claude'}: {'ready' if ready else 'not set up'} ({reason})")
    return 0


def move(agent: str, keep: set[str], apply: bool, force: bool, rebuild_index: Callable[[], Any]) -> int:
    root = agent_skill_dirs()[agent]
    skills = loaded_skills(root)
    chosen = [skill for skill in skills if not _protected(skill, keep)]
    kept = [skill for skill in skills if _protected(skill, keep)]
    saved = sum(skill["tokens"] for skill in chosen)
    target_root = library_root() / agent
    if apply and chosen and not force:
        ready, reason = routing_ready(agent)
        if not ready:
            print(f"status: error\nsummary: {reason}")
            return 1
    verb = "moving" if apply else "would move"
    print(f"{agent}: {len(skills)} skills loaded from {root}")
    print(f"{verb} {len(chosen)} to {target_root} (about {saved:,} fewer tokens in every session)")
    if kept:
        print("keeping: " + ", ".join(skill["folder"] for skill in kept))
    if not chosen:
        print("nothing to move")
        return 0
    if not apply:
        print(f"this was a plan; run `lockkeeper library move --agent {agent} --apply` to do it")
        return 0
    manifest = load_manifest()
    moved = skipped = 0
    target_root.mkdir(parents=True, exist_ok=True)
    for skill in chosen:
        destination = target_root / skill["folder"]
        if destination.exists() or destination.is_symlink():
            print(f"  skipped {skill['folder']}: {destination} already exists")
            skipped += 1
            continue
        shutil.move(str(skill["path"]), str(destination))
        manifest["moves"].append(
            {
                "agent": agent,
                "name": skill["folder"],
                "from": str(skill["path"]),
                "to": str(destination),
                "moved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        save_manifest(manifest)  # after every move, so an interruption loses nothing
        moved += 1
    time.sleep(SETTLE_SECONDS)
    rebuild_index()
    print(f"moved {moved}" + (f", skipped {skipped}" if skipped else "") + "; the index is rebuilt.")
    print(f"undo: lockkeeper library restore --agent {agent} --apply")
    return 0


def restore(agent: str, names: set[str], apply: bool, rebuild_index: Callable[[], Any]) -> int:
    manifest = load_manifest()
    wanted = [
        entry for entry in manifest["moves"]
        if entry.get("agent") == agent and (not names or entry.get("name") in names)
    ]
    verb = "restoring" if apply else "would restore"
    print(f"{verb} {len(wanted)} {agent} skill(s) from {library_root() / agent}")
    if not wanted:
        return 0
    if not apply:
        for entry in wanted[:20]:
            print(f"  {entry.get('name')} -> {entry.get('from')}")
        if len(wanted) > 20:
            print(f"  ... and {len(wanted) - 20} more")
        print(f"this was a plan; run `lockkeeper library restore --agent {agent} --apply` to do it")
        return 0
    restored = conflicts = 0
    for entry in wanted:
        source, destination = Path(str(entry.get("to"))), Path(str(entry.get("from")))
        if destination.exists() or destination.is_symlink():
            print(f"  kept in the library: {entry.get('name')} ({destination} exists again)")
            conflicts += 1
            continue
        if not (source.exists() or source.is_symlink()):
            print(f"  forgot {entry.get('name')}: it is no longer in the library")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            restored += 1
        manifest["moves"].remove(entry)
        save_manifest(manifest)
    time.sleep(SETTLE_SECONDS)
    rebuild_index()
    print(f"restored {restored}" + (f", {conflicts} left in the library" if conflicts else "") + "; the index is rebuilt.")
    return 0


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "library",
        help="Move rarely used skills out of the folders agents load on their own; route them on demand",
        description=(
            "Agents load every skill's name and description from their own skills folder into every "
            "session. `move` puts them in ~/.agents/library, which Lockkeeper indexes and routes but no "
            "agent loads; `restore` puts them back. Both only show a plan until --apply."
        ),
    )
    parser.add_argument("action", choices=["status", "move", "restore"])
    parser.add_argument("names", nargs="*", help="restore: only these skills (default: all)")
    parser.add_argument("--agent", choices=sorted(agent_skill_dirs()), default=None,
                        help="the agent whose skills folder to use (default: claude)")
    parser.add_argument("--keep", action="append", default=[], metavar="NAME",
                        help="move: leave this skill where it is (repeatable)")
    parser.add_argument("--apply", action="store_true", help="make the change; without it, only show the plan")
    parser.add_argument("--force", action="store_true",
                        help="move even when Lockkeeper can't confirm the agent routes through it")


def run(args: argparse.Namespace, rebuild_index: Callable[[], Any]) -> int:
    """rebuild_index re-indexes after a move or restore; the caller passes its own,
    configured rebuild so a launcher-run CLI never imports a second registry."""
    if args.action == "status":
        return status(args.agent)
    agent = args.agent or "claude"
    if args.action == "move":
        if args.names:
            raise RuntimeError("move takes --keep NAME for skills to leave alone, not skill names")
        return move(agent, {name.lower() for name in args.keep}, args.apply, args.force, rebuild_index)
    return restore(agent, set(args.names), args.apply, rebuild_index)
