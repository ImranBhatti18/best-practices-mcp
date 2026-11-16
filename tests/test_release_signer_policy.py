"""Post-publication verification must select only reviewed signer identities."""

import ast
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VERIFY = yaml.safe_load((ROOT / ".github/workflows/verify-release.yml").read_text())
RELEASE = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
JOB = VERIFY["jobs"]["verify"]


def selector():
    step = next(
        item
        for item in JOB["steps"]
        if item["name"] == "Select the reviewed signer policy for this release"
    )
    source = step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    module = ast.parse(source)
    module.body = [
        item
        for item in module.body
        if isinstance(item, ast.Import | ast.ImportFrom | ast.FunctionDef)
    ]
    namespace = {}
    exec(compile(module, "reviewed-release-signer", "exec"), namespace)
    return namespace["select_signer"]


@pytest.mark.parametrize("tag", ["v0.2.0-beta.3", "v0.3.0-beta.1", "v0.5.0"])
def test_legacy_releases_keep_source_signer(tag):
    assert selector()(tag, "a" * 40) == (
        "reponomics/best-practices-mcp/.github/workflows/release.yml",
        "a" * 40,
    )


@pytest.mark.parametrize("tag", ["v0.5.1", "v0.6.0-beta.1", "v1.0.0"])
def test_later_releases_require_reviewed_external_signer(tag, monkeypatch):
    for name in ("BUILDER_WORKFLOW", "BUILDER_COMMIT"):
        monkeypatch.setenv(name, JOB["env"][name])
        assert JOB["env"][name] == RELEASE["env"][name]
    assert selector()(tag, "a" * 40) == (
        JOB["env"]["BUILDER_WORKFLOW"],
        JOB["env"]["BUILDER_COMMIT"],
    )


@pytest.mark.parametrize(
    ("tag", "sha"), [("main", "a" * 40), ("v0.5.0\ninjected", "a" * 40), ("v0.5.0", "bad")]
)
def test_signer_policy_rejects_invalid_release_identity(tag, sha):
    with pytest.raises(ValueError, match="Invalid release identity"):
        selector()(tag, sha)


def test_online_and_bundle_verification_use_selected_policy():
    commands = [
        step["run"] for step in JOB["steps"] if "gh attestation verify" in step.get("run", "")
    ]
    assert len(commands) == 2
    for source in commands:
        assert source.count("gh attestation verify") == 2
        assert source.count('--signer-workflow "$SIGNER_WORKFLOW"') == 2
        assert source.count('--signer-digest "$SIGNER_COMMIT"') == 2
        assert source.count('--source-digest "$EXPECTED_SHA"') == 2
        assert source.count('--source-ref "refs/tags/$RELEASE_TAG"') == 2
