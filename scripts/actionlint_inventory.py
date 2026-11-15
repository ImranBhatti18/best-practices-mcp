"""Resolve the pinned hook's Go module graph for CI, without building upstream code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import yaml

MODULE = "github.com/rhysd/actionlint"
REPOSITORY = f"https://{MODULE}"
TARGETS = tuple(
    (os_name, arch, cgo)
    for os_name in ("linux", "darwin", "windows")
    for arch in ("amd64", "arm64")
    for cgo in ("0", "1")
)
OUTPUT_FILES = (
    "inventory.json",
    "full-graph.json",
    "imports.json",
    "provenance.json",
    "go.mod",
    "go.sum",
    "assessment.toml",
    "osv-scanner.toml",
)


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


def run_go(
    go: str, directory: Path, *arguments: str, target: tuple[str, str, str] | None = None
) -> str:
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
    if target is not None:
        env.update(GOOS=target[0], GOARCH=target[1], CGO_ENABLED=target[2])
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


def imported_modules(records: list[dict], root_version: str) -> tuple[dict, list[str]]:
    modules = {}
    imports = []
    for record in records:
        if record.get("Error") or record.get("DepsErrors") or record.get("Incomplete"):
            raise ValueError("Executable dependency listing is incomplete")
        if record.get("Standard"):
            continue
        package = module_package(record["Module"], root_version)
        if package["name"] in modules and modules[package["name"]]["package"] != package:
            raise ValueError("Executable dependency listing contains contradictory module versions")
        modules[package["name"]] = {"package": package}
        imports.append(record["ImportPath"])
    if f"{MODULE}/cmd/actionlint" not in imports:
        raise ValueError("Executable dependency listing does not include cmd/actionlint")
    return modules, sorted(imports)


def resolve_executable(
    go: str, directory: Path, root: dict, full_graph: list[dict]
) -> tuple[list[dict], dict]:
    source = directory / "source"
    shutil.copytree(root["Dir"], source)
    original = {name: (source / name).read_bytes() for name in ("go.mod", "go.sum")}
    expected = {p["package"]["name"]: p for p in full_graph}
    selected = {}
    imports = {}
    for target in TARGETS:
        records = decode_graph(
            run_go(
                go,
                source,
                "list",
                "-mod=readonly",
                "-deps",
                "-json",
                "./cmd/actionlint",
                target=target,
            )
        )
        label = f"{target[0]}/{target[1]}/cgo={target[2]}"
        modules, imports[label] = imported_modules(records, root["Version"])
        if any(expected.get(name) != package for name, package in modules.items()):
            raise ValueError("Executable modules differ from the resolved module graph")
        selected.update(modules)
    if any((source / name).read_bytes() != content for name, content in original.items()):
        raise ValueError("Executable dependency listing changed the pinned module inputs")
    return [selected[name] for name in sorted(selected)], imports


def assessed_policy(policy: Path, assessment: Path, revision: str, packages: list[dict]) -> str:
    reviewed = tomllib.loads(assessment.read_text(encoding="utf-8"))
    versions = {p["package"]["name"]: p["package"]["version"] for p in packages}
    if reviewed["revision"] != revision or versions.get(reviewed["module"]) != reviewed["version"]:
        raise ValueError(
            "Actionlint or assessed dependency changed; reassess the advisory exception"
        )
    expires = reviewed["effective_until"]
    if expires <= datetime.now(UTC):
        raise ValueError("Actionlint advisory assessment expired; reassessment is required")
    base = policy.read_text(encoding="utf-8")
    tomllib.loads(base)
    return (
        base + "\n# Generated only after checking the reviewed actionlint revision and module.\n"
        "[[IgnoredVulns]]\n"
        f"id = {json.dumps(reviewed['advisory'])}\n"
        f"effectiveUntil = {expires.isoformat()}\n"
        f"reason = {json.dumps(reviewed['reason'])}\n"
    )


def generate(
    config: Path,
    output: Path,
    go: str = "go",
    *,
    policy: Path = Path("osv-scanner.toml"),
    assessment: Path = Path("actionlint-assessment.toml"),
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    # Never let an unsuccessful rerun leave a stale successful inventory behind.
    for name in OUTPUT_FILES:
        (output / name).unlink(missing_ok=True)
    revision = read_pin(config)
    with tempfile.TemporaryDirectory(prefix="actionlint-inventory-") as temporary:
        directory = Path(temporary)
        root = resolve_root(go, directory, revision)
        full_graph, inputs = resolve_graph(go, directory, root)
        packages, imports = resolve_executable(go, directory, root, full_graph)
        effective_policy = assessed_policy(policy, assessment, revision, packages)
        provenance = {
            "repository": REPOSITORY,
            "revision": revision,
            "version": root["Version"],
            "go_version": run_go(go, directory, "version").strip(),
            "module_sum": root["Sum"],
            "go_mod_sum": root["GoModSum"],
            "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "policy_sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
            "assessment_sha256": hashlib.sha256(assessment.read_bytes()).hexdigest(),
            "entrypoint": "./cmd/actionlint",
            "targets": list(imports),
            "input_sha256": {
                name: hashlib.sha256(content).hexdigest() for name, content in inputs.items()
            },
        }
    for name, content in inputs.items():
        (output / name).write_bytes(content)
    (output / "assessment.toml").write_bytes(assessment.read_bytes())
    (output / "osv-scanner.toml").write_text(effective_policy, encoding="utf-8")
    (output / "full-graph.json").write_text(
        json.dumps({"results": [{"packages": full_graph}]}, indent=2) + "\n", encoding="utf-8"
    )
    (output / "imports.json").write_text(json.dumps(imports, indent=2) + "\n", encoding="utf-8")
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
    parser.add_argument("--policy", type=Path, default=Path("osv-scanner.toml"))
    parser.add_argument("--assessment", type=Path, default=Path("actionlint-assessment.toml"))
    args = parser.parse_args()
    generate(args.config, args.output, args.go, policy=args.policy, assessment=args.assessment)


if __name__ == "__main__":
    main()
