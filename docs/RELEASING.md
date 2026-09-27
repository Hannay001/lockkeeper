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
   add an environment named `pypi`. It needs **no secrets or variables**: trusted
   publishing proves the upload comes from this workflow, so there is no token to
   store. Optional hardening:
   - *Required reviewers*: add yourself, and every PyPI upload waits for your approval
     in the Actions tab.
   - *Deployment branches and tags*: choose *Selected branches and tags* and allow the
     branch `main` (used by *Run workflow*) and tags matching `v*` (used by
     hand-published releases). Allowing only tags would block *Run workflow*.

After the first successful publish, the pending publisher becomes a normal one and the
project page appears at <https://pypi.org/project/lockkeeper/>.

## Each release

1. Bump `version` in `pyproject.toml` and add a `## X.Y.Z — date` section to
   [CHANGELOG.md](../CHANGELOG.md). Merge that to `main`.
2. Open *Actions → publish → Run workflow* and enter the version, for example
   `1.2.0`. Leave *commit* empty to release the latest `main`, or give the full SHA
   of an earlier `main` commit.
3. The workflow checks the version matches `pyproject.toml` at that commit, that the
   commit is on `main` and that the tag doesn't exist yet. It runs the test suite,
   builds the sdist and wheel, checks them with `twine check --strict`, smoke-tests
   the wheel in a clean environment, and uploads to PyPI. Only after the upload
   succeeds does it create the `vX.Y.Z` tag and a GitHub release titled
   `Lockkeeper X.Y.Z`, with that version's CHANGELOG section as the notes and the
   built files attached.
4. Check the result:

   ```sh
   pipx install lockkeeper       # or: pip install lockkeeper
   lockkeeper doctor
   ```

You can also publish a GitHub release by hand (tag `vX.Y.Z` on `main`); the same
workflow then builds, tests and uploads that tag to PyPI.

If a check fails, nothing is uploaded or tagged: fix it and run the workflow again.
PyPI never accepts the same version twice, so a bad upload needs a new version number.
