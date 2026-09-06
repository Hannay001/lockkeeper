"""Phase A integration and boundary tests for the capability registry.

The CLI fixture copies the two router modules into a temporary router root.  All
registry outputs, links, configuration files, and home-like paths are temporary.
"""
from __future__ import annotations

import contextlib
import functools
import importlib
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
SYSTEM_PYTHON = Path("/usr/bin/python3")
SUPPORTED_PYTHON_ENV = "CAPABILITY_ROUTER_PYTHON"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capability_registry as registry  # noqa: E402
import cap_audit  # noqa: E402
import router_config  # noqa: E402


class StandaloneAuditCommandTest(unittest.TestCase):
    def test_receipt_verification_does_not_scan_the_current_directory(self) -> None:
        with mock.patch.object(cap_audit, "run_audit_flow") as run_audit_flow:
            with mock.patch.object(
                cap_audit,
                "verify_receipt_file",
                return_value=(True, "receipt valid"),
            ):
                code = registry._run_standalone(
                    "audit",
                    ["audit", "--verify-receipt", "receipt.json", "--receipt-key", "key.hex"],
                )

        self.assertEqual(code, 0)
        run_audit_flow.assert_not_called()


def python_major_minor(python: Path) -> tuple[int, int] | None:
    version = subprocess.run(
        [str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
        capture_output=True,
        check=False,
    )
    if version.returncode:
        return None
    try:
        major, minor = version.stdout.strip().split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def resolve_supported_python() -> Path | None:
    """Prefer an override, then a current Python 3.11-or-newer executable."""
    candidate_names = []
    override = os.environ.get(SUPPORTED_PYTHON_ENV, "").strip()
    if override:
        candidate_names.append(override)
    candidate_names.extend(("python3.14", "python3.13", "python3.12", "python3.11", sys.executable))

    seen: set[Path] = set()
    for candidate_name in candidate_names:
        candidate = Path(candidate_name).expanduser()
        if not candidate.is_absolute() and "/" not in candidate_name:
            resolved_name = shutil.which(candidate_name)
            if resolved_name is None:
                continue
            candidate = Path(resolved_name)
        candidate = candidate.resolve(strict=False)
        if candidate in seen:
            continue
        seen.add(candidate)
        version = python_major_minor(candidate)
        if version is not None and version >= (3, 11):
            return candidate
    return None


@functools.lru_cache(maxsize=None)
def _symlink_capable() -> bool:
    """Probe whether creating a symlink works in a scratch directory."""
    probe = Path(tempfile.mkdtemp(prefix="capability-router-symlink-probe-"))
    try:
        try:
            (probe / "link").symlink_to(probe / "target")
        except OSError:
            return False
        return True
    finally:
        shutil.rmtree(probe, ignore_errors=True)


class IsolatedRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_config = registry.ROUTER_CONFIG

    @classmethod
    def tearDownClass(cls) -> None:
        registry.configure_router(cls._original_config)

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="phase-a-registry-")
        self.addCleanup(self._temporary_directory.cleanup)
        self.temp = Path(self._temporary_directory.name).resolve(strict=False)
        self.router_root = self.temp / "router"
        self.script_path = self.router_root / "scripts" / "capability_registry.py"
        self.script_path.parent.mkdir(parents=True)
        self.script_path.touch()
        self.home = self.temp / "home"
        self.home.mkdir()
        self._environment = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "CAPABILITY_ROUTER_CONFIG": ""},
            clear=False,
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)

    def configure(self, **changes: object) -> router_config.RouterConfig:
        builtin = router_config.load_router_config(
            script_path=self.script_path,
            include_repository=False,
            include_explicit=False,
        )
        config = replace(builtin, **changes)
        registry.configure_router(config)
        return config

    def test_load_registry_accepts_agent_command_entrypoint_sources(self) -> None:
        """Regression: the trust check used a hardcoded "skill" type, so every
        .md agent/command/entrypoint record made load_registry raise and bricked
        all read verbs on mixed registries."""
        self.configure(output_dir=self.temp / "mixed-output")
        trusted_root = self.temp / "trusted-root"
        sources = {}
        for name in ("AGENTS", "deploy-command", "review-entrypoint"):
            directory = trusted_root / name
            directory.mkdir(parents=True)
            source = directory / f"{name}.md"
            source.write_text(f"# {name}\n", encoding="utf-8")
            sources[name] = source

        rows = []
        for index, capability_type in enumerate(("agent", "command", "entrypoint")):
            name = list(sources)[index]
            rows.append(
                {
                    "id": f"{capability_type}-{name}",
                    "name": name,
                    "type": capability_type,
                    "description": "regression fixture",
                    "category": "research-knowledge",
                    "status": "active",
                    "runtimes": ["claude"],
                    "source_path": str(sources[name]),
                    "registration_count": 1,
                    "owner": "",
                }
            )
        output = self.temp / "mixed-registry"
        output.mkdir()
        (output / "registry.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )

        with mock.patch.object(registry, "SKILL_ROOTS", [("shared", trusted_root, "skills-root")]):
            records = registry.load_registry(output)
        self.assertEqual([record["type"] for record in records], ["agent", "command", "entrypoint"])

    def test_moved_source_registry_row_is_recoverable_staleness_not_corruption(self) -> None:
        """Regression: a recorded skill whose source later moved outside the
        trusted roots (e.g. a symlinked skill whose target left the tree) made
        load_registry raise, bricking bundle/route, search, check and export-csv.
        It is a stale-registry state -- rebuild rediscovers from disk and drops
        the row -- so the error must be actionable and classified as
        auto-refreshable so query verbs self-heal instead of dead-ending."""
        self.configure(output_dir=self.temp / "moved-output")
        trusted_root = self.temp / "trusted-root"
        trusted_root.mkdir(parents=True)
        untrusted_source = self.temp / "elsewhere" / "capability-router" / "SKILL.md"
        untrusted_source.parent.mkdir(parents=True)
        untrusted_source.write_text("# capability-router\n", encoding="utf-8")

        row = {
            "id": "skill-moved",
            "name": "capability-router",
            "type": "skill",
            "description": "regression fixture",
            "category": "research-knowledge",
            "status": "discoverable",
            "runtimes": ["claude"],
            "source_path": str(untrusted_source),
            "registration_count": 1,
            "owner": "",
        }
        output = self.temp / "moved-registry"
        output.mkdir()
        (output / "registry.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

        with mock.patch.object(registry, "SKILL_ROOTS", [("shared", trusted_root, "skills-root")]):
            with self.assertRaises(RuntimeError) as caught:
                registry.load_registry(output)

        message = str(caught.exception)
        # Actionable: the maintenance verbs surface this verbatim, so it must
        # tell the user the recovery step rather than dead-ending.
        self.assertIn("run rebuild", message)
        # Recoverable: query verbs (bundle/route, search) must self-heal.
        self.assertTrue(
            registry.auto_refreshable_staleness(RuntimeError(message)),
            "moved-source staleness must be auto-refreshable so routing recovers",
        )

    def test_output_directory_reaches_legacy_archive_alias_and_semantic_helpers(self) -> None:
        custom_output = self.temp / "custom-output"
        self.configure(output_dir=custom_output)

        with mock.patch.object(registry, "SKILL_ROOTS", []), mock.patch.object(
            registry, "load_json", return_value={"links": []}
        ) as load_json:
            self.assertEqual(list(registry.iter_all_skill_entries(custom_output)), [])
        self.assertEqual(load_json.call_args.args[0], custom_output / "legacy" / "auto-discovery-symlinks.json")

        record = {"id": "record-a", "type": "skill", "name": "Record A", "description": "example"}
        with mock.patch.object(registry, "record_is_eligible", return_value=True), mock.patch.object(
            registry, "record_is_rankable", return_value=True
        ), mock.patch.object(registry, "damped_query_terms", return_value=[]), mock.patch.object(
            registry, "load_aliases", return_value={"record-a": "alias"}
        ) as load_aliases, mock.patch.object(
            registry, "registry_manifest_fingerprint", return_value="fingerprint"
        ) as manifest_fingerprint, mock.patch.object(
            registry, "semantic_hits", return_value={}
        ) as semantic_hits, mock.patch.object(registry, "search_score", return_value=1.0):
            ranked = registry.ranked_records([record], "example", "codex", custom_output)

        self.assertEqual(ranked, [(1.0, record)])
        self.assertEqual(load_aliases.call_args.args, (str(custom_output),))
        self.assertEqual(manifest_fingerprint.call_args.args, (str(custom_output),))
        self.assertEqual(semantic_hits.call_args.args, (custom_output, "example", "fingerprint"))

    def test_query_freshness_self_heals_only_known_staleness_and_verifies_once(self) -> None:
        output = self.temp / "canonical-output"
        self.configure(output_dir=output)
        stale = RuntimeError(
            "Runtime configuration changed after the registry was built; run snapshot-runtimes, then rebuild"
        )

        with (
            mock.patch.object(
                registry, "assert_registry_fresh", side_effect=[stale, stale, None, None]
            ) as fresh,
            mock.patch.object(registry, "refresh_runtime_snapshots") as snapshots,
            mock.patch.object(registry, "rebuild") as rebuild,
            mock.patch.object(registry, "reindex_semantic") as reindex,
            mock.patch.object(registry, "link_surfaces") as link,
            mock.patch.object(registry, "run_check") as check,
        ):
            registry.ensure_query_registry_fresh(output)

        snapshots.assert_called_once_with()
        rebuild.assert_called_once_with(output, quiet=True)
        # Bounded recovery: no semantic reindex (fails open to lexical), no
        # surface linking or strict link checks on the query path.
        reindex.assert_not_called()
        link.assert_not_called()
        check.assert_not_called()
        self.assertEqual(fresh.call_count, 3)

    def test_query_freshness_does_not_recover_corruption_or_noncanonical_output(self) -> None:
        canonical_output = self.temp / "canonical-output"
        self.configure(output_dir=canonical_output)
        corrupt = RuntimeError("Invalid registry JSONL at line 1")
        with mock.patch.object(registry, "assert_registry_fresh", side_effect=corrupt), mock.patch.object(
            registry, "refresh_runtime_snapshots"
        ) as snapshots:
            with self.assertRaisesRegex(RuntimeError, "Invalid registry JSONL"):
                registry.ensure_query_registry_fresh(canonical_output)
        snapshots.assert_not_called()

        stale = RuntimeError("Registry is older than runtime snapshots: codex-mcps.json; run rebuild")
        with mock.patch.object(registry, "assert_registry_fresh", side_effect=stale), mock.patch.object(
            registry, "refresh_runtime_snapshots"
        ) as snapshots:
            with self.assertRaisesRegex(RuntimeError, "only supports the canonical output"):
                registry.ensure_query_registry_fresh(self.temp / "noncanonical-output")
        snapshots.assert_not_called()

    def test_configure_router_refreshes_dynamic_hermes_and_trusted_roots(self) -> None:
        first_shared_root = self.temp / "first-shared"
        second_shared_root = self.temp / "second-shared"
        first_source = self.temp / "first-source"
        second_source = self.temp / "second-source"
        self.configure(
            hermes_shared_surface_root=first_shared_root,
            hermes_project_source=first_source,
        )
        self.assertEqual(registry.HERMES_SHARED_SURFACE_ROOT, first_shared_root)
        self.assertIn(("hermes", first_shared_root, "profile-skills-root"), registry.SKILL_ROOTS)
        self.assertIn(first_shared_root, registry.trusted_capability_roots())
        self.assertIn(first_source / "capability-router", registry.trusted_capability_roots())

        self.configure(
            hermes_shared_surface_root=second_shared_root,
            hermes_project_source=second_source,
        )
        trusted_roots = registry.trusted_capability_roots()
        self.assertEqual(registry.HERMES_SHARED_SURFACE_ROOT, second_shared_root)
        self.assertIn(("hermes", second_shared_root, "profile-skills-root"), registry.SKILL_ROOTS)
        self.assertNotIn(("hermes", first_shared_root, "profile-skills-root"), registry.SKILL_ROOTS)
        self.assertIn(second_shared_root, trusted_roots)
        self.assertNotIn(first_shared_root, trusted_roots)
        self.assertIn(second_source / "capability-router", trusted_roots)
        self.assertNotIn(first_source / "capability-router", trusted_roots)

    def test_helper_modules_import_without_legacy_globals_and_use_configured_paths(self) -> None:
        output = self.temp / "helper-output"
        output.mkdir()
        skill_catalog_csv = self.temp / "exports" / "skills.csv"
        self.configure(output_dir=output, skill_catalog_csv=skill_catalog_csv)
        self.assertFalse(hasattr(registry, "PROJECT_ROOT"))
        self.assertFalse(hasattr(registry, "DEFAULT_OUTPUT"))

        helpers = {
            name: importlib.import_module(name)
            for name in (
                "build_aliases",
                "build_synonym_batches",
                "build_audit_corpus",
                "build_skill_catalog",
            )
        }
        for name, helper in helpers.items():
            with self.subTest(helper=name):
                self.assertIs(helper.registry, registry)
                self.assertFalse(hasattr(helper, "PROJECT_ROOT"))
                self.assertFalse(hasattr(helper, "DEFAULT_OUTPUT"))

        with mock.patch.object(registry, "load_registry", return_value=[]), mock.patch.object(
            registry, "load_json", return_value={}
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(helpers["build_aliases"].main([]), 0)
        self.assertTrue((output / "aliases.json").is_file())

        with mock.patch.object(registry, "load_registry", return_value=[]), contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertEqual(helpers["build_synonym_batches"].main([]), 0)
            self.assertEqual(helpers["build_audit_corpus"].main([]), 0)
        self.assertTrue((output / "audit" / "batches").is_dir())
        self.assertTrue((output / "audit" / "corpus").is_dir())

        with mock.patch.object(registry, "rebuild") as rebuild, mock.patch.object(
            registry, "export_skill_csv"
        ) as export_skill_csv:
            self.assertEqual(helpers["build_skill_catalog"].main([]), 0)
        rebuild.assert_called_once_with(output, quiet=True)
        export_skill_csv.assert_called_once_with(output, skill_catalog_csv)

    def test_link_and_prune_reject_noncanonical_outputs_before_writing(self) -> None:
        canonical_output = self.temp / "canonical-output"
        noncanonical_output = self.temp / "other-output"
        self.configure(output_dir=canonical_output)

        with self.assertRaisesRegex(RuntimeError, "canonical output"):
            registry.link_surfaces(noncanonical_output)
        with self.assertRaisesRegex(RuntimeError, "canonical output"):
            registry.prune_auto_discovery(noncanonical_output, apply=False)
        self.assertFalse(canonical_output.exists())
        self.assertFalse(noncanonical_output.exists())

    @unittest.skipUnless(_symlink_capable(), "requires symlink privilege")
    def test_empty_surface_roots_keep_snapshot_system_link_without_project_index_or_categories(self) -> None:
        output = self.temp / "output"
        output.mkdir()
        (output / "Capabilities.md").write_text("index\n", encoding="utf-8")
        (output / "Capabilities-phase-a-001.md").write_text("category\n", encoding="utf-8")
        snapshot_dir = self.temp / "snapshots"
        snapshot_dir.mkdir()
        project_surface = self.temp / "must-not-be-linked"
        project_surface.mkdir()
        shared_surface = self.temp / "shared-hermes"
        self.configure(
            output_dir=output,
            snapshot_dir=snapshot_dir,
            surface_roots=(),
            hermes_shared_surface_root=shared_surface,
            hermes_profiles=(),
        )

        with mock.patch.object(registry.Path, "home", return_value=self.home), mock.patch.object(
            registry, "hermes_shared_surface_entries", return_value=[]
        ), mock.patch.object(registry, "hermes_shared_surface_source_errors", return_value=[]), mock.patch.object(
            registry, "hermes_shared_surface_link_preflight_errors", return_value=[]
        ), mock.patch.object(registry, "ensure_hermes_profile_skill_opt_out", return_value=0), mock.patch.object(
            registry, "remove_pristine_hermes_profile_bundles", return_value=0
        ), mock.patch.object(registry, "clear_hermes_skill_snapshots", return_value=0), contextlib.redirect_stdout(
            io.StringIO()
        ):
            registry.link_surfaces(output)

        system_link = snapshot_dir / "system"
        self.assertTrue(system_link.is_symlink())
        self.assertEqual(system_link.resolve(), output.resolve())
        cli_link = self.home / ".agents" / "bin" / "capability-registry"
        cli_target = SCRIPTS_DIR / "capability-registry"
        self.assertTrue(cli_link.is_symlink())
        self.assertTrue(registry.symlink_points_directly(cli_link, cli_target))
        with mock.patch.object(registry.Path, "home", return_value=self.home), mock.patch.object(
            registry, "hermes_shared_surface_entries", return_value=[]
        ), mock.patch.object(registry, "hermes_shared_surface_integrity_errors", return_value=[]):
            self.assertEqual(registry.check_links(output), [])
        self.assertFalse((project_surface / "Capabilities.md").exists())
        self.assertEqual(list(project_surface.glob("Capabilities-*.md")), [])

    @unittest.skipUnless(_symlink_capable(), "requires symlink privilege")
    def test_preflight_allows_only_identical_first_party_legacy_capability_router_link(self) -> None:
        shared_root = self.temp / "shared-hermes"
        shared_root.mkdir()
        desired_root = self.temp / "standalone" / "skills"
        desired_source = desired_root / "capability-router"
        desired_source.mkdir(parents=True)
        desired_skill = desired_source / "SKILL.md"
        desired_skill.write_text("identical capability router\n", encoding="utf-8")
        first_party_root = self.temp / "legacy-first-party"
        legacy_source = first_party_root / "old-router" / "capability-router"
        legacy_source.mkdir(parents=True)
        legacy_skill = legacy_source / "SKILL.md"
        legacy_skill.write_text(desired_skill.read_text(encoding="utf-8"), encoding="utf-8")
        entry = shared_root / "capability-router"
        entry.symlink_to(legacy_source, target_is_directory=True)
        entries = (("core", Path("capability-router"), desired_source),)
        self.configure(first_party_roots=(first_party_root, desired_root))

        self.assertEqual(registry.hermes_shared_surface_link_preflight_errors(shared_root, entries), [])

        legacy_skill.write_text("altered legacy router\n", encoding="utf-8")
        altered_errors = registry.hermes_shared_surface_link_preflight_errors(shared_root, entries)
        self.assertEqual(
            altered_errors,
            [f"{entry} is not a direct canonical Hermes shared-surface link"],
        )

        legacy_skill.write_text(desired_skill.read_text(encoding="utf-8"), encoding="utf-8")
        external_source = self.temp / "external" / "capability-router"
        external_source.mkdir(parents=True)
        (external_source / "SKILL.md").write_text(desired_skill.read_text(encoding="utf-8"), encoding="utf-8")
        entry.unlink()
        entry.symlink_to(external_source, target_is_directory=True)
        external_errors = registry.hermes_shared_surface_link_preflight_errors(shared_root, entries)
        self.assertEqual(
            external_errors,
            [f"{entry} is not a direct canonical Hermes shared-surface link"],
        )

    def test_project_catalog_pointer_uses_shared_cli_without_relative_registry_links(self) -> None:
        catalog = self.temp / "catalog.md"
        catalog.write_text(
            f"before\n{registry.PROJECT_CATALOG_START}\nold\n{registry.PROJECT_CATALOG_END}\nafter\n",
            encoding="utf-8",
        )
        self.configure(catalog_path=catalog)

        registry.update_project_catalog_pointer({"counts": {"capabilities": 12, "registrations": 34}})

        generated = catalog.read_text(encoding="utf-8")
        self.assertIn("category-sharded under the shared registry", generated)
        self.assertIn("lockkeeper bundle --stdin --runtime codex", generated)
        self.assertIn("lockkeeper search --stdin --runtime codex", generated)
        self.assertNotIn("node scripts/capability-catalog.mjs", generated)
        self.assertNotIn("](Capabilities.md)", generated)

    def test_first_party_provenance_accepts_each_configured_root(self) -> None:
        first_root = self.temp / "first-party-a"
        second_root = self.temp / "first-party-b"
        self.configure(first_party_roots=(first_root, second_root))

        for root in (first_root, second_root):
            with self.subTest(root=root):
                self.assertEqual(
                    registry.capability_provenance({"source_path": str(root / "nested" / "SKILL.md")}),
                    ("first-party", False),
                )
        self.assertEqual(
            registry.capability_provenance({"source_path": str(self.temp / "outside" / "SKILL.md")}),
            ("external", True),
        )

    def test_active_plugin_roots_prefer_shallow_same_version_then_higher_version(self) -> None:
        cache_root = self.temp / ".claude" / "plugins" / "cache"

        def manifest_for(root: Path) -> Path:
            manifest = root / ".claude-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}\n", encoding="utf-8")
            return manifest

        package_root = cache_root / "marketplace" / "sample-plugin" / "1.0.0"
        nested_same_version_root = package_root / "nested-copy"
        nested_higher_version_root = cache_root / "marketplace" / "sample-plugin" / "2.0.0" / "nested-copy"
        package_manifest = manifest_for(package_root)
        nested_same_version_manifest = manifest_for(nested_same_version_root)
        nested_higher_version_manifest = manifest_for(nested_higher_version_root)
        active = {"claude": {"sample-plugin@marketplace"}}

        for manifests in (
            [nested_same_version_manifest, package_manifest],
            [package_manifest, nested_same_version_manifest],
        ):
            with self.subTest(discovery_order=[str(manifest) for manifest in manifests]), mock.patch.object(
                registry, "PLUGIN_CACHE_ROOTS", [("claude", cache_root)]
            ), mock.patch.object(Path, "rglob", return_value=manifests):
                selected = registry.selected_active_plugin_roots(active)
            self.assertEqual(selected[("claude", "sample-plugin@marketplace")], package_root)

        with mock.patch.object(registry, "PLUGIN_CACHE_ROOTS", [("claude", cache_root)]), mock.patch.object(
            Path,
            "rglob",
            return_value=[package_manifest, nested_same_version_manifest, nested_higher_version_manifest],
        ):
            selected = registry.selected_active_plugin_roots(active)
        self.assertEqual(selected[("claude", "sample-plugin@marketplace")], nested_higher_version_root)


class RouterCliProjectSelectorTest(unittest.TestCase):
    """Run a copied CLI so command-line parsing never touches the real router root."""

    def setUp(self) -> None:
        self.supported_python = resolve_supported_python()
        if self.supported_python is None:
            self.skipTest("requires a discoverable Python 3.11-or-newer interpreter for CLI subprocesses")
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="phase-a-router-cli-")
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = (Path(self._temporary_directory.name) / "router").resolve(strict=False)
        scripts = self.root / "scripts"
        scripts.mkdir(parents=True)
        for filename in (
            "capability_registry.py",
            "router_config.py",
            "build_aliases.py",
            "build_synonym_batches.py",
            "build_audit_corpus.py",
            "build_skill_catalog.py",
        ):
            shutil.copy2(REPOSITORY_ROOT / "scripts" / filename, scripts / filename)
        config_directory = self.root / "config"
        config_directory.mkdir()
        (config_directory / "default.toml").write_text("# intentionally empty\n", encoding="utf-8")
        (config_directory / "phase-a.toml").write_text(
            '[project]\nname = "phase-a"\ncwd = "."\n', encoding="utf-8"
        )
        (config_directory / "demo.toml").write_text(
            '[project]\nname = "demo"\ncwd = "."\n', encoding="utf-8"
        )
        self.home = (Path(self._temporary_directory.name) / "home").resolve(strict=False)
        self.home.mkdir()

    def run_cli(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["PYTHONNOUSERSITE"] = "1"
        environment.pop("CAPABILITY_ROUTER_CONFIG", None)
        environment.pop("PYTHONPATH", None)
        return subprocess.run(
            [str(self.supported_python), str(self.root / "scripts" / "capability_registry.py"), *arguments],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_stubbed_bundle(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        script = textwrap.dedent(
            f"""
            import sys
            import capability_registry as registry

            registry.assert_registry_fresh = lambda *args, **kwargs: None
            registry.load_registry = lambda output: []
            registry.emit_bundle = lambda result, as_json: None

            def bundle(records, query, runtime, project, max_count, output, **kwargs):
                print(f"bundle_project={{project!r}}")
                return {{}}

            registry.bundle = bundle
            sys.argv = ["capability-registry", *{arguments!r}]
            raise SystemExit(registry.main())
            """
        )
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["PYTHONNOUSERSITE"] = "1"
        environment.pop("CAPABILITY_ROUTER_CONFIG", None)
        environment.pop("PYTHONPATH", None)
        return subprocess.run(
            [str(self.supported_python), "-c", script],
            cwd=self.root / "scripts",
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_invalid_startup_config_blocks_central_loaders_and_mutator_before_output_creation(self) -> None:
        (self.root / "config" / "default.toml").write_text("output_dir = [\n", encoding="utf-8")
        output = self.root / "must-not-exist"
        script = textwrap.dedent(
            f"""
            from pathlib import Path
            import capability_registry as registry

            output = Path({str(output)!r})
            for name, operation in (
                ("rebuild", lambda: registry.rebuild(output, quiet=True)),
                ("load_registry", lambda: registry.load_registry(output)),
                ("load_registrations", lambda: registry.load_registrations(output)),
            ):
                try:
                    operation()
                except registry.RouterConfigError as error:
                    print(f"{{name}}_blocked={{'Router startup configuration is invalid' in str(error)}}")
                else:
                    raise SystemExit(f"{{name}} unexpectedly ran")
            print(f"created={{output.exists()}}")
            """
        )
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["PYTHONNOUSERSITE"] = "1"
        environment.pop("CAPABILITY_ROUTER_CONFIG", None)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [str(self.supported_python), "-c", script],
            cwd=self.root / "scripts",
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rebuild_blocked=True", result.stdout)
        self.assertIn("load_registry_blocked=True", result.stdout)
        self.assertIn("load_registrations_blocked=True", result.stdout)
        self.assertIn("created=False", result.stdout)

    def test_invalid_explicit_config_blocks_all_helper_mains_before_side_effects(self) -> None:
        invalid_config = self.root / "invalid.toml"
        invalid_config.write_text("output_dir = [\n", encoding="utf-8")
        fallback_output = self.home / ".agents" / "capabilities"
        script = textwrap.dedent(
            """
            import build_aliases
            import build_audit_corpus
            import build_skill_catalog
            import build_synonym_batches
            import capability_registry as registry

            helpers = (
                build_aliases,
                build_synonym_batches,
                build_audit_corpus,
                build_skill_catalog,
            )
            for helper in helpers:
                try:
                    helper.main([])
                except registry.RouterConfigError as error:
                    print(f"{helper.__name__}_blocked={'Router startup configuration is invalid' in str(error)}")
                else:
                    raise SystemExit(f"{helper.__name__} unexpectedly ran")
            """
        )
        environment = os.environ.copy()
        environment["CAPABILITY_ROUTER_CONFIG"] = str(invalid_config)
        environment["HOME"] = str(self.home)
        environment["PYTHONNOUSERSITE"] = "1"
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [str(self.supported_python), "-c", script],
            cwd=self.root / "scripts",
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for helper_name in (
            "build_aliases",
            "build_synonym_batches",
            "build_audit_corpus",
            "build_skill_catalog",
        ):
            self.assertIn(f"{helper_name}_blocked=True", result.stdout)
        self.assertFalse(fallback_output.exists())

    def test_cli_accepts_project_before_and_after_subcommand(self) -> None:
        for arguments in (
            ["--project", "phase-a", "bundle", "--help"],
            ["bundle", "--project=phase-a", "--help"],
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Select a project configuration", result.stdout)

    def test_bundle_receives_no_project_for_bare_cli_and_selected_project_when_explicit(self) -> None:
        for arguments, expected_project in (
            (["bundle", "test"], "''"),
            (["bundle", "--project", "demo", "test"], "'demo'"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_stubbed_bundle(arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"bundle_project={expected_project}", result.stdout)

    def test_cli_rejects_identical_and_conflicting_duplicate_project_selectors(self) -> None:
        for arguments in (
            ["--project", "phase-a", "bundle", "--project", "phase-a", "--help"],
            ["bundle", "--project=phase-a", "--project", "other", "--help"],
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("only once", result.stderr)


@unittest.skipUnless(
    sys.platform != "win32" and _symlink_capable(),
    "contract tests require the POSIX sh launcher and symlink privilege",
)
class SupportedPythonCliContractTest(unittest.TestCase):
    """Exercise copied router entrypoints without user-site Python dependencies."""

    def setUp(self) -> None:
        self.supported_python = resolve_supported_python()
        if self.supported_python is None:
            self.skipTest("requires a discoverable Python 3.11-or-newer interpreter for the CLI contract")
        self.node = shutil.which("node")
        if self.node is None:
            self.skipTest("requires node to exercise the capability-catalog.mjs shim")

        self._temporary_directory = tempfile.TemporaryDirectory(prefix="phase-a-router-g7-")
        self.addCleanup(self._temporary_directory.cleanup)
        self.temp = Path(self._temporary_directory.name).resolve(strict=False)
        self.root = self.temp / "router"
        self.scripts = self.root / "scripts"
        self.scripts.mkdir(parents=True)
        for filename in (
            "capability_registry.py",
            "router_config.py",
            "capability-registry",
            "capability-catalog.mjs",
        ):
            shutil.copy2(REPOSITORY_ROOT / "scripts" / filename, self.scripts / filename)

        config_directory = self.root / "config"
        config_directory.mkdir()
        (config_directory / "default.toml").write_text(
            'output_dir = "../isolated-output"\n', encoding="utf-8"
        )
        self.home = self.temp / "home"
        self.home.mkdir()
        self.caller_cwd = self.temp / "arbitrary-cwd"
        self.caller_cwd.mkdir()
        self.supported_bin = self.temp / "supported-bin"
        self.supported_bin.mkdir()
        (self.supported_bin / "python3").symlink_to(self.supported_python)

    def unsupported_bin(self, interpreter: Path) -> Path:
        bin_directory = self.temp / "unsupported-bin"
        if not bin_directory.exists():
            bin_directory.mkdir()
            (bin_directory / "python3").symlink_to(interpreter)
        return bin_directory

    def mixed_python_bin(self) -> Path:
        bin_directory = self.temp / "mixed-bin"
        bin_directory.mkdir()
        unsupported_python = bin_directory / "python3"
        unsupported_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        unsupported_python.chmod(0o755)
        (bin_directory / "python3.14").symlink_to(self.supported_python)
        return bin_directory

    def isolated_environment(
        self, *, python_bin: Path, router_python: str | Path | None
    ) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PATH"] = os.pathsep.join((str(python_bin), "/usr/bin", "/bin"))
        environment.pop("CAPABILITY_ROUTER_CONFIG", None)
        environment.pop("PYTHONPATH", None)
        if router_python is None:
            environment.pop(SUPPORTED_PYTHON_ENV, None)
        else:
            environment[SUPPORTED_PYTHON_ENV] = str(router_python)
        return environment

    def run_entrypoint(
        self, command: list[str], *, python_bin: Path, router_python: str | Path | None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=self.caller_cwd,
            env=self.isolated_environment(python_bin=python_bin, router_python=router_python),
            text=True,
            capture_output=True,
            check=False,
        )

    def require_unsupported_system_python(self) -> Path:
        version = python_major_minor(SYSTEM_PYTHON)
        if version is None:
            self.skipTest("/usr/bin/python3 is unavailable, so unsupported-interpreter coverage is inapplicable")
        if version >= (3, 11):
            self.skipTest("/usr/bin/python3 is already supported; no unsupported interpreter is available")
        return SYSTEM_PYTHON

    def assert_minimum_version_rejection(
        self, result: subprocess.CompletedProcess[str], *, shim_preflight: bool = False
    ) -> None:
        combined_output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            combined_output,
            r"(?i)(python\s*(?:>=\s*)?3\.11|3\.11(?:\+|\s+or\s+newer))",
        )
        self.assertNotIn("ModuleNotFoundError", combined_output)
        if shim_preflight:
            self.assertNotIn("Traceback", combined_output)

    def test_supported_python_direct_and_mjs_help_are_isolated_from_user_site_and_cwd(self) -> None:
        entrypoints = (
            ("installed-python-cli", [str(self.scripts / "capability_registry.py"), "--help"]),
            ("mjs-shim", [self.node, str(self.scripts / "capability-catalog.mjs"), "--help"]),
        )
        for name, command in entrypoints:
            with self.subTest(entrypoint=name):
                result = self.run_entrypoint(
                    command, python_bin=self.supported_bin, router_python=None
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage: capability-registry", result.stdout)

        self.assertEqual((self.supported_bin / "python3").resolve(), self.supported_python)
        self.assertFalse((self.root / "isolated-output").exists())

    def test_launcher_and_mjs_fall_back_to_versioned_python_from_arbitrary_cwd(self) -> None:
        mixed_bin = self.mixed_python_bin()
        (mixed_bin / "capability-registry").symlink_to(self.scripts / "capability-registry")
        self.assertIsNone(python_major_minor(mixed_bin / "python3"))
        versioned_python = python_major_minor(mixed_bin / "python3.14")
        self.assertIsNotNone(versioned_python)
        self.assertGreaterEqual(versioned_python, (3, 11))
        entrypoints = (
            ("launcher", [str(self.scripts / "capability-registry"), "--help"]),
            ("launcher-via-path", ["capability-registry", "--help"]),
            ("mjs-shim", [self.node, str(self.scripts / "capability-catalog.mjs"), "--help"]),
        )
        for name, command in entrypoints:
            with self.subTest(entrypoint=name):
                result = self.run_entrypoint(command, python_bin=mixed_bin, router_python=None)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith("usage: capability-registry"))
                self.assertIn("usage: capability-registry", result.stdout)

        self.assertFalse((self.root / "isolated-output").exists())

    def test_quoted_supported_override_is_one_executable_path(self) -> None:
        override = self.temp / "supported python"
        override.symlink_to(self.supported_python)
        result = self.run_entrypoint(
            [str(self.scripts / "capability-registry"), "--help"],
            python_bin=self.mixed_python_bin(),
            router_python=override,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: capability-registry", result.stdout)
        self.assertFalse((self.root / "isolated-output").exists())

    def test_unsupported_override_cannot_run_poisoned_registry_or_shell_inject(self) -> None:
        poison_marker = self.temp / "poison-ran"
        injection_marker = self.temp / "injection-ran"
        (self.scripts / "capability_registry.py").write_text(
            f"from pathlib import Path\nPath({str(poison_marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        unsafe_override = f"{self.supported_python}; /usr/bin/touch {injection_marker}"
        result = self.run_entrypoint(
            [str(self.scripts / "capability-registry"), "--help"],
            python_bin=self.supported_bin,
            router_python=unsafe_override,
        )

        self.assert_minimum_version_rejection(result, shim_preflight=True)
        self.assertFalse(poison_marker.exists())
        self.assertFalse(injection_marker.exists())
        self.assertFalse((self.root / "isolated-output").exists())

    def test_direct_system_python_rejects_before_loading_router_configuration(self) -> None:
        unsupported_python = self.require_unsupported_system_python()
        result = self.run_entrypoint(
            [str(unsupported_python), str(self.scripts / "capability_registry.py"), "--help"],
            python_bin=self.unsupported_bin(unsupported_python),
            router_python=None,
        )

        self.assert_minimum_version_rejection(result)
        self.assertFalse((self.root / "isolated-output").exists())

    def test_mjs_rejects_unsupported_configured_interpreter_before_running_registry(self) -> None:
        unsupported_python = self.require_unsupported_system_python()
        (self.scripts / "capability_registry.py").write_text(
            'raise SystemExit("registry reached")\n', encoding="utf-8"
        )
        result = self.run_entrypoint(
            [self.node, str(self.scripts / "capability-catalog.mjs"), "--help"],
            python_bin=self.unsupported_bin(unsupported_python),
            router_python=unsupported_python,
        )

        self.assert_minimum_version_rejection(result, shim_preflight=True)
        self.assertNotIn("registry reached", result.stdout + result.stderr)
        self.assertFalse((self.root / "isolated-output").exists())


class PolicyPackTest(unittest.TestCase):
    """Policy packs must fail loudly when malformed, never silently fall back."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="cap-policy-")
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = (Path(self._temporary_directory.name) / "router").resolve(strict=False)
        scripts = self.root / "scripts"
        scripts.mkdir(parents=True)
        for filename in ("capability_registry.py", "router_config.py"):
            shutil.copy2(REPOSITORY_ROOT / "scripts" / filename, scripts / filename)

    def _load_module(self):
        scripts = self.root / "scripts"
        saved = sys.path[:]
        sys.path.insert(0, str(scripts))
        try:
            for name in ("router_config", "capability_registry"):
                sys.modules.pop(name, None)
            import capability_registry  # noqa: F401
            return sys.modules["capability_registry"]
        finally:
            sys.path[:] = saved

    def test_missing_pack_is_empty_and_malformed_pack_raises(self) -> None:
        registry_mod = self._load_module()
        self.assertEqual(registry_mod.policy_pack_for(""), {})
        self.assertEqual(registry_mod.policy_pack_for("undefined"), {})
        policies = self.root / "policies"
        policies.mkdir()
        (policies / "broken.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            registry_mod.policy_pack_for("broken")
        (policies / "array.json").write_text("[1, 2]", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "JSON object"):
            registry_mod.policy_pack_for("array")


class PolicyPackBehaviorTest(unittest.TestCase):
    """End-to-end policy pack behavior through bundle(): deny/require_context/prefer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._original_config = registry.ROUTER_CONFIG

    @classmethod
    def tearDownClass(cls) -> None:
        registry.configure_router(cls._original_config)

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="cap-policy-behavior-")
        self.addCleanup(self._temporary_directory.cleanup)
        self.temp = Path(self._temporary_directory.name).resolve(strict=False)
        self.router_root = self.temp / "router"
        scripts = self.router_root / "scripts"
        scripts.mkdir(parents=True)
        (self.router_root / "config").mkdir()
        for filename in ("capability_registry.py", "router_config.py"):
            shutil.copy2(REPOSITORY_ROOT / "scripts" / filename, scripts / filename)
        (self.router_root / "data" / "snapshots").mkdir(parents=True)
        self._saved_path = sys.path[:]
        sys.path.insert(0, str(scripts))
        for name in ("router_config", "capability_registry"):
            sys.modules.pop(name, None)
        import capability_registry  # noqa: F401
        self.registry = sys.modules["capability_registry"]
        config = router_config.load_router_config(
            script_path=scripts / "capability_registry.py",
            include_repository=False,
            include_explicit=False,
        )
        self.registry.configure_router(config, verified_startup=True)
        self.output = self.temp / "registry-out"
        self.output.mkdir()

    def tearDown(self) -> None:
        sys.path[:] = self._saved_path
        sys.modules.pop("capability_registry", None)
        sys.modules.pop("router_config", None)

    @staticmethod
    def record(name: str, record_type: str) -> dict:
        return {
            "id": f"{record_type}:{name}",
            "type": record_type,
            "name": name,
            "description": f"{name} capability for policy tests",
            "category": "software-engineering",
            "status": "active",
            "runtimes": ["codex", "claude", "shared"],
            "source_path": f"/fixtures/{name}/SKILL.md",
            "provenance": "first-party",
        }

    def _bundle_with_pack(self, pack: dict, records: list[dict], query: str = "implement the feature"):
        policies = self.router_root / "policies"
        policies.mkdir(exist_ok=True)
        (policies / "demo.json").write_text(json.dumps(pack), encoding="utf-8")
        with mock.patch.object(
            self.registry, "ensure_router_config_valid", lambda: None
        ):
            return self.registry.bundle(records, query, "codex", "demo", 8, self.output)

    def test_require_context_prefers_tool_alternative_with_mixed_types(self) -> None:
        # Regression: the shared-type bug made {"tool": ...} alternatives unresolvable.
        pack = {
            "require_context": [
                {
                    "choose": [{"tool": "mcp__myproject__search_decisions"}, {"mcp": "myproject"}],
                    "why": "Load prior decisions.",
                    "required": True,
                }
            ]
        }
        records = [
            self.record("mcp__myproject__search_decisions", "tool"),
            self.record("feature", "skill"),
        ]
        result = self._bundle_with_pack(pack, records, query="feature")
        context_ids = {item["id"] for item in result["bundle"] if item["lane"] == "context"}
        self.assertIn("tool:mcp__myproject__search_decisions", context_ids)

    def test_prefer_rule_adds_matching_capability_to_lane(self) -> None:
        pack = {
            "prefer": [
                {
                    "match": "\\b(library|sdk)\\b",
                    "choose": [{"mcp": "context7"}],
                    "lane": "context",
                    "why": "Prefer live docs.",
                }
            ]
        }
        records = [self.record("context7", "mcp"), self.record("feature", "skill")]
        result = self._bundle_with_pack(pack, records, query="sdk feature")
        context_names = {item["name"] for item in result["bundle"] if item["lane"] == "context"}
        self.assertIn("context7", context_names)

    def test_deny_is_absolute_even_against_required_lanes(self) -> None:
        pack = {
            "deny": [{"types": ["mcp"], "names": ["myproject"]}],
            "require_context": [
                {"choose": [{"mcp": "myproject"}], "why": "Load decisions.", "required": False}
            ],
        }
        records = [self.record("myproject", "mcp"), self.record("feature", "skill")]
        result = self._bundle_with_pack(pack, records, query="feature")
        self.assertEqual(
            [item for item in result["bundle"] if item["name"] == "myproject" and item["lane"] == "context"],
            [],
        )

    def test_policy_pack_validation_failures_are_loud(self) -> None:
        records = [self.record("feature", "skill")]
        with self.assertRaisesRegex(RuntimeError, "must be a list of strings"):
            self._bundle_with_pack({"deny": [{"types": "mcp"}]}, records, query="feature")
        with self.assertRaisesRegex(RuntimeError, "unknown type"):
            self._bundle_with_pack({"deny": [{"types": ["not-a-type"]}]}, records, query="feature")

    def test_bundle_reports_context_savings_counts_by_default(self) -> None:
        # The default route must expose the count-based savings (free, in-memory)
        # so the core value -- most capabilities kept out of context -- is visible
        # without paying the per-file stat cost of the token estimate.
        records = [self.record(f"feature-{index}", "skill") for index in range(6)]
        result = self._bundle_with_pack({}, records, query="feature")
        savings = result["savings"]
        self.assertEqual(savings["eligible_capabilities"], 6)
        self.assertEqual(savings["selected_capabilities"], len(result["bundle"]))
        self.assertEqual(
            savings["avoided_capabilities"],
            savings["eligible_capabilities"] - savings["selected_capabilities"],
        )
        # Token estimate is opt-in: the default run must not claim to be estimated.
        self.assertFalse(savings["estimated"])
        self.assertNotIn("eligible_body_tokens", savings)

    def test_bundle_savings_token_estimate_is_opt_in_and_body_only(self) -> None:
        # With estimate_savings, the summary sizes real skill bodies on disk and
        # excludes MCP/tool connectors (no local body is loaded for those).
        skill_dir = self.temp / "skills" / "feature"
        skill_dir.mkdir(parents=True)
        skill_body = skill_dir / "SKILL.md"
        skill_body.write_text("x" * 4000, encoding="utf-8")  # ~1000 tokens @ 4 B/tok

        skill = self.record("feature", "skill")
        skill["source_path"] = str(skill_body)
        connector = self.record("myservice", "mcp")
        connector["source_path"] = ""

        policies = self.router_root / "policies"
        policies.mkdir(exist_ok=True)
        (policies / "demo.json").write_text(json.dumps({}), encoding="utf-8")
        with mock.patch.object(self.registry, "ensure_router_config_valid", lambda: None):
            result = self.registry.bundle(
                [skill, connector], "feature", "codex", "demo", 8, self.output, estimate_savings=True
            )

        savings = result["savings"]
        self.assertTrue(savings["estimated"])
        self.assertEqual(savings["bytes_per_token"], 4)
        # Only the skill body counts; the MCP connector contributes 0 tokens.
        self.assertEqual(savings["eligible_body_tokens"], 1000)
        self.assertGreaterEqual(savings["eligible_body_tokens"], savings["selected_body_tokens"])
        self.assertEqual(
            savings["avoided_body_tokens"],
            savings["eligible_body_tokens"] - savings["selected_body_tokens"],
        )


class ContextSavingsHelperTest(unittest.TestCase):
    """Unit-level guarantees for the savings estimator, independent of bundle()."""

    @staticmethod
    def _record(record_id: str, record_type: str, source_path: str = "") -> dict:
        return {"id": record_id, "type": record_type, "name": record_id, "source_path": source_path}

    def test_estimate_body_tokens_sizes_only_readable_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            body = temp / "SKILL.md"
            body.write_text("y" * 400, encoding="utf-8")  # 100 tokens @ 4 B/tok
            skill = self._record("skill:a", "skill", str(body))
            self.assertEqual(registry.estimate_body_tokens(skill), 100)
            # MCP/tool connectors are called, not read: no body-token cost.
            self.assertEqual(registry.estimate_body_tokens(self._record("mcp:x", "mcp", str(body))), 0)
            # A moved/missing source degrades to 0 instead of raising.
            self.assertEqual(
                registry.estimate_body_tokens(self._record("skill:gone", "skill", str(temp / "missing.md"))),
                0,
            )

    def test_context_savings_counts_always_tokens_opt_in(self) -> None:
        eligible = [self._record(f"skill:{i}", "skill") for i in range(10)]
        selected = eligible[:3]
        counts = registry.context_savings(eligible, selected, estimate_tokens=False)
        self.assertEqual(
            (counts["eligible_capabilities"], counts["selected_capabilities"], counts["avoided_capabilities"]),
            (10, 3, 7),
        )
        self.assertFalse(counts["estimated"])
        self.assertNotIn("avoided_body_tokens", counts)

    def test_context_savings_never_reports_negative_avoided(self) -> None:
        # Defensive: selected should be a subset of eligible, but a mismatch must
        # never yield a nonsense negative "avoided" figure.
        eligible = [self._record("skill:a", "skill")]
        selected = [self._record("skill:a", "skill"), self._record("skill:b", "skill")]
        summary = registry.context_savings(eligible, selected, estimate_tokens=True)
        self.assertGreaterEqual(summary["avoided_capabilities"], 0)
        self.assertGreaterEqual(summary["avoided_body_tokens"], 0)


class SnapshotArtifactContractTest(IsolatedRegistryTest):
    """`snapshot-runtimes` must actually create every artifact it reports.

    Regression: the Codex tool snapshot was listed in the success output but
    only written when it already existed, so a non-seeded deployment (the
    documented `pip install` path) left `codex-tools.json` missing and every
    subsequent `rebuild`/`route` failed with "Required runtime snapshot is
    missing". A seeded git clone masked the bug.
    """

    def test_snapshot_runtimes_creates_every_reported_artifact(self) -> None:
        snapshot_dir = self.temp / "state" / "snapshots"
        self.configure(output_dir=self.temp / "snapshot-output", snapshot_dir=snapshot_dir)

        self.assertFalse(registry.TOOL_SNAPSHOT.exists())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            registry.refresh_runtime_snapshots()
        output = buffer.getvalue()

        reported = [
            Path(item.strip())
            for line in output.splitlines()
            if line.startswith("artifacts: ")
            for item in line.removeprefix("artifacts: ").split(",")
        ]
        self.assertIn(registry.TOOL_SNAPSHOT, reported)
        missing = [str(path) for path in reported if not path.is_file()]
        self.assertEqual(missing, [], f"snapshot-runtimes reported artifacts it did not write: {missing}")

        payload = json.loads(registry.TOOL_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(payload["tools"], [])

    def test_snapshot_runtimes_preserves_imported_codex_tools(self) -> None:
        snapshot_dir = self.temp / "state" / "snapshots"
        self.configure(output_dir=self.temp / "snapshot-output-2", snapshot_dir=snapshot_dir)

        snapshot_dir.mkdir(parents=True, exist_ok=True)
        existing = {
            "schema_version": 1,
            "captured_at": "2026-01-01T00:00:00Z",
            "tools": [{"name": "imported_tool", "runtime": "codex"}],
        }
        registry.TOOL_SNAPSHOT.write_text(json.dumps(existing), encoding="utf-8")

        with contextlib.redirect_stdout(io.StringIO()):
            registry.refresh_runtime_snapshots()

        payload = json.loads(registry.TOOL_SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual([tool["name"] for tool in payload["tools"]], ["imported_tool"])


if __name__ == "__main__":
    unittest.main()
