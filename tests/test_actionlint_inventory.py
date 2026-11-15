"""Fail-closed inventory generation without network or Go in ordinary tests."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "actionlint_inventory", Path(__file__).resolve().parents[1] / "scripts/actionlint_inventory.py"
)
assert SPEC and SPEC.loader
INVENTORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INVENTORY)
SHA = "a" * 40


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

    def go_command(go, directory, *args):
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
            return json.dumps({"Path": INVENTORY.MODULE, "Main": True})
        return "go version go1.27.4 linux/amd64\n"

    monkeypatch.setattr(INVENTORY, "run_go", go_command)
    output = tmp_path / "result"
    INVENTORY.generate(config, output)
    assert calls[:2] == [
        ("mod", "download", "-json", f"{INVENTORY.MODULE}@{SHA}"),
        ("list", "-mod=readonly", "-m", "-json", "all"),
    ]
    assert (output / "go.mod").read_bytes() == (source / "go.mod").read_bytes()
    assert json.loads((output / "provenance.json").read_text())["revision"] == SHA
    package = json.loads((output / "inventory.json").read_text())["results"][0]["packages"][0]
    assert package["package"]["version"] == "1.7.12"


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
