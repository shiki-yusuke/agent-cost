# Guardrail tests for release_checks.py.
# Spec: docs/spec/I-2026-09-23-agent-cost-pypi-trusted-publisher/spec.md §5
# (TEST-01 version, TEST-02 workflow-lint, TEST-03 verify-pypi, TEST-04 docs-lint,
#  TEST-08 tag-only `if`, TEST-09 recovery wording). Expected values come from the
# spec's RULE-02/03/04/05/06/07/10/11, not from the implementation.
#
# Run: python -m pytest -q .github/scripts
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import release_checks as rc  # noqa: E402

WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
RELEASE_DOC = REPO / "docs" / "release.md"
README = REPO / "README.md"
PYPROJECT = REPO / "pyproject.toml"


def run(argv):
    return rc.main(argv)


def mutate(src: Path, dst: Path, old: str, new: str) -> Path:
    text = src.read_text(encoding="utf-8")
    assert text.count(old) >= 1, f"fixture anchor not found: {old!r}"
    dst.write_text(text.replace(old, new), encoding="utf-8")
    return dst


# ---------------------------------------------------------------- TEST-01 ---
def test_01_version_matches_real_pyproject(capsys):
    version = rc.read_project_version(PYPROJECT)
    assert run(["version", "--tag", f"v{version}", "--pyproject", str(PYPROJECT)]) == 0


def test_01_version_mismatch_exits_1(tmp_path, capsys):
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "x"\nversion = "0.2.2"\n', encoding="utf-8")
    assert run(["version", "--tag", "v0.2.3", "--pyproject", str(py)]) == 1
    assert "does not match" in capsys.readouterr().err


def test_01_version_tag_without_v_exits_1(tmp_path, capsys):
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nversion = "0.2.2"\n', encoding="utf-8")
    assert run(["version", "--tag", "0.2.2", "--pyproject", str(py)]) == 1
    assert "must start with 'v'" in capsys.readouterr().err


# ---------------------------------------------------------------- TEST-02 ---
def test_02_real_workflow_passes_lint(capsys):
    assert run(["workflow-lint", str(WORKFLOW)]) == 0


@pytest.mark.parametrize(
    "old,new,expect",
    [
        # RULE-04a: a long-lived token reference.
        ("GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}", "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n      PYPI_X: ${{ secrets.PYPI_API_TOKEN }}", "secrets.PYPI_API_TOKEN"),
        # RULE-04b: pypa action's API-token input.
        ("          packages-dir: dist/", "          packages-dir: dist/\n          password: not-a-real-token", "with.password"),
        # RULE-04c: twine env variable.
        ("      TAG: ${{ github.ref_name }}", "      TAG: ${{ github.ref_name }}\n      TWINE_PASSWORD: x", "env.TWINE_PASSWORD"),
        # RULE-06: unpinned action.
        ("pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33", "pypa/gh-action-pypi-publish@release/v1", "pinned allowlist"),
        # RULE-04d: id-token: write on the build job.
        ("    name: Build and check\n    runs-on: ubuntu-latest", "    name: Build and check\n    runs-on: ubuntu-latest\n    permissions:\n      id-token: write", "must not declare permissions"),
        # RULE-04d: contents: write on the publish job.
        ("      id-token: write\n      contents: read", "      id-token: write\n      contents: write", "permissions must be exactly"),
        # RULE-02: a branch trigger sneaks in.
        ('    tags: ["v*"]', '    tags: ["v*"]\n    branches: [main]', "no branches"),
        # RULE-02: tags widened.
        ('    tags: ["v*"]', '    tags: ["*"]', "tags: ['v*']"),
        # RULE-05: packages-dir changed.
        ("          packages-dir: dist/", "          packages-dir: .", "packages-dir"),
        # D1: environment renamed.
        ("    environment: pypi", "    environment: testpypi", "environment must be"),
        # D1: gh token missing.
        ("      GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n", "", "GH_TOKEN"),
        # RULE-04a: index syntax must not slip past the secrets check.
        ("      TAG: ${{ github.ref_name }}", "      TAG: ${{ github.ref_name }}\n      PYPI_X: ${{ secrets['PYPI_TOKEN'] }}", "touches secrets"),
        # RULE-04a: dumping the whole secrets context.
        ("      TAG: ${{ github.ref_name }}", "      TAG: ${{ github.ref_name }}\n      ALL: ${{ toJSON(secrets) }}", "touches secrets"),
        # RULE-04d: top-level write-all is not "no permissions".
        ("permissions:\n  contents: read\n\nconcurrency:", "permissions: write-all\n\nconcurrency:", "top-level permissions must be exactly"),
        # RULE-04d: build job must not declare its own permissions.
        ("    name: Build and check\n    runs-on: ubuntu-latest", "    name: Build and check\n    runs-on: ubuntu-latest\n    permissions: read-all", "must not declare permissions"),
        # D1: an extra job is a violation even if harmless-looking.
        ("jobs:\n  build:", "jobs:\n  extra:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n  build:", "jobs must be exactly"),
        # D1: job-level reusable workflow.
        ("jobs:\n  build:", "jobs:\n  extra:\n    uses: octo/wf/.github/workflows/x.yml@dc37677b2e1c63e2034f94d8a5b11f265b73ba33\n  build:", "jobs must be exactly"),
        # RULE-05: needs widened.
        ("    needs: build\n", "    needs: [build, github-release]\n", "needs must be exactly"),
        # RULE-06: a different action carrying a valid-looking SHA.
        ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "evil/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "pinned allowlist"),
        # RULE-08: attestations disabled.
        ("          packages-dir: dist/", "          packages-dir: dist/\n          attestations: false", "attestations must stay"),
        # RULE-05: artifact renamed so publish would download the wrong thing.
        ("          name: release-dist\n          path: dist/\n          if-no-files-found: error", "          name: somewhere-else\n          path: dist/\n          if-no-files-found: error", "upload-artifact must be one of"),
        # RULE-02: `on` spelled as a list of events.
        ('on:\n  push:\n    tags: ["v*"]\n  workflow_dispatch:\n', "on: [push, workflow_dispatch]\n", "on:"),
        # RULE-02: literal boolean key in place of on.
        ("\non:\n  push:", "\ntrue:\n  push:", "literal top-level `on:`"),
        # RULE-05: the checksum upload step deleted (every remaining uses is still allowlisted).
        ("      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1\n        with:\n          name: release-checksums\n          path: checksums/SHA256SUMS\n          if-no-files-found: error\n", "", "uses sequence must be exactly"),
        # RULE-05: the publish step duplicated.
        ("      - uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2\n        with:\n          packages-dir: dist/\n", "      - uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2\n        with:\n          packages-dir: dist/\n      - uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2\n        with:\n          packages-dir: dist/\n", "uses sequence must be exactly"),
        # RULE-05: an allowlisted action relocated into the publish job.
        ("    steps:\n      - uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1\n        with:\n          name: release-dist\n          path: dist/\n      # No API token", "    steps:\n      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n      - uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1\n        with:\n          name: release-dist\n          path: dist/\n      # No API token", "uses sequence must be exactly"),
        # RULE-05: download path changed so publish would look in the wrong directory.
        ("          name: release-dist\n          path: dist/\n      # No API token", "          name: release-dist\n          path: pkgs/\n      # No API token", "artifact steps must be exactly"),
        # RULE-05: github-release downloads the checksums into the wrong place.
        ("          name: release-checksums\n          path: checksums/\n", "          name: release-checksums\n          path: dist/\n", "artifact steps must be exactly"),
        # RULE-03: the tag/version check step deleted (Copilot #1) -- every uses step still matches.
        ("      - name: Tag must match pyproject version (RULE-03; tag pushes only)\n        if: github.event_name == 'push'\n        run: python .github/scripts/release_checks.py version --tag \"$GITHUB_REF_NAME\"\n", "", "expected 13 steps"),
        # RULE-03: the version check kept in place but hollowed out.
        ("        run: python .github/scripts/release_checks.py version --tag \"$GITHUB_REF_NAME\"", "        run: echo skip", "run script must be exactly"),
        # RULE-03: failure swallowed with `|| true` (terra #1).
        ("        run: python .github/scripts/release_checks.py version --tag \"$GITHUB_REF_NAME\"", "        run: python .github/scripts/release_checks.py version --tag \"$GITHUB_REF_NAME\" || true", "run script must be exactly"),
        # RULE-03: the real command parked in a comment, followed by a no-op (terra #1).
        ("        run: python .github/scripts/release_checks.py version --tag \"$GITHUB_REF_NAME\"", "        run: |\n          # python .github/scripts/release_checks.py version --tag \"$GITHUB_REF_NAME\"\n          true", "run script must be exactly"),
        # RULE-03: an extra command appended after the genuine one.
        ("        run: python .github/scripts/release_checks.py version --tag \"$GITHUB_REF_NAME\"", "        run: |\n          python .github/scripts/release_checks.py version --tag \"$GITHUB_REF_NAME\"\n          git tag -f \"$GITHUB_REF_NAME\"", "run script must be exactly"),
        # RULE-03: the version check's `if` removed (would now run on dispatch, hiding the contract).
        ("      - name: Tag must match pyproject version (RULE-03; tag pushes only)\n        if: github.event_name == 'push'\n", "      - name: Tag must match pyproject version (RULE-03; tag pushes only)\n", "if must be"),
        # RULE-12: twine check step replaced by a no-op with the same name.
        ("        run: python -m twine check --strict dist/*", "        run: true", "run script must be exactly"),
        # RULE-07 / 14: the remote asset-set check renamed (terra #3: a rename alone is caught by the name check).
        ("      - name: Verify the release carries exactly wheel, sdist and SHA256SUMS (RULE-07)\n", "      - name: Something else\n", "expected run step"),
        # RULE-07 / 14: the asset-set check kept its name but the script became a no-op.
        ("          if [ \"$expected\" != \"$actual\" ]; then\n            echo \"release $TAG asset set differs from the published files:\" >&2\n            printf 'expected:\\n%s\\nactual:\\n%s\\n' \"$expected\" \"$actual\" >&2\n            exit 1\n          fi\n", "          true\n", "run script must be exactly"),
    ],
)
def test_02_mutated_workflow_fails_lint(tmp_path, capsys, old, new, expect):
    wf = mutate(WORKFLOW, tmp_path / "release.yml", old, new)
    assert run(["workflow-lint", str(wf)]) == 1
    assert expect in capsys.readouterr().err


def test_02_release_job_if_alone_cannot_be_loosened(tmp_path, capsys):
    text = WORKFLOW.read_text(encoding="utf-8")
    anchor = "    if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    assert text.count(anchor) == 2
    head, _, tail = text.rpartition(anchor)  # only the github-release occurrence
    wf = tmp_path / "release.yml"
    wf.write_text(head + "    if: always()" + tail, encoding="utf-8")
    assert run(["workflow-lint", str(wf)]) == 1
    err = capsys.readouterr().err
    assert "'github-release'" in err and "if must be exactly" in err


def test_02_asset_check_step_deleted_entirely_fails_lint(tmp_path, capsys):
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("      # --clobber only replaces same-named assets")
    end = text.index("            exit 1\n          fi\n", start) + len("            exit 1\n          fi\n")
    wf = tmp_path / "release.yml"
    wf.write_text(text[:start] + text[end:], encoding="utf-8")
    assert run(["workflow-lint", str(wf)]) == 1
    err = capsys.readouterr().err
    assert "'github-release'" in err and "expected 4 steps, got 3" in err


# ---------------------------------------------------------------- TEST-08 ---
@pytest.mark.parametrize(
    "new",
    [
        "    if: github.event_name == 'push'",  # dropped the tag condition
        "    if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v') || true",  # loosened
        "    if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/')",  # any tag
        "",  # no condition at all -> dispatch would publish
    ],
)
def test_08_publish_if_must_be_tag_only(tmp_path, capsys, new):
    old = "    if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    wf = mutate(WORKFLOW, tmp_path / "release.yml", old, new)
    assert run(["workflow-lint", str(wf)]) == 1
    assert "if must be exactly" in capsys.readouterr().err


# ---------------------------------------------------------------- TEST-03 ---
WHL = "coding_agent_cost-0.2.2-py3-none-any.whl"
SDIST = "coding_agent_cost-0.2.2.tar.gz"


def _write_assets(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir()
    digests = {}
    for name, payload in ((WHL, b"wheel-bytes"), (SDIST, b"sdist-bytes")):
        (assets / name).write_bytes(payload)
        digests[name] = hashlib.sha256(payload).hexdigest()
    sums = assets / "SHA256SUMS"
    sums.write_text("".join(f"{d}  {n}\n" for n, d in digests.items()), encoding="utf-8")
    pypi = tmp_path / "pypi.json"
    pypi.write_text(json.dumps({"urls": [{"filename": n, "digests": {"sha256": d}} for n, d in digests.items()]}), encoding="utf-8")
    return assets, sums, pypi, digests


def _verify(sums, pypi, assets=None):
    argv = ["verify-pypi", "--version", "0.2.2", "--sums", str(sums), "--pypi-json", str(pypi)]
    if assets is not None:
        argv += ["--assets-dir", str(assets)]
    return run(argv)


def test_03_three_way_match_exits_0(tmp_path, capsys):
    assets, sums, pypi, _ = _write_assets(tmp_path)
    assert _verify(sums, pypi, assets) == 0


def test_03_pypi_missing_file_exits_1(tmp_path, capsys):
    assets, sums, pypi, digests = _write_assets(tmp_path)
    pypi.write_text(json.dumps({"urls": [{"filename": WHL, "digests": {"sha256": digests[WHL]}}]}), encoding="utf-8")
    assert _verify(sums, pypi) == 1
    assert "listed in SHA256SUMS but absent" in capsys.readouterr().err


def test_03_pypi_extra_file_exits_1(tmp_path, capsys):
    assets, sums, pypi, digests = _write_assets(tmp_path)
    data = json.loads(pypi.read_text())
    data["urls"].append({"filename": "extra.egg", "digests": {"sha256": "0" * 64}})
    pypi.write_text(json.dumps(data))
    assert _verify(sums, pypi) == 1
    assert "present but not in SHA256SUMS" in capsys.readouterr().err


def test_03_pypi_digest_mismatch_exits_1(tmp_path, capsys):
    assets, sums, pypi, digests = _write_assets(tmp_path)
    data = json.loads(pypi.read_text())
    data["urls"][0]["digests"]["sha256"] = "f" * 64
    pypi.write_text(json.dumps(data))
    assert _verify(sums, pypi) == 1
    assert "sha256 mismatch" in capsys.readouterr().err


def test_03_duplicate_sums_line_exits_1(tmp_path, capsys):
    assets, sums, pypi, digests = _write_assets(tmp_path)
    sums.write_text(sums.read_text() + f"{digests[WHL]}  {WHL}\n")
    assert _verify(sums, pypi) == 1
    assert "duplicate filename" in capsys.readouterr().err


def test_03_malformed_digest_exits_1(tmp_path, capsys):
    assets, sums, pypi, digests = _write_assets(tmp_path)
    sums.write_text(f"not-a-digest  {WHL}\n")
    assert _verify(sums, pypi) == 1
    assert "malformed line" in capsys.readouterr().err


def test_03_empty_sums_exits_1(tmp_path, capsys):
    assets, sums, pypi, _ = _write_assets(tmp_path)
    sums.write_text("\n")
    assert _verify(sums, pypi) == 1
    assert "empty SHA256SUMS" in capsys.readouterr().err


def test_03_pypi_unavailable_exits_1(tmp_path, capsys):
    assets, sums, pypi, _ = _write_assets(tmp_path)
    assert _verify(sums, tmp_path / "does-not-exist.json") == 1
    assert "unavailable" in capsys.readouterr().err


def test_03_pypi_invalid_json_exits_1(tmp_path, capsys):
    assets, sums, pypi, _ = _write_assets(tmp_path)
    pypi.write_text("{not json")
    assert _verify(sums, pypi) == 1
    assert "unavailable" in capsys.readouterr().err


def test_03_pypi_without_urls_exits_1(tmp_path, capsys):
    assets, sums, pypi, _ = _write_assets(tmp_path)
    pypi.write_text(json.dumps({"info": {}}))
    assert _verify(sums, pypi) == 1
    assert "no urls" in capsys.readouterr().err


def test_03_assets_missing_wheel_exits_1(tmp_path, capsys):
    assets, sums, pypi, _ = _write_assets(tmp_path)
    (assets / WHL).unlink()
    assert _verify(sums, pypi, assets) == 1
    assert "assets" in capsys.readouterr().err


def test_03_assets_extra_file_exits_1(tmp_path, capsys):
    assets, sums, pypi, _ = _write_assets(tmp_path)
    (assets / "notes.txt").write_text("x")
    assert _verify(sums, pypi, assets) == 1
    assert "neither a listed distribution" in capsys.readouterr().err


def test_03_assets_sha_mismatch_exits_1(tmp_path, capsys):
    assets, sums, pypi, _ = _write_assets(tmp_path)
    (assets / SDIST).write_bytes(b"tampered")
    assert _verify(sums, pypi, assets) == 1
    assert "sha256 mismatch" in capsys.readouterr().err


def test_03_assets_without_sums_file_exits_1(tmp_path, capsys):
    assets, sums, pypi, _ = _write_assets(tmp_path)
    moved = tmp_path / "SHA256SUMS"
    sums.rename(moved)
    assert _verify(moved, pypi, assets) == 1
    assert "not among the release assets" in capsys.readouterr().err


# ---------------------------------------------------------- TEST-04 / 09 ---
def test_04_real_docs_pass(capsys):
    assert run(["docs-lint", "--release-doc", str(RELEASE_DOC), "--readme", str(README)]) == 0


def test_04_twine_upload_as_procedure_fails(tmp_path, capsys):
    doc = mutate(RELEASE_DOC, tmp_path / "release.md", "## Releasing a version", "## Releasing a version\n\nRun `twine upload dist/*` with the token in ~/.pypirc.")
    assert run(["docs-lint", "--release-doc", str(doc)]) == 1
    assert "RULE-10" in capsys.readouterr().err


def test_04_readme_twine_procedure_fails(tmp_path, capsys):
    readme = mutate(README, tmp_path / "README.md", "see [docs/release.md](docs/release.md).", "run `twine upload dist/*`.")
    assert run(["docs-lint", "--release-doc", str(RELEASE_DOC), "--readme", str(readme)]) == 1
    assert "RULE-10" in capsys.readouterr().err


@pytest.mark.parametrize("old", ["Re-run failed jobs", "git tag -f", "all jobs", "Reusing a tag"])
def test_09_recovery_wording_required(tmp_path, capsys, old):
    doc = mutate(RELEASE_DOC, tmp_path / "release.md", old, "REDACTED")
    assert run(["docs-lint", "--release-doc", str(doc)]) == 1
    assert "RULE-11" in capsys.readouterr().err
