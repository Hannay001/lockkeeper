# Windows support

## Current support level

The router runs natively on Windows with one privilege requirement: **symlink
creation must be permitted for the invoking user**. Enable Windows Developer
Mode (Settings → Privacy & security → For developers) or run from an elevated
shell before using any command that installs links.

The core registry (rebuild, audit, CSV export, policy packs) works without
elevated privileges; only the surface/CLI linking steps need it.

## Why real symlinks

`archive_and_link()` in `scripts/capability_registry.py` creates real symlinks
(`Path.symlink_to`) and archives whatever previously occupied the link path,
rolling back loudly on `OSError`. On unprivileged Windows accounts this call
fails with a permission error; the error message points here.

### Junction fallback was rejected

NTFS junctions cannot substitute for symlinks here:

- Junctions are directory-only: the CLI link (`~/.agents/bin/capability-registry`)
  targets a file, which junctions do not support.
- `Path.is_symlink()` returns `False` for junctions, so roughly fifteen
  verification gates (`symlink_points_directly`, `check_links`, preflight
  checks) would report every junction as broken or non-canonical.
- Developer Mode makes true symlinks available to ordinary users anyway, so
  there is no privilege problem left to work around.

## Installing on Windows

Install from PyPI, which puts a native `lockkeeper.exe` on your PATH:

```powershell
py -m pip install lockkeeper      # or: pipx install lockkeeper
lockkeeper init
lockkeeper snapshot-runtimes
lockkeeper rebuild
```

The POSIX launcher and `install.sh` below are only for working from a clone.

## POSIX-only launcher and installer

The `scripts/capability-registry` sh launcher uses POSIX `exec` semantics and
`install.sh` is a POSIX shell script; both are not supported on native Windows.
Accordingly, `SupportedPythonCliContractTest` in
`tests/test_router_integration.py` is skipped on `win32`, and the two
symlink-dependent registry tests are skipped whenever the environment cannot
create symlinks (probed at runtime by `_symlink_capable()`).

CI runs the suite on `windows-latest` under Git Bash so the shared
`HOME=$(mktemp -d)` harness works across all three operating systems.
