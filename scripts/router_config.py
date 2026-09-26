"""Startup configuration for the capability router's structural paths.

The router deliberately keeps routing policy and domain data in code.  This module
only resolves the filesystem and project-boundary values that must move with a
standalone installation.
"""
from __future__ import annotations

import os
import re
import sys

if sys.version_info < (3, 11):
    raise RuntimeError("capability router configuration requires Python 3.11 or newer")

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping


class RouterConfigError(RuntimeError):
    """A configured router source could not be read or validated."""


TOP_LEVEL_SCALAR_KEYS = {"output_dir", "claude_json_path", "default_project", "bundle_size"}

# How many capabilities a routed bundle holds unless a caller asks for another size.
DEFAULT_BUNDLE_SIZE = 10
BUNDLE_SIZE_RANGE = (3, 20)
BUNDLE_SIZE_ENV = "LOCKKEEPER_BUNDLE_SIZE"
PROJECT_KEYS = {
    "name",
    "snapshot_dir",
    "catalog_path",
    "mcp_config_paths",
    "surface_roots",
    "cwd",
    "first_party_roots",
    "hermes_project_source",
    "skill_catalog_csv",
}
HERMES_KEYS = {"profiles", "shared_surface_root"}


@dataclass(frozen=True)
class RouterConfig:
    output_dir: Path
    project_name: str
    snapshot_dir: Path
    catalog_path: Path
    mcp_config_paths: tuple[Path, ...]
    surface_roots: tuple[Path, ...]
    cwd: Path
    first_party_roots: tuple[Path, ...]
    hermes_project_source: Path
    skill_catalog_csv: Path
    claude_json_path: Path
    hermes_profiles: tuple[str, ...]
    hermes_shared_surface_root: Path
    active_config_paths: tuple[Path, ...]
    extensions: tuple[tuple[str, Any], ...] = ()
    bundle_size: int = DEFAULT_BUNDLE_SIZE

    def get_extension(self, key: str, default: Any = None) -> Any:
        return dict(self.extensions).get(key, default)

    @property
    def source_paths(self) -> tuple[Path, ...]:
        """Compatibility-friendly name for the files active in this resolution."""
        return self.active_config_paths


def _router_root(script_path: Path | None) -> Path:
    source = (script_path or Path(__file__)).expanduser().resolve(strict=False)
    return source.parents[1]


def _builtin_config(script_path: Path | None) -> RouterConfig:
    root = _router_root(script_path)
    # Runtime-mutable snapshots live in a machine-local state dir so a clone
    # never accumulates personal inventory as modified tracked files. The
    # copies under data/snapshots are read-only seeds bootstrapped on first use.
    snapshot_dir = Path.home() / ".local" / "state" / "cap" / "snapshots"
    return RouterConfig(
        output_dir=Path.home() / ".agents" / "capabilities",
        project_name="",
        snapshot_dir=snapshot_dir,
        catalog_path=snapshot_dir.parent / "CAPABILITIES-DETAIL.md",
        mcp_config_paths=(root / ".mcp.json",),
        surface_roots=(root,),
        cwd=root,
        first_party_roots=(root,),
        hermes_project_source=snapshot_dir,
        skill_catalog_csv=snapshot_dir / "SKILL-CATALOG.csv",
        claude_json_path=Path.home() / ".claude.json",
        hermes_profiles=(),
        hermes_shared_surface_root=Path.home() / ".hermes" / "cap-shared-skills",
        active_config_paths=(),
        extensions=(),
    )


def _config_error(path: Path, message: str) -> RouterConfigError:
    return RouterConfigError(f"Router configuration {path}: {message}")


def _read_toml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise _config_error(path, "file does not exist")
        return {}
    if not path.is_file():
        raise _config_error(path, "expected a regular TOML file")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except RecursionError as error:
        raise _config_error(path, "TOML nesting too deep") from error
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise _config_error(path, f"could not parse TOML: {error}") from error
    if not isinstance(data, dict):
        raise _config_error(path, "top level must be a TOML table")
    return data


def _text(value: Any, path: Path, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _config_error(path, f"{field} must be a{' non-empty' if not allow_empty else ''} string")
    return value.strip()


def _path(value: Any, path: Path, field: str) -> Path:
    text = _text(value, path, field)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = path.parent / candidate
    return candidate.resolve(strict=False)


def _bundle_size(value: Any, source: str) -> int:
    low, high = BUNDLE_SIZE_RANGE
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RouterConfigError(f"{source}: bundle_size must be an integer from {low} to {high}")
    return value


def resolve_bundle_size(config: RouterConfig, environ: Mapping[str, str] = os.environ) -> int:
    """The bundle size to use when a caller doesn't pass one: the environment, then the config."""
    override = environ.get(BUNDLE_SIZE_ENV, "").strip()
    if override:
        return _bundle_size(override, BUNDLE_SIZE_ENV)
    return config.bundle_size


def _path_list(value: Any, path: Path, field: str) -> tuple[Path, ...]:
    if not isinstance(value, list):
        raise _config_error(path, f"{field} must be an array of paths")
    return tuple(_path(item, path, f"{field}[{index}]") for index, item in enumerate(value))


def _text_list(value: Any, path: Path, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _config_error(path, f"{field} must be an array of strings")
    values = tuple(_text(item, path, f"{field}[{index}]") for index, item in enumerate(value))
    if not values and not allow_empty:
        raise _config_error(path, f"{field} must not be empty")
    return values


def _table(data: Mapping[str, Any], path: Path, field: str) -> Mapping[str, Any]:
    value = data.get(field, {})
    if not isinstance(value, dict):
        raise _config_error(path, f"{field} must be a TOML table")
    return value


def _validate_keys(data: Mapping[str, Any], path: Path) -> None:
    """Allow only the explicit future-extension namespace beyond structural fields."""
    for key, value in data.items():
        if key in {"project", "hermes"}:
            continue
        if key == "extensions":
            if not isinstance(value, dict):
                raise _config_error(path, "extensions must be a TOML table")
            continue
        if key not in TOP_LEVEL_SCALAR_KEYS:
            raise _config_error(path, f"unknown top-level structural key {key!r}")

    project = _table(data, path, "project")
    unknown_project_keys = sorted(set(project) - PROJECT_KEYS)
    if unknown_project_keys:
        raise _config_error(path, f"unknown [project] key(s): {', '.join(unknown_project_keys)}")

    hermes = _table(data, path, "hermes")
    unknown_hermes_keys = sorted(set(hermes) - HERMES_KEYS)
    if unknown_hermes_keys:
        raise _config_error(path, f"unknown [hermes] key(s): {', '.join(unknown_hermes_keys)}")


def _apply_overlay(config: RouterConfig, data: Mapping[str, Any], path: Path) -> RouterConfig:
    _validate_keys(data, path)
    values: dict[str, Any] = {}
    if "output_dir" in data:
        values["output_dir"] = _path(data["output_dir"], path, "output_dir")
    if "claude_json_path" in data:
        values["claude_json_path"] = _path(data["claude_json_path"], path, "claude_json_path")
    if "bundle_size" in data:
        if isinstance(data["bundle_size"], str):
            raise _config_error(path, "bundle_size must be an integer, not a string")
        values["bundle_size"] = _bundle_size(data["bundle_size"], f"Router configuration {path}")

    project = _table(data, path, "project")
    project_fields = {
        "snapshot_dir",
        "catalog_path",
        "cwd",
        "hermes_project_source",
        "skill_catalog_csv",
    }
    for field in project_fields:
        if field in project:
            values[field] = _path(project[field], path, f"project.{field}")
    if "name" in project:
        values["project_name"] = _text(project["name"], path, "project.name", allow_empty=True)
    for field in ("mcp_config_paths", "surface_roots", "first_party_roots"):
        if field in project:
            values[field] = _path_list(project[field], path, f"project.{field}")

    hermes = _table(data, path, "hermes")
    if "profiles" in hermes:
        values["hermes_profiles"] = _text_list(
            hermes["profiles"], path, "hermes.profiles", allow_empty=True
        )
    if "shared_surface_root" in hermes:
        values["hermes_shared_surface_root"] = _path(
            hermes["shared_surface_root"], path, "hermes.shared_surface_root"
        )

    source_paths = tuple(dict.fromkeys((*config.active_config_paths, path)))
    merged_extensions = {**dict(config.extensions), **data.get("extensions", {})}
    if merged_extensions:
        values["extensions"] = tuple(sorted(merged_extensions.items()))
    return replace(config, active_config_paths=source_paths, **values)


def _normalise_project_name(project_name: str) -> str:
    name = project_name.strip().lower()
    if not name:
        return ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise RouterConfigError(
            f"Invalid project selector {project_name!r}; use lowercase letters, digits, hyphens, or underscores"
        )
    return name


def _default_project(data: Mapping[str, Any], path: Path) -> str:
    if "default_project" not in data:
        return ""
    return _normalise_project_name(_text(data["default_project"], path, "default_project", allow_empty=True))


def _validate_selected_project(
    data: Mapping[str, Any], path: Path, project_name: str, *, require_name: bool
) -> None:
    project = _table(data, path, "project")
    if "name" not in project:
        if require_name:
            raise _config_error(path, "selected project configuration must define project.name")
        return
    configured_name = _normalise_project_name(_text(project["name"], path, "project.name"))
    if configured_name != project_name:
        raise _config_error(
            path,
            f"project.name {configured_name!r} does not match selected project {project_name!r}",
        )


def load_router_config(
    *,
    project_name: str = "",
    script_path: Path | None = None,
    include_repository: bool = True,
    include_explicit: bool = True,
) -> RouterConfig:
    """Resolve built-ins, repository defaults, an optional project, then an env file."""
    selected_project = _normalise_project_name(project_name)
    root = _router_root(script_path)
    config = _builtin_config(script_path)
    default_project = ""
    if include_repository:
        default_path = (root / "config" / "default.toml").resolve(strict=False)
        default_data = _read_toml(default_path, required=False)
        if default_path.is_file():
            config = _apply_overlay(config, default_data, default_path)
            default_project = _default_project(default_data, default_path)

    # Machine-local bindings (written by `lockkeeper init`, never committed): sits
    # above defaults so a cloned repo binds to whatever this machine has.
    local_path = (root / "config" / "local.toml").resolve(strict=False)
    if include_repository and local_path.is_file():
        config = _apply_overlay(config, _read_toml(local_path, required=True), local_path)

    resolved_project = selected_project or default_project
    if resolved_project:
        if not include_repository:
            raise RouterConfigError("Project selection requires repository configuration sources")
        project_path = (root / "config" / f"{resolved_project}.toml").resolve(strict=False)
        project_data = _read_toml(project_path, required=True)
        _validate_selected_project(project_data, project_path, resolved_project, require_name=True)
        config = _apply_overlay(config, project_data, project_path)

    explicit_text = os.environ.get("CAPABILITY_ROUTER_CONFIG", "").strip()
    if include_explicit and explicit_text:
        explicit_path = Path(explicit_text).expanduser()
        if not explicit_path.is_absolute():
            explicit_path = Path.cwd() / explicit_path
        explicit_path = explicit_path.resolve(strict=False)
        explicit_data = _read_toml(explicit_path, required=True)
        if resolved_project:
            _validate_selected_project(explicit_data, explicit_path, resolved_project, require_name=False)
        config = _apply_overlay(config, explicit_data, explicit_path)
    if resolved_project and config.project_name != resolved_project:
        raise RouterConfigError(
            f"Resolved project configuration is {config.project_name!r}; expected {resolved_project!r}"
        )
    return config


def split_project_argument(argv: list[str]) -> tuple[str, list[str]]:
    """Extract --project anywhere so config selection precedes argparse subcommands."""
    project_name = ""
    project_seen = False
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--project":
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                raise RouterConfigError("--project requires a project name")
            value = argv[index + 1]
            index += 2
        elif token.startswith("--project="):
            value = token.partition("=")[2]
            if not value:
                raise RouterConfigError("--project requires a project name")
            index += 1
        else:
            remaining.append(token)
            index += 1
            continue
        normalized = _normalise_project_name(value)
        if project_seen:
            raise RouterConfigError("--project may be provided only once")
        project_name = normalized
        project_seen = True
    return project_name, remaining
