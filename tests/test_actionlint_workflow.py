"""Exercise the inline CI reporter with untrusted diagnostics and tool failures."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/actionlint.yml"
DOCUMENT = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
STEPS = DOCUMENT["jobs"]["actionlint"]["steps"]
REPORTER = next(
    step["run"] for step in STEPS if step["name"] == "Lint workflows and report safe annotations"
)
SOURCE = REPORTER.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


@pytest.fixture
def reporter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    monkeypatch.setenv("ACTIONLINT_REPORTS", str(tmp_path / "reports"))
    monkeypatch.setenv("ACTIONLINT_BINARY", "/fixed/actionlint")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    namespace = {"__name__": "actionlint_reporter"}
    exec(compile(SOURCE, str(WORKFLOW), "exec"), namespace)
    return namespace


def finding(**changes):
    return {
        "filepath": ".github/workflows/ci.yml",
        "line": 3,
        "column": 7,
        "message": "unknown job",
        "kind": "job-needs",
        **changes,
    }


def mock_linter(monkeypatch, output, code=1, stderr=""):
    def run(command, *, stdout, stderr, timeout, check):
        assert command[:6] == [
            "/fixed/actionlint",
            "-no-color",
            "-shellcheck=",
            "-pyflakes=",
            "-format",
            "{{json .}}",
        ]
        assert command[6:] == [".github/workflows/ci.yml"]
        stdout.write(output.encode())
        stderr.write(error_output.encode())
        return subprocess.CompletedProcess(command, code)

    error_output = stderr
    monkeypatch.setattr(subprocess, "run", run)


def status(tmp_path):
    return json.loads((tmp_path / "reports/status.json").read_text())


def test_annotations_escape_untrusted_fields(reporter, monkeypatch, tmp_path, capsys):
    message = "bad %0A text\r\n::add-mask::injected"
    kind = "bad,:\r\n::error::injected"
    mock_linter(monkeypatch, json.dumps([finding(message=message, kind=kind)]))
    reporter["main"]()
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert "bad %250A text%0D%0A::add-mask::injected" in output
    assert "title=actionlint%3A bad%2C%3A%0D%0A%3A%3Aerror%3A%3Ainjected" in output
    assert "file=.github/workflows/ci.yml,line=3,col=7" in output
    assert status(tmp_path)["exit_code"] == 1
    assert "injected" not in (tmp_path / "summary.md").read_text()


@pytest.mark.parametrize("output", ["null\n", "[]\n"])
def test_clean_scan_passes(reporter, monkeypatch, tmp_path, capsys, output):
    mock_linter(monkeypatch, output, code=0)
    reporter["main"]()
    assert capsys.readouterr().out == ""
    assert status(tmp_path) == {"exit_code": 0, "findings": 0, "report_valid": True}


@pytest.mark.parametrize(
    "output",
    [
        "",
        "::error::not json",
        "{}",
        "[null]",
        json.dumps([finding(filepath="../outside.yml")]),
        json.dumps([finding(filepath=[".github/workflows/ci.yml"])]),
        json.dumps([finding(line=True)]),
        json.dumps([finding(column=0)]),
        json.dumps([finding(message={})]),
    ],
)
def test_malformed_reports_fail_closed(reporter, monkeypatch, tmp_path, capsys, output):
    mock_linter(monkeypatch, output, code=0)
    reporter["main"]()
    assert status(tmp_path)["exit_code"] == 1
    assert status(tmp_path)["report_valid"] is False
    assert capsys.readouterr().out.startswith("::error::actionlint could not complete")
    assert (tmp_path / "reports/findings.json").read_text() == output


def test_fatal_tool_status_survives_empty_findings(reporter, monkeypatch, tmp_path, capsys):
    mock_linter(monkeypatch, "null", code=3, stderr="::add-mask::do-not-print\n")
    reporter["main"]()
    assert status(tmp_path)["exit_code"] == 3
    assert "do-not-print" not in capsys.readouterr().out
    assert (tmp_path / "reports/stderr.log").read_text() == "::add-mask::do-not-print\n"


def test_findings_fail_even_if_tool_returns_success(reporter, monkeypatch, tmp_path):
    mock_linter(monkeypatch, json.dumps([finding()]), code=0)
    reporter["main"]()
    assert status(tmp_path)["exit_code"] == 1


def test_annotation_limit_does_not_truncate_report(reporter, monkeypatch, tmp_path, capsys):
    mock_linter(monkeypatch, json.dumps([finding()] * 55))
    reporter["main"]()
    assert len(capsys.readouterr().out.splitlines()) == 50
    assert status(tmp_path)["findings"] == 55
    assert len(json.loads((tmp_path / "reports/findings.json").read_text())) == 55


def test_timeout_fails_and_retains_partial_output(reporter, monkeypatch, tmp_path, capsys):
    def timeout(command, *, stdout, **kwargs):
        stdout.write(b"partial")
        raise subprocess.TimeoutExpired(command, 180)

    monkeypatch.setattr(subprocess, "run", timeout)
    reporter["main"]()
    assert status(tmp_path)["exit_code"] == 1
    assert (tmp_path / "reports/findings.json").read_text() == "partial"
    assert capsys.readouterr().out.startswith("::error::actionlint could not complete")


def test_symlinked_workflow_is_rejected(reporter, tmp_path):
    path = tmp_path / ".github/workflows/linked.yml"
    try:
        path.symlink_to("ci.yml")
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(ValueError, match="regular workflow"):
        reporter["workflow_files"]()


def test_workflow_retains_reports_before_enforcing_status():
    names = [step["name"] for step in STEPS]
    assert names.index("Retain workflow lint reports") < names.index("Enforce lint result")
    assert DOCUMENT["permissions"] == {"contents": "read"}
    assert DOCUMENT["jobs"]["actionlint"]["permissions"] == {"contents": "read"}
    checkout = STEPS[0]["with"]
    assert checkout["fetch-depth"] == 1
    assert checkout["persist-credentials"] is False
    assert checkout["sparse-checkout-cone-mode"] is False
    assert checkout["sparse-checkout"].strip() == "/.github/workflows/*"


@pytest.mark.parametrize("tool_status,expected", [(0, 0), (1, 1), (3, 1), (-9, 1)])
def test_final_step_preserves_failure_after_reporting(tmp_path, monkeypatch, tool_status, expected):
    (tmp_path / "status.json").write_text(json.dumps({"exit_code": tool_status}))
    monkeypatch.setenv("ACTIONLINT_REPORTS", str(tmp_path))
    step = next(step["run"] for step in STEPS if step["name"] == "Enforce lint result")
    source = step.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    with pytest.raises(SystemExit) as exited:
        exec(compile(source, str(WORKFLOW), "exec"), {})
    assert exited.value.code == expected
