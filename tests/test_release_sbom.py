"""Reference-installation evidence must retain identity without publishing local paths."""

import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_sbom", ROOT / "scripts/release_sbom.py")
sbom = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sbom)


def installation():
    return {
        "version": "1",
        "pip_version": "26.3",
        "environment": {"python_full_version": "3.11.11", "platform_system": "Linux"},
        "install": [
            {
                "metadata": {"name": "best-practices-mcp", "version": "0.2.0b1"},
                "download_info": {"url": "file:///private/build/candidate.whl"},
            },
            {
                "metadata": {
                    "name": "pydantic",
                    "version": "2.14.3",
                    "description": "Private metadata should not be published",
                },
                "download_info": {
                    "url": "https://user:secret@example.org/package.whl?token=secret#secret",
                    "archive_info": {"hashes": {"sha256": "a" * 64}},
                },
            },
        ],
    }


def test_public_installation_removes_local_paths_credentials_and_descriptions():
    report = sbom.sanitized_installation(installation())
    serialized = json.dumps(report)
    assert "/private" not in serialized
    assert "secret" not in serialized
    assert "description" not in serialized
    assert report["install"][0]["download_info"]["url"] == "urn:redacted:local-source"
    assert report["install"][1]["download_info"]["url"] == "https://example.org/package.whl"
    assert report["install"][1]["download_info"]["archive_info"]["hashes"]["sha256"] == "a" * 64


@pytest.mark.parametrize("change", ["missing-root", "duplicate", "invalid-version", "schema"])
def test_invalid_installation_identity_fails(change):
    report = installation()
    if change == "missing-root":
        report["install"].pop(0)
    elif change == "duplicate":
        report["install"].append(report["install"][1])
    elif change == "invalid-version":
        report["install"][1]["metadata"]["version"] = ""
    else:
        report["version"] = "2"
    with pytest.raises(ValueError):
        sbom.installation_packages(report)


def document():
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {"component": {"name": "best-practices-mcp", "version": "0.2.0b1"}},
        "components": [{"name": "pip"}, {"name": "pydantic"}],
    }


def test_sbom_records_candidate_hash_reference_scope_and_platform(tmp_path):
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"qualified archive")
    result = sbom.annotate_sbom(document(), artifact, installation(), "b" * 40)
    root = result["metadata"]["component"]
    assert root["hashes"] == [
        {"alg": "SHA-256", "content": hashlib.sha256(b"qualified archive").hexdigest()}
    ]
    properties = {
        item["name"].rsplit(":", 1)[1]: item["value"] for item in result["metadata"]["properties"]
    }
    assert properties["artifact"] == "candidate.whl"
    assert properties["source-commit"] == "b" * 40
    assert properties["platform"] == "Linux"
    assert properties["python"] == "3.11.11"
    assert properties["scope"] == "reference installation; includes environment bootstrap tools"


@pytest.mark.parametrize("path_type", [Path, PurePosixPath, PureWindowsPath])
def test_report_generation_uses_isolated_tool_and_scans_only_runtime_dependencies(
    tmp_path, path_type
):
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"candidate")
    installation_path = tmp_path / "installation.json"
    installation_path.write_text(json.dumps(installation()))
    reports = tmp_path / "public"
    runtime_python = path_type(tmp_path) / "runtime venv" / "python"
    tool_python = path_type(tmp_path) / "tools venv" / "python"
    calls = []

    def run(label, command):
        calls.append(command)
        if "environment" in command:
            target = Path(command[command.index("--output-file") + 1])
            assert target.parent == tmp_path
            target.write_text(json.dumps(document()))

    result = sbom.generate_reference(
        artifact,
        runtime_python,
        tool_python,
        installation_path,
        reports,
        ROOT,
        "b" * 40,
        run,
    )
    assert calls[0][:5] == [
        str(tool_python),
        "-m",
        "cyclonedx_py",
        "environment",
        str(runtime_python),
    ]
    assert calls[1][0] == str(tool_python)
    assert "--validate" in calls[1]
    assert result == {
        "sbom": "runtime-wheel.cdx.json",
        "inventory": "runtime-wheel.inventory.json",
        "installation": "pip-wheel.json",
    }
    inventory = json.loads((reports / result["inventory"]).read_text())
    assert inventory["results"][0]["packages"] == [
        {"package": {"name": "pydantic", "version": "2.14.3", "ecosystem": "PyPI"}}
    ]
    assert "secret" not in (reports / result["installation"]).read_text()


def test_generator_failure_never_copies_raw_report_to_public_directory(tmp_path):
    artifact = tmp_path / "candidate.whl"
    artifact.write_bytes(b"candidate")
    install = tmp_path / "installation.json"
    install.write_text(json.dumps(installation()))

    def fail(label, command):
        raw = Path(command[command.index("--output-file") + 1])
        raw.write_text("secret private raw output")
        raise RuntimeError("generator failed")

    reports = tmp_path / "public"
    with pytest.raises(RuntimeError, match="generator failed"):
        sbom.generate_reference(
            artifact, Path("python"), Path("tool"), install, reports, ROOT, "sha", fail
        )
    assert list(reports.iterdir()) == []
