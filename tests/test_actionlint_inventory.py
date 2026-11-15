"""Fail-closed inventory generation without network or Go in ordinary tests."""

import importlib.util
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "actionlint_inventory", Path(__file__).resolve().parents[1] / "scripts/actionlint_inventory.py"
)
assert SPEC and SPEC.loader
INVENTORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INVENTORY)
SHA = "a" * 40
ROOT_MODULE = {"Path": INVENTORY.MODULE, "Main": True}
SYS_MODULE = {"Path": "golang.org/x/sys", "Version": "v0.42.0"}


def assessment_files(tmp_path, *, revision=SHA, version="0.42.0", expires="2099-01-01T00:00:00Z"):
    policy = tmp_path / "policy.toml"
    policy.write_text('[[PackageOverrides]]\nname = "example"\nlicense.override = ["MIT"]\n')
    assessment = tmp_path / "assessment.toml"
    assessment.write_text(
        f'revision = "{revision}"\nmodule = "golang.org/x/sys"\nversion = "{version}"\n'
        f'advisory = "GO-2026-5024"\neffective_until = {expires}\nreason = "Assessed unreachable"\n'
    )
    return {"policy": policy, "assessment": assessment}


def config_file(tmp_path, revision=SHA, repository=INVENTORY.REPOSITORY):
    config = tmp_path / "hooks.yaml"
    config.write_text(
        f"repos:\n  - repo: {repository}\n    rev: {revision}\n"
        "    hooks:\n      - id: actionlint\n",
        encoding="utf-8",
    )
    return config


def test_read_exact_official_pin(tmp_path):
    assert INVENTORY.read_pin(config_file(tmp_path)) == SHA


@pytest.mark.parametrize("revision", ["v1.7.12", "main", "abc123", "1234"])
def test_reject_mutable_or_abbreviated_revision(tmp_path, revision):
    with pytest.raises(ValueError, match="full commit SHA"):
        INVENTORY.read_pin(config_file(tmp_path, revision))


def test_reject_other_repository(tmp_path):
    with pytest.raises(ValueError, match="official actionlint"):
        INVENTORY.read_pin(config_file(tmp_path, repository="https://example.com/fork"))


def test_reject_ambiguous_hook(tmp_path):
    config = config_file(tmp_path)
    with config.open("a", encoding="utf-8") as stream:
        stream.write(config.read_text().removeprefix("repos:\n"))
    with pytest.raises(ValueError, match="exactly one"):
        INVENTORY.read_pin(config)


@pytest.mark.parametrize("origin", [{}, {"Hash": "b" * 40, "URL": INVENTORY.REPOSITORY}])
def test_resolved_source_must_match_sha(tmp_path, monkeypatch, origin):
    monkeypatch.setattr(
        INVENTORY,
        "run_go",
        lambda *args: json.dumps(
            {"Path": INVENTORY.MODULE, "Version": "v1.7.12", "Origin": origin}
        ),
    )
    with pytest.raises(ValueError, match="pinned repository and SHA"):
        INVENTORY.resolve_root("go", tmp_path, SHA)


def test_graph_includes_root_and_normalizes_versions_for_osv_licenses():
    modules = INVENTORY.decode_graph(
        json.dumps({"Path": INVENTORY.MODULE, "Main": True})
        + "\n"
        + json.dumps({"Path": "golang.org/x/net", "Version": "v0.52.0"})
    )
    assert INVENTORY.graph_packages(modules, "v1.7.12") == [
        {"package": {"name": INVENTORY.MODULE, "version": "1.7.12", "ecosystem": "Go"}},
        {"package": {"name": "golang.org/x/net", "version": "0.52.0", "ecosystem": "Go"}},
    ]


@pytest.mark.parametrize(
    "modules",
    [
        [],
        [{"Path": "example.com/dep", "Version": "v1.0.0"}],
        [{"Path": INVENTORY.MODULE, "Main": True, "Error": {"Err": "missing"}}],
        [{"Path": INVENTORY.MODULE, "Main": True, "Replace": {"Path": "../local"}}],
        [{"Path": "example.com/unversioned"}],
        [{"Path": INVENTORY.MODULE, "Main": True}] * 2,
    ],
)
def test_reject_incomplete_or_unrepresentable_graph(modules):
    with pytest.raises(ValueError):
        INVENTORY.graph_packages(modules, "v1.7.12")


def test_success_retains_inputs_provenance_and_scannable_packages(tmp_path, monkeypatch):
    config = config_file(tmp_path)
    source = tmp_path / "download"
    source.mkdir()
    (source / "go.mod").write_text(f"module {INVENTORY.MODULE}\n", encoding="utf-8")
    (source / "go.sum").write_text("checksums\n", encoding="utf-8")
    calls = []

    def go_command(go, directory, *args, target=None):
        calls.append(args)
        if args[0] == "mod":
            return json.dumps(
                {
                    "Path": INVENTORY.MODULE,
                    "Version": "v1.7.12",
                    "Origin": {"Hash": SHA, "URL": INVENTORY.REPOSITORY},
                    "Dir": str(source),
                    "Sum": "h1:module",
                    "GoModSum": "h1:mod",
                }
            )
        if args[0] == "list":
            if "-m" in args:
                return "\n".join(
                    json.dumps(m)
                    for m in [
                        ROOT_MODULE,
                        SYS_MODULE,
                        {"Path": "example.com/unused", "Version": "v1.0.0"},
                    ]
                )
            records = [{"ImportPath": f"{INVENTORY.MODULE}/cmd/actionlint", "Module": ROOT_MODULE}]
            if target[0] == "windows":
                records.append({"ImportPath": "golang.org/x/sys/windows", "Module": SYS_MODULE})
            return "\n".join(json.dumps(record) for record in records)
        return "go version go1.27.4 linux/amd64\n"

    monkeypatch.setattr(INVENTORY, "run_go", go_command)
    output = tmp_path / "result"
    INVENTORY.generate(config, output, **assessment_files(tmp_path))
    assert calls[:2] == [
        ("mod", "download", "-json", f"{INVENTORY.MODULE}@{SHA}"),
        ("list", "-mod=readonly", "-m", "-json", "all"),
    ]
    assert (output / "go.mod").read_bytes() == (source / "go.mod").read_bytes()
    assert json.loads((output / "provenance.json").read_text())["revision"] == SHA
    package = json.loads((output / "inventory.json").read_text())["results"][0]["packages"][0]
    assert package["package"]["version"] == "1.7.12"
    scanned = json.loads((output / "inventory.json").read_text())["results"][0]["packages"]
    assert len(scanned) == 2  # Windows-only imports remain in the union; unused modules do not.
    assert len(json.loads((output / "full-graph.json").read_text())["results"][0]["packages"]) == 3
    assert len(json.loads((output / "imports.json").read_text())) == 12
    effective = tomllib.loads((output / "osv-scanner.toml").read_text())
    assert effective["IgnoredVulns"][0]["id"] == "GO-2026-5024"


def test_failure_clears_stale_inventory(tmp_path, monkeypatch):
    output = tmp_path / "result"
    output.mkdir()
    for name in INVENTORY.OUTPUT_FILES:
        (output / name).write_text("stale", encoding="utf-8")

    def fail(*args):
        raise subprocess.CalledProcessError(1, ["go"])

    monkeypatch.setattr(INVENTORY, "run_go", fail)
    with pytest.raises(subprocess.CalledProcessError):
        INVENTORY.generate(config_file(tmp_path), output)
    assert list(output.iterdir()) == []


def test_graph_resolution_must_not_modify_upstream_inputs(tmp_path, monkeypatch):
    source = tmp_path / "download"
    source.mkdir()
    for name in ("go.mod", "go.sum"):
        (source / name).write_text("original\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def modifies_input(go, directory, *args):
        (directory / "go.sum").write_text("changed\n", encoding="utf-8")
        return json.dumps({"Path": INVENTORY.MODULE, "Main": True})

    monkeypatch.setattr(INVENTORY, "run_go", modifies_input)
    with pytest.raises(ValueError, match="changed the pinned module inputs"):
        INVENTORY.resolve_graph("go", workspace, {"Dir": str(source), "Version": "v1.7.12"})


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"Standard": True}],
        [{"Error": {"Err": "missing"}}],
        [{"DepsErrors": [{"Err": "dependency missing"}]}],
        [{"Incomplete": True}],
    ],
)
def test_incomplete_executable_listing_fails(records):
    with pytest.raises(ValueError):
        INVENTORY.imported_modules(records, "v1.7.12")


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": "b" * 40},
        {"version": "0.44.0"},
        {"expires": "2000-01-01T00:00:00Z"},
        {"expires": '"not-a-date"'},
    ],
)
def test_assessment_requires_reviewed_revision_version_and_valid_expiry(tmp_path, changes):
    files = assessment_files(tmp_path, **changes)
    packages = INVENTORY.graph_packages([ROOT_MODULE, SYS_MODULE], "v1.7.12")
    with pytest.raises((ValueError, TypeError)):
        INVENTORY.assessed_policy(**files, revision=SHA, packages=packages)


def test_missing_assessed_module_fails(tmp_path):
    with pytest.raises(ValueError, match="reassess"):
        INVENTORY.assessed_policy(**assessment_files(tmp_path), revision=SHA, packages=[])


def test_failed_assessment_clears_stale_policy_and_inventory(tmp_path, monkeypatch):
    output = tmp_path / "result"
    output.mkdir()
    for name in INVENTORY.OUTPUT_FILES:
        (output / name).write_text("stale")
    packages = INVENTORY.graph_packages([ROOT_MODULE, SYS_MODULE], "v1.7.12")
    monkeypatch.setattr(INVENTORY, "resolve_root", lambda *args: {"Version": "v1.7.12"})
    monkeypatch.setattr(INVENTORY, "resolve_graph", lambda *args: (packages, {}))
    monkeypatch.setattr(INVENTORY, "resolve_executable", lambda *args: (packages, {}))
    with pytest.raises(ValueError, match="reassess"):
        INVENTORY.generate(
            config_file(tmp_path), output, **assessment_files(tmp_path, revision="b" * 40)
        )
    assert list(output.iterdir()) == []


def test_contradictory_module_versions_fail():
    records = [
        {"ImportPath": "golang.org/x/sys/windows", "Module": SYS_MODULE},
        {"ImportPath": "golang.org/x/sys/execabs", "Module": {**SYS_MODULE, "Version": "v0.43.0"}},
    ]
    with pytest.raises(ValueError, match="contradictory"):
        INVENTORY.imported_modules(records, "v1.7.12")


def test_effective_policy_preserves_root_policy_and_only_ignores_one_advisory(tmp_path):
    files = assessment_files(tmp_path)
    packages = INVENTORY.graph_packages([ROOT_MODULE, SYS_MODULE], "v1.7.12")
    effective = tomllib.loads(INVENTORY.assessed_policy(**files, revision=SHA, packages=packages))
    original = tomllib.loads(files["policy"].read_text())
    assert effective["PackageOverrides"] == original["PackageOverrides"]
    assert len(effective["IgnoredVulns"]) == 1
    assert effective["IgnoredVulns"][0]["id"] == "GO-2026-5024"
    assert "vulnerability" not in effective["PackageOverrides"][0]


def test_executable_module_mismatch_fails(tmp_path, monkeypatch):
    source = tmp_path / "download"
    source.mkdir()
    for name in ("go.mod", "go.sum"):
        (source / name).write_text("original\n")
    records = [
        {"ImportPath": f"{INVENTORY.MODULE}/cmd/actionlint", "Module": ROOT_MODULE},
        {"ImportPath": "golang.org/x/sys/windows", "Module": {**SYS_MODULE, "Version": "v0.43.0"}},
    ]
    monkeypatch.setattr(INVENTORY, "run_go", lambda *a, **kw: "\n".join(map(json.dumps, records)))
    full_graph = INVENTORY.graph_packages([ROOT_MODULE, SYS_MODULE], "v1.7.12")
    with pytest.raises(ValueError, match="differ"):
        INVENTORY.resolve_executable(
            "go", tmp_path / "work", {"Dir": str(source), "Version": "v1.7.12"}, full_graph
        )
