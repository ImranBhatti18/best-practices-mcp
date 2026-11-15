"""Resolve the pinned hook's Go module graph for CI, without building upstream code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

MODULE = "github.com/rhysd/actionlint"
REPOSITORY = f"https://{MODULE}"
OUTPUT_FILES = ("inventory.json", "provenance.json", "go.mod", "go.sum")


def read_pin(config: Path) -> str:
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    repositories = [
        repo
        for repo in document["repos"]
        if any(hook["id"] == "actionlint" for hook in repo["hooks"])
    ]
    if len(repositories) != 1 or repositories[0]["repo"] != REPOSITORY:
        raise ValueError("Expected exactly one official actionlint hook repository")
    revision = repositories[0]["rev"]
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("actionlint must be pinned to a full commit SHA")
    return revision


def run_go(go: str, directory: Path, *arguments: str) -> str:
    env = {
        **os.environ,
        "GOENV": "off",
        "GO111MODULE": "on",
        "GOWORK": "off",
        "GOFLAGS": "",
        "GOTOOLCHAIN": "local",
        "GOPROXY": "https://proxy.golang.org",
        "GOSUMDB": "sum.golang.org",
        "GOPRIVATE": "",
        "GONOPROXY": "",
        "GONOSUMDB": "",
    }
    try:
        return subprocess.run(
            [go, *arguments],
            cwd=directory,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"Go metadata resolution failed: {error.stderr or error.stdout}"
        ) from error


def resolve_root(go: str, directory: Path, revision: str) -> dict:
    root = json.loads(run_go(go, directory, "mod", "download", "-json", f"{MODULE}@{revision}"))
    if root.get("Error") or root.get("Path") != MODULE or not root.get("Version"):
        raise ValueError("Could not resolve actionlint's module version")
    origin = root.get("Origin", {})
    if origin.get("Hash") != revision or origin.get("URL") != REPOSITORY:
        raise ValueError("Resolved actionlint source does not match the pinned repository and SHA")
    return root


def decode_graph(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    modules = []
    remaining = text.lstrip()
    while remaining:
        module, end = decoder.raw_decode(remaining)
        modules.append(module)
        remaining = remaining[end:].lstrip()
    return modules


def module_package(module: dict, root_version: str) -> dict:
    if module.get("Error") or module.get("Replace"):
        raise ValueError("Unresolved or replaced Go modules require explicit inventory support")
    name = module["Path"]
    version = root_version if module.get("Main") and name == MODULE else module.get("Version")
    if not version:
        raise ValueError("Go graph contains an unversioned module")
    # Match OSV's native Go extractor: its license enricher adds the leading v.
    return {"name": name, "version": version.removeprefix("v"), "ecosystem": "Go"}


def graph_packages(modules: list[dict], version: str) -> list[dict]:
    packages = {}
    for module in modules:
        package = module_package(module, version)
        if package["name"] in packages:
            raise ValueError("Go graph contains a duplicate module")
        packages[package["name"]] = {"package": package}
    root = packages.get(MODULE, {}).get("package", {})
    if root.get("version") != version.removeprefix("v"):
        raise ValueError("Go graph must include the pinned actionlint root module")
    return [packages[name] for name in sorted(packages)]


def resolve_graph(go: str, directory: Path, root: dict) -> tuple[list[dict], dict[str, bytes]]:
    source = Path(root["Dir"])
    inputs = {name: (source / name).read_bytes() for name in ("go.mod", "go.sum")}
    for name, content in inputs.items():
        (directory / name).write_bytes(content)
    graph = decode_graph(run_go(go, directory, "list", "-mod=readonly", "-m", "-json", "all"))
    if any((directory / name).read_bytes() != content for name, content in inputs.items()):
        raise ValueError("Resolving the module graph changed the pinned module inputs")
    return graph_packages(graph, root["Version"]), inputs


def generate(config: Path, output: Path, go: str = "go") -> None:
    output.mkdir(parents=True, exist_ok=True)
    # Never let an unsuccessful rerun leave a stale successful inventory behind.
    for name in OUTPUT_FILES:
        (output / name).unlink(missing_ok=True)
    revision = read_pin(config)
    with tempfile.TemporaryDirectory(prefix="actionlint-inventory-") as temporary:
        directory = Path(temporary)
        root = resolve_root(go, directory, revision)
        packages, inputs = resolve_graph(go, directory, root)
        provenance = {
            "repository": REPOSITORY,
            "revision": revision,
            "version": root["Version"],
            "go_version": run_go(go, directory, "version").strip(),
            "module_sum": root["Sum"],
            "go_mod_sum": root["GoModSum"],
            "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "input_sha256": {
                name: hashlib.sha256(content).hexdigest() for name, content in inputs.items()
            },
        }
    for name, content in inputs.items():
        (output / name).write_bytes(content)
    (output / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    inventory = {"results": [{"packages": packages}]}
    (output / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    print(f"Inventoried actionlint {root['Version']} at {revision}: {len(packages)} Go modules")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(".pre-commit-config.yaml"))
    parser.add_argument("--output", type=Path, default=Path("build/actionlint-inventory"))
    parser.add_argument("--go", default="go")
    args = parser.parse_args()
    generate(args.config, args.output, args.go)


if __name__ == "__main__":
    main()
