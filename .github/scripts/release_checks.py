#!/usr/bin/env python3
"""Release guardrails for coding-agent-cost.

Spec: docs/spec/I-2026-09-23-agent-cost-pypi-trusted-publisher/spec.md

Subcommands (every failure prints one reason per line to stderr and exits 1;
a usage/setup problem exits 2):

  version --tag vX.Y.Z [--pyproject PATH]            RULE-03
  workflow-lint PATH                                  RULE-02 / 04 / 05 / 06
  verify-pypi --version X --sums PATH
              [--assets-dir DIR] [--pypi-json PATH]  RULE-07
  docs-lint --release-doc PATH [--readme PATH]        RULE-10 / 11

The script deliberately depends only on the standard library plus PyYAML
(installed by the release workflow with a pinned version) so that it runs the
same way on a clean runner and on a maintainer's machine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

PROJECT = "coding-agent-cost"

# --- workflow-lint contract (RULE-02 / 04 / 05 / 06) -------------------------
ALLOWED_SECRETS = {"GITHUB_TOKEN"}
FORBIDDEN_WITH_KEYS = {"password", "user"}  # pypa action's API-token inputs
FORBIDDEN_ENV_KEYS = {
    "TWINE_PASSWORD",
    "TWINE_USERNAME",
    "PYPI_API_TOKEN",
    "PYPI_TOKEN",
    "UV_PUBLISH_TOKEN",
}
BUILD_JOB = "build"
PUBLISH_JOB = "publish"
RELEASE_JOB = "github-release"
# The publish-side `if` must be exactly this expression (whitespace-normalised); a
# looser expression (`||`, negation, extra refs) is a violation, not a variant.
TAG_ONLY_IF = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
GH_TOKEN_OK = {"${{ secrets.GITHUB_TOKEN }}", "${{ github.token }}"}
EXPECTED_TRIGGERS = {"push", "workflow_dispatch"}
EXPECTED_TAGS = ["v*"]
PUBLISH_ACTION = "pypa/gh-action-pypi-publish"
PACKAGES_DIR_OK = {"dist", "dist/"}
ENVIRONMENT_NAME = "pypi"
# Every `${{ ... }}` expression that mentions `secrets` must be exactly this one; the
# index form `secrets['X']`, `toJSON(secrets)` etc. are violations (RULE-04a).
ALLOWED_SECRET_EXPR = "secrets.GITHUB_TOKEN"
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
# The workflow's structure is an allowlist, not a pattern: exactly these jobs, this
# `needs` chain, these permissions, these pinned actions and these artifacts (D1, D3,
# RULE-04d / 05 / 06). Bumping a pin is a spec revision and is edited here too.
EXPECTED_JOBS = {BUILD_JOB, PUBLISH_JOB, RELEASE_JOB}
EXPECTED_NEEDS = {BUILD_JOB: [], PUBLISH_JOB: [BUILD_JOB], RELEASE_JOB: [PUBLISH_JOB]}
TOP_LEVEL_PERMISSIONS = {"contents": "read"}
EXPECTED_JOB_PERMISSIONS: Dict[str, Optional[Dict[str, str]]] = {
    BUILD_JOB: None,  # must be absent -> inherits the top-level read-only grant
    PUBLISH_JOB: {"id-token": "write", "contents": "read"},
    RELEASE_JOB: {"contents": "write"},
}
ALLOWED_USES = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",  # v7.0.1
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",  # v7.0.0
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",  # v7.0.1
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",  # v8.0.1
    "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33",  # v1.14.2
}
ARTIFACT_PATHS = {"release-dist": "dist/", "release-checksums": "checksums/SHA256SUMS"}
_CHECKOUT, _SETUP_PY, _UPLOAD, _DOWNLOAD, _PYPA = sorted(ALLOWED_USES, key=lambda u: (
    ["actions/checkout", "actions/setup-python", "actions/upload-artifact", "actions/download-artifact", PUBLISH_ACTION].index(u.split("@")[0])))
# Exact `uses` sequence per job (order and count), and the exact (kind, name, path)
# triples of the artifact steps in step order. A missing, duplicated or relocated step
# is a violation even if every individual `uses` is on the allowlist (D1, RULE-05).
EXPECTED_USES_SEQUENCE = {
    BUILD_JOB: [_CHECKOUT, _SETUP_PY, _UPLOAD, _UPLOAD],
    PUBLISH_JOB: [_DOWNLOAD, _PYPA],
    RELEASE_JOB: [_DOWNLOAD, _DOWNLOAD],
}
EXPECTED_ARTIFACT_STEPS = {
    BUILD_JOB: [("upload", "release-dist", "dist/"), ("upload", "release-checksums", "checksums/SHA256SUMS")],
    PUBLISH_JOB: [("download", "release-dist", "dist/")],
    RELEASE_JOB: [("download", "release-dist", "dist/"), ("download", "release-checksums", "checksums/")],
}
# Full step contract per job, in order. Every step is either an allowlisted `uses`
# ({"uses": ...}) or a named `run` step whose script must equal the expected script
# EXACTLY after normalisation (trailing whitespace stripped, blank lines dropped; comment
# lines are kept and compared too) and whose `if` must equal the listed value (None = no
# `if`). Substring markers were rejected in review: `... || true`, a trailing `true`, or
# the marker parked in a comment would have hollowed out the check while passing lint.
# Consequence: any edit to a run script, including a comment, is a spec revision that
# updates this table in the same change (RULE-03 / 05 / 15).
TAG_PUSH_ONLY_IF = "github.event_name == 'push'"


def _norm_script(text: object) -> List[str]:
    return [ln.rstrip() for ln in str(text).splitlines() if ln.strip()]


EXPECTED_STEPS: Dict[str, List[Dict[str, object]]] = {
    BUILD_JOB: [
        {"uses": _CHECKOUT},
        {"uses": _SETUP_PY},
        {"name": "Install pinned tooling (RULE-12)", "if": None, "run": _norm_script("""
python -m pip install --upgrade pip
python -m pip install build==1.6.1 twine==7.0.0 pyyaml==6.0.3 pytest==9.1.1
""")},
        {"name": "Lint this workflow (RULE-02 / 04 / 05 / 06)", "if": None, "run": _norm_script(
            "python .github/scripts/release_checks.py workflow-lint .github/workflows/release.yml")},
        {"name": "Guardrail unit tests", "if": None, "run": _norm_script("python -m pytest -q .github/scripts")},
        {"name": "Lint release docs (RULE-10 / 11)", "if": None, "run": _norm_script(
            "python .github/scripts/release_checks.py docs-lint --release-doc docs/release.md --readme README.md")},
        {"name": "Tag must match pyproject version (RULE-03; tag pushes only)", "if": TAG_PUSH_ONLY_IF, "run": _norm_script(
            'python .github/scripts/release_checks.py version --tag "$GITHUB_REF_NAME"')},
        {"name": "Build sdist and wheel", "if": None, "run": _norm_script("python -m build")},
        {"name": "twine check", "if": None, "run": _norm_script("python -m twine check --strict dist/*")},
        {"name": "Exactly one wheel and one sdist (RULE-05)", "if": None, "run": _norm_script("""
set -euo pipefail
test "$(ls dist/*.whl | wc -l)" -eq 1
test "$(ls dist/*.tar.gz | wc -l)" -eq 1
test "$(ls dist | wc -l)" -eq 2
""")},
        {"name": "Write SHA256SUMS (outside dist/ so publish only sees distributions)", "if": None, "run": _norm_script("""
set -euo pipefail
mkdir -p checksums
(cd dist && sha256sum *) > checksums/SHA256SUMS
cat checksums/SHA256SUMS
""")},
        {"uses": _UPLOAD},
        {"uses": _UPLOAD},
    ],
    PUBLISH_JOB: [
        {"uses": _DOWNLOAD},
        {"uses": _PYPA},
    ],
    RELEASE_JOB: [
        {"uses": _DOWNLOAD},
        {"uses": _DOWNLOAD},
        {"name": "Create the release if missing, then attach the exact published files", "if": None, "run": _norm_script("""
set -euo pipefail
if ! gh release view "$TAG" >/dev/null 2>&1; then
  gh release create "$TAG" --verify-tag --title "$TAG" --notes "See CHANGELOG.md for $TAG."
fi
gh release upload "$TAG" dist/*.whl dist/*.tar.gz checksums/SHA256SUMS --clobber
""")},
        {"name": "Verify the release carries exactly wheel, sdist and SHA256SUMS (RULE-07)", "if": None, "run": _norm_script("""
set -euo pipefail
whl=(dist/*.whl)
sdist=(dist/*.tar.gz)
test "${#whl[@]}" -eq 1
test "${#sdist[@]}" -eq 1
test -f "${whl[0]}"
test -f "${sdist[0]}"
expected="$(printf '%s\\n' "$(basename "${whl[0]}")" "$(basename "${sdist[0]}")" SHA256SUMS | sort)"
actual="$(gh release view "$TAG" --json assets --jq '.assets[].name' | sort)"
if [ "$expected" != "$actual" ]; then
  echo "release $TAG asset set differs from the published files:" >&2
  printf 'expected:\\n%s\\nactual:\\n%s\\n' "$expected" "$actual" >&2
  exit 1
fi
""")},
    ],
}

# --- verify-pypi contract (RULE-07) -------------------------------------------
SUMS_LINE_RE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)\s*$")
DIST_GLOBS = ("*.whl", "*.tar.gz")

# --- docs-lint contract (RULE-10 / 11) ----------------------------------------
# A line that mentions the old path is a violation unless it says the path is
# no longer used.
DOC_FORBIDDEN_RE = re.compile(r"(twine upload|~/\.pypirc|\.pypirc)")
DOC_ALLOW_RE = re.compile(r"(不要|使わない|廃止|no longer|not needed|not required|replaced|旧経路)")
# RULE-11 recovery wording that docs/release.md must contain.
RELEASE_DOC_REQUIRED = (
    ("failed-jobs-only re-run", r"Re-run failed jobs"),
    ("git tag -f prohibition", r"git tag -f"),
    ("all-jobs re-run prohibition after publish", r"all jobs"),
    ("tag reuse prohibition", r"[Rr]eusing a tag"),
)


class CheckFailure(Exception):
    """Aggregated reasons for a failed check."""

    def __init__(self, reasons: Iterable[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


def _fail(reasons: List[str]) -> None:
    if reasons:
        raise CheckFailure(reasons)


# ------------------------------------------------------------------ version ---
def read_project_version(pyproject: Path) -> str:
    text = pyproject.read_text(encoding="utf-8")
    try:  # Python 3.11+
        import tomllib  # type: ignore

        data = tomllib.loads(text)
        return str(data["project"]["version"])
    except ModuleNotFoundError:
        pass
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            m = re.match(r'version\s*=\s*"([^"]+)"', stripped)
            if m:
                return m.group(1)
    raise CheckFailure([f"{pyproject}: project.version not found"])


def check_version(tag: str, pyproject: Path) -> str:
    reasons: List[str] = []
    if not tag.startswith("v"):
        reasons.append(f"tag {tag!r} must start with 'v' (RULE-03)")
        _fail(reasons)
    version = read_project_version(pyproject)
    if tag[1:] != version:
        reasons.append(f"tag {tag!r} does not match pyproject version {version!r} (RULE-03)")
    _fail(reasons)
    return version


# ------------------------------------------------------------ workflow-lint ---
def _load_yaml(path: Path):
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        print("workflow-lint requires PyYAML (pip install pyyaml)", file=sys.stderr)
        sys.exit(2)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _env_keys(node) -> List[str]:
    env = node.get("env") if isinstance(node, dict) else None
    return list(env.keys()) if isinstance(env, dict) else []


def lint_workflow(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    wf = _load_yaml(path)
    reasons: List[str] = []
    if not isinstance(wf, dict):
        _fail([f"{path}: not a mapping"])

    # (a) secrets references, checked per expression so index syntax cannot slip through
    for raw_expr in EXPR_RE.findall(text):
        expr = " ".join(raw_expr.split())
        if "secrets" in expr and expr != ALLOWED_SECRET_EXPR:
            reasons.append(f"expression ${{{{ {expr} }}}} touches secrets; only ${{{{ {ALLOWED_SECRET_EXPR} }}}} allowed (RULE-04a)")

    # (e) triggers. PyYAML 1.1 parses the bare key `on` as boolean True, so also pin the
    # raw spelling: the trigger block must be introduced by a literal `on:` line and no
    # literal `true:` key may stand in for it.
    if not re.search(r"^on:\s*$", text, re.M):
        reasons.append("workflow must have a literal top-level `on:` block (RULE-02)")
    if re.search(r"^(true|True|TRUE|yes|Yes|YES):", text, re.M):
        reasons.append("a literal boolean key is used where `on:` is expected (RULE-02)")
    triggers = wf.get("on", wf.get(True))
    if not isinstance(triggers, dict):
        reasons.append("on: must be a mapping with push.tags and workflow_dispatch (RULE-02)")
        triggers = {}
    if set(triggers.keys()) != EXPECTED_TRIGGERS:
        reasons.append(f"on: keys {sorted(map(str, triggers.keys()))} != {sorted(EXPECTED_TRIGGERS)} (RULE-02)")
    push = triggers.get("push")
    if not isinstance(push, dict) or set(push.keys()) != {"tags"} or push.get("tags") != EXPECTED_TAGS:
        reasons.append("on.push must contain only tags: ['v*'] (no branches) (RULE-02)")

    # (c) env keys and (d) permissions at top level
    for key in _env_keys(wf):
        if key in FORBIDDEN_ENV_KEYS:
            reasons.append(f"top-level env.{key} is a token variable (RULE-04c)")
    # (d) permissions: mappings only, exact grants. `write-all` / `read-all` strings are
    # violations, not "no permissions".
    if "permissions" not in wf or wf.get("permissions") != TOP_LEVEL_PERMISSIONS:
        reasons.append(f"top-level permissions must be exactly {TOP_LEVEL_PERMISSIONS} (RULE-04d)")

    jobs = wf.get("jobs")
    if not isinstance(jobs, dict):
        _fail(reasons + ["jobs: missing"])
    if set(map(str, jobs.keys())) != EXPECTED_JOBS:
        reasons.append(f"jobs must be exactly {sorted(EXPECTED_JOBS)}, got {sorted(map(str, jobs.keys()))} (D1)")

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            reasons.append(f"job {job_name!r}: not a mapping")
            continue
        if "uses" in job:
            reasons.append(f"job {job_name!r}: job-level uses (reusable workflow) is not allowed (D1)")
        for key in _env_keys(job):
            if key in FORBIDDEN_ENV_KEYS:
                reasons.append(f"job {job_name!r} env.{key} is a token variable (RULE-04c)")
        expected_perms = EXPECTED_JOB_PERMISSIONS.get(str(job_name), None)
        if expected_perms is None:
            if "permissions" in job:
                reasons.append(f"job {job_name!r}: must not declare permissions (inherits read-only) (RULE-04d)")
        elif job.get("permissions") != expected_perms:
            reasons.append(f"job {job_name!r}: permissions must be exactly {expected_perms}, got {job.get('permissions')!r} (RULE-04d)")
        job_needs = job.get("needs")
        needs_list = [] if job_needs is None else (job_needs if isinstance(job_needs, list) else [job_needs])
        if [str(n) for n in needs_list] != EXPECTED_NEEDS.get(str(job_name), []):
            reasons.append(f"job {job_name!r}: needs must be exactly {EXPECTED_NEEDS.get(str(job_name), [])}, got {needs_list!r} (RULE-05)")

        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            reasons.append(f"job {job_name!r}: steps missing (D1)")
            steps = []
        uses_sequence = [str(s.get("uses")) for s in steps if isinstance(s, dict) and s.get("uses") is not None]
        if str(job_name) in EXPECTED_USES_SEQUENCE and uses_sequence != EXPECTED_USES_SEQUENCE[str(job_name)]:
            reasons.append(f"job {job_name!r}: uses sequence must be exactly {EXPECTED_USES_SEQUENCE[str(job_name)]}, got {uses_sequence} (D1 / RULE-05)")
        artifact_steps = []
        for s in steps:
            if not isinstance(s, dict) or not isinstance(s.get("uses"), str):
                continue
            w = s.get("with") if isinstance(s.get("with"), dict) else {}
            if "actions/upload-artifact@" in s["uses"]:
                artifact_steps.append(("upload", str(w.get("name")), str(w.get("path"))))
            elif "actions/download-artifact@" in s["uses"]:
                artifact_steps.append(("download", str(w.get("name")), str(w.get("path"))))
        if str(job_name) in EXPECTED_ARTIFACT_STEPS and artifact_steps != EXPECTED_ARTIFACT_STEPS[str(job_name)]:
            reasons.append(f"job {job_name!r}: artifact steps must be exactly {EXPECTED_ARTIFACT_STEPS[str(job_name)]}, got {artifact_steps} (RULE-05)")
        contract = EXPECTED_STEPS.get(str(job_name))
        if contract is not None:
            if len(steps) != len(contract):
                reasons.append(f"job {job_name!r}: expected {len(contract)} steps, got {len(steps)} (D1 step contract)")
            for idx, (step, want) in enumerate(zip(steps, contract)):
                label = f"job {job_name!r} step {idx + 1}"
                if not isinstance(step, dict):
                    reasons.append(f"{label}: not a mapping")
                    continue
                if "uses" in want:
                    if str(step.get("uses")) != want["uses"]:
                        reasons.append(f"{label}: expected uses {want['uses']!r}, got {step.get('uses')!r} (D1 step contract)")
                    continue
                if step.get("name") != want["name"]:
                    reasons.append(f"{label}: expected run step {want['name']!r}, got {step.get('name')!r} (D1 step contract)")
                    continue
                if _norm_script(step.get("run", "")) != want["run"]:
                    reasons.append(f"{label} ({want['name']}): run script must be exactly the spec'd script (D1 step contract, RULE-15)")
                cond = step.get("if")
                cond_norm = None if cond is None else " ".join(str(cond).split())
                if cond_norm != want["if"]:
                    reasons.append(f"{label} ({want['name']}): if must be {want['if']!r}, got {cond_norm!r} (D1 step contract)")
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            label = f"job {job_name!r} step {idx + 1}"
            uses = step.get("uses")
            if uses is not None and str(uses) not in ALLOWED_USES:
                reasons.append(f"{label}: uses {uses!r} is not in the pinned allowlist (RULE-06)")
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            for key in with_:
                if str(key) in FORBIDDEN_WITH_KEYS:
                    reasons.append(f"{label}: with.{key} is an API-token input (RULE-04b)")
            if isinstance(uses, str) and PUBLISH_ACTION in uses:
                if job_name != PUBLISH_JOB:
                    reasons.append(f"{label}: {PUBLISH_ACTION} used outside {PUBLISH_JOB!r} (RULE-05)")
                if str(with_.get("packages-dir")) not in PACKAGES_DIR_OK:
                    reasons.append(f"{label}: packages-dir {with_.get('packages-dir')!r} must be dist/ (RULE-05)")
                if "attestations" in with_ and str(with_.get("attestations")).lower() != "true":
                    reasons.append(f"{label}: attestations must stay at the action default (true) (RULE-08)")
            if isinstance(uses, str) and "actions/upload-artifact@" in uses:
                name, path = str(with_.get("name")), str(with_.get("path"))
                if ARTIFACT_PATHS.get(name) != path:
                    reasons.append(f"{label}: upload-artifact must be one of {ARTIFACT_PATHS}, got {name!r}: {path!r} (RULE-05)")
            for key in _env_keys(step):
                if key in FORBIDDEN_ENV_KEYS:
                    reasons.append(f"{label}: env.{key} is a token variable (RULE-04c)")

    # (f) tag-only publish jobs
    for job_name in (PUBLISH_JOB, RELEASE_JOB):
        job = jobs.get(job_name)
        if not isinstance(job, dict):
            continue
        cond = " ".join(str(job.get("if", "")).split())
        if cond != TAG_ONLY_IF:
            reasons.append(f"job {job_name!r}: if must be exactly {TAG_ONLY_IF!r}, got {cond!r} (RULE-02)")
    publish = jobs.get(PUBLISH_JOB)
    if isinstance(publish, dict):
        env_name = publish.get("environment")
        if isinstance(env_name, dict):
            env_name = env_name.get("name")
        if env_name != ENVIRONMENT_NAME:
            reasons.append(f"job {PUBLISH_JOB!r}: environment must be {ENVIRONMENT_NAME!r} (D1)")
        if not any(
            isinstance(s, dict) and PUBLISH_ACTION in str(s.get("uses", "")) for s in (publish.get("steps") or [])
        ):
            reasons.append(f"job {PUBLISH_JOB!r}: {PUBLISH_ACTION} step missing (D1)")
    release = jobs.get(RELEASE_JOB)
    if isinstance(release, dict):
        # (h) gh needs an explicit token; only the workflow's own GITHUB_TOKEN is acceptable.
        env = release.get("env") if isinstance(release.get("env"), dict) else {}
        gh_token = " ".join(str(env.get("GH_TOKEN", "")).split())
        if gh_token not in GH_TOKEN_OK:
            reasons.append(f"job {RELEASE_JOB!r}: env.GH_TOKEN must be one of {sorted(GH_TOKEN_OK)} (D1)")
    _fail(reasons)


# -------------------------------------------------------------- verify-pypi ---
def parse_sums(path: Path) -> Dict[str, str]:
    reasons: List[str] = []
    sums: Dict[str, str] = {}
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        _fail([f"{path}: empty SHA256SUMS"])
    for ln in lines:
        m = SUMS_LINE_RE.match(ln)
        if not m:
            reasons.append(f"{path}: malformed line {ln!r}")
            continue
        digest, filename = m.group(1), m.group(2)
        if filename in sums:
            reasons.append(f"{path}: duplicate filename {filename!r}")
        sums[filename] = digest
    _fail(reasons)
    return sums


def fetch_pypi_files(version: str, pypi_json: Optional[Path]) -> Dict[str, str]:
    try:
        if pypi_json is not None:
            data = json.loads(pypi_json.read_text(encoding="utf-8"))
        else:
            url = f"https://pypi.org/pypi/{PROJECT}/{version}/json"
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed https host)
                data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CheckFailure([f"PyPI JSON for {PROJECT} {version} unavailable: {exc}"])
    urls = data.get("urls") if isinstance(data, dict) else None
    if not isinstance(urls, list):
        raise CheckFailure(["PyPI JSON has no urls[]"])
    files: Dict[str, str] = {}
    for entry in urls:
        try:
            files[str(entry["filename"])] = str(entry["digests"]["sha256"])
        except (KeyError, TypeError):
            raise CheckFailure([f"PyPI JSON entry lacks filename/digests.sha256: {entry!r}"])
    return files


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _compare_sets(label: str, expected: Dict[str, str], actual: Dict[str, str]) -> List[str]:
    reasons: List[str] = []
    for missing in sorted(set(expected) - set(actual)):
        reasons.append(f"{label}: {missing!r} listed in SHA256SUMS but absent")
    for extra in sorted(set(actual) - set(expected)):
        reasons.append(f"{label}: {extra!r} present but not in SHA256SUMS")
    for name in sorted(set(expected) & set(actual)):
        if expected[name] != actual[name]:
            reasons.append(f"{label}: sha256 mismatch for {name!r}")
    return reasons


def verify_pypi(version: str, sums_path: Path, assets_dir: Optional[Path], pypi_json: Optional[Path]) -> None:
    sums = parse_sums(sums_path)
    reasons: List[str] = []
    reasons += _compare_sets("PyPI", sums, fetch_pypi_files(version, pypi_json))
    if assets_dir is not None:
        # The release must carry exactly the distributions listed in SHA256SUMS plus the
        # SHA256SUMS file itself -- nothing else (RULE-07b).
        present = sorted(p.name for p in assets_dir.iterdir() if p.is_file())
        expected_names = set(sums) | {sums_path.name}
        for extra in sorted(set(present) - expected_names):
            reasons.append(f"assets: {extra!r} present but neither a listed distribution nor {sums_path.name}")
        if sums_path.name not in present:
            reasons.append(f"assets: {sums_path.name} itself is not among the release assets")
        local: Dict[str, str] = {}
        for name in present:
            if name in sums:
                local[name] = sha256_of(assets_dir / name)
        reasons += _compare_sets("assets", sums, local)
    _fail(reasons)


# ---------------------------------------------------------------- docs-lint ---
def lint_docs(release_doc: Path, readme: Optional[Path]) -> None:
    reasons: List[str] = []
    for doc in [release_doc] + ([readme] if readme else []):
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if DOC_FORBIDDEN_RE.search(line) and not DOC_ALLOW_RE.search(line):
                reasons.append(f"{doc}:{lineno}: describes the old twine/.pypirc path as a procedure (RULE-10)")
    text = release_doc.read_text(encoding="utf-8")
    for label, pattern in RELEASE_DOC_REQUIRED:
        if not re.search(pattern, text):
            reasons.append(f"{release_doc}: missing recovery rule {label!r} (RULE-11)")
    _fail(reasons)


# --------------------------------------------------------------------- main ---
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ver = sub.add_parser("version")
    p_ver.add_argument("--tag", required=True)
    p_ver.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))

    p_lint = sub.add_parser("workflow-lint")
    p_lint.add_argument("path", type=Path)

    p_pypi = sub.add_parser("verify-pypi")
    p_pypi.add_argument("--version", required=True)
    p_pypi.add_argument("--sums", type=Path, required=True)
    p_pypi.add_argument("--assets-dir", type=Path)
    p_pypi.add_argument("--pypi-json", type=Path, help="read the PyPI JSON from a file instead of the network")

    p_docs = sub.add_parser("docs-lint")
    p_docs.add_argument("--release-doc", type=Path, required=True)
    p_docs.add_argument("--readme", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "version":
            version = check_version(args.tag, args.pyproject)
            print(f"ok: tag {args.tag} matches pyproject version {version}")
        elif args.cmd == "workflow-lint":
            lint_workflow(args.path)
            print(f"ok: {args.path} passes workflow-lint")
        elif args.cmd == "verify-pypi":
            verify_pypi(args.version, args.sums, args.assets_dir, args.pypi_json)
            print(f"ok: {PROJECT} {args.version} matches SHA256SUMS" + (" and assets" if args.assets_dir else ""))
        elif args.cmd == "docs-lint":
            lint_docs(args.release_doc, args.readme)
            print("ok: docs pass docs-lint")
    except CheckFailure as exc:
        for reason in exc.reasons:
            print(f"FAIL: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
