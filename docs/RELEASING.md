# Releasing Lockkeeper

Releases are published to [PyPI](https://pypi.org/project/lockkeeper/) by
[.github/workflows/publish.yml](../.github/workflows/publish.yml) when a GitHub release
is published. It uses PyPI **trusted publishing**, so no API token is stored in GitHub.

## One-time setup

1. **Create a PyPI account** at <https://pypi.org/account/register/> and enable
   two-factor authentication.
2. **Register the trusted publisher.** Because the project doesn't exist on PyPI yet,
   add a *pending* publisher at <https://pypi.org/manage/account/publishing/>:
   - PyPI project name: `lockkeeper`
   - Owner: `Hannay001`
   - Repository name: `lockkeeper`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. **Create the GitHub environment.** In the repository's *Settings → Environments*,
   add an environment named `pypi`. Optionally require a reviewer, so every publish
   waits for your approval.

After the first successful publish, the pending publisher becomes a normal one and the
project page appears at <https://pypi.org/project/lockkeeper/>.

## Each release

1. Bump `version` in `pyproject.toml` and add a section to [CHANGELOG.md](../CHANGELOG.md).
   Merge that to `main`.
2. Create a release on GitHub (*Releases → Draft a new release*):
   - Tag: `v` + the version, for example `v1.2.0`, created on `main`.
   - Title: `Lockkeeper 1.2.0`.
   - Notes: paste that version's CHANGELOG section.
3. Publish the release. The workflow checks that the tag matches `pyproject.toml`,
   runs the test suite, builds the sdist and wheel, checks them with
   `twine check --strict`, smoke-tests the wheel in a clean environment, and uploads
   to PyPI.
4. Check the result:

   ```sh
   pipx install lockkeeper       # or: pip install lockkeeper
   lockkeeper doctor
   ```

If the tag and version don't match, the workflow stops before building. Fix the
version, then delete and recreate the release and tag. PyPI never accepts the same
version twice, so a bad upload needs a new version number.
