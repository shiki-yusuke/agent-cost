# Release procedure

`coding-agent-cost` is published to PyPI by GitHub Actions
(`.github/workflows/release.yml`) through PyPI's **Trusted Publisher** (OIDC).
The workflow neither requests nor stores a PyPI API token, and the repository
has no PyPI-related secrets.
The old `~/.pypirc` + `twine upload` path is no longer used by this procedure.
Deleting a leftover local `~/.pypirc` is the maintainer's call (it is no longer needed).

Spec: `docs/spec/I-2026-09-23-agent-cost-pypi-trusted-publisher/spec.md`.

## One-time setup (human, PyPI web UI)

1. Open <https://pypi.org/manage/project/coding-agent-cost/settings/publishing/>.
2. Add a Trusted Publisher (GitHub): owner `shiki-yusuke`, repository
   `agent-cost`, workflow `release.yml`, environment `pypi`.
3. Confirm the repository has no PyPI-related secrets:
   `gh secret list -R shiki-yusuke/agent-cost` must show nothing named
   `PYPI*` or `TWINE*`.

Until step 2 is done, a tag push fails in the `publish` job with an
authentication error and nothing is published or attached.

## Releasing a version

1. Bump `version` in `pyproject.toml` and add the section to `CHANGELOG.md`
   in a pull request. Merge it.
2. Tag the merge commit and push the tag (the tag is the **only** publish
   trigger):

   ```sh
   git tag v<version> <merge-sha>
   git push origin v<version>
   ```

3. Watch the `Release` workflow. Jobs run in order: `build` (lint, unit
   tests, tag/version check, `python -m build`, `twine check --strict`,
   `SHA256SUMS`) -> `publish` (PyPI, OIDC) -> `github-release` (creates the
   release if it does not exist and attaches the wheel, the sdist and
   `SHA256SUMS`).
4. Verify the three-way match locally (release assets, `SHA256SUMS`, PyPI):

   ```sh
   gh release download v<version> -D assets/
   python .github/scripts/release_checks.py verify-pypi \
     --version <version> --sums assets/SHA256SUMS --assets-dir assets/
   ```

   Exit 0 means every published file is on PyPI and in the release with the
   same sha256. Any missing, extra or mismatching file exits 1.
5. Optionally edit the release notes on GitHub from `CHANGELOG.md`.

A `workflow_dispatch` run is a **dry run**: it executes `build` only and
skips `publish` and `github-release`. It does not check the tag/version match
(there is no tag), so a green dry run does not prove the next tag will pass.

## Recovery (RULE-11)

Which operation is safe depends on how far the run got:

| State | Safe action |
|---|---|
| `build` or `publish` failed for an **external, retryable** reason and nothing is on PyPI (Trusted Publisher not yet registered, PyPI outage, runner hiccup) | Fix the external cause (e.g. finish the one-time setup), then **Re-run failed jobs** on the same run. The source and the tag are unchanged, so no new version is needed. |
| `build` or `publish` failed because the **source** is wrong (tag/version mismatch, packaging error) and nothing is on PyPI | Fix, merge, and push a **new** patch version tag. Never move or recreate the existing tag. |
| `publish` succeeded, `github-release` failed | Use **Re-run failed jobs** on the run. `publish` is not re-executed and the artifacts of that run are reused. If that fails too: `gh run download <run-id>` and `gh release upload v<version> <files> --clobber` by hand. If the asset-set check fails because a pre-created release carries an unrelated asset, delete that asset on GitHub and re-run the failed job. |
| Everything succeeded but the release is defective | Yank the version on PyPI and release a new patch version. |

Forbidden, because PyPI rejects re-uploading an existing filename and the run
would end red halfway:

- Re-running **all jobs** of a run whose `publish` already succeeded.
- Reusing a tag for different content: never `git tag -f`, never delete and
  recreate a pushed `v*` tag. Bump the version instead.

## Pinned tooling

`build==1.6.1`, `twine==7.0.0`, `pyyaml==6.0.3`, `pytest==9.1.1` and the
commit-SHA-pinned actions listed in `release.yml`. Updating any of them is a
spec revision, not a drive-by edit.
