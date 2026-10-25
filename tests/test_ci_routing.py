"""Exercise the hosted fallback's event guard with representative GitHub contexts."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())


def namespace(**values):
    return SimpleNamespace(**values)


def hosted_job_runs(event, head_repository="aditzel/hermes-muninndb-plugin", base="main"):
    if event not in WORKFLOW["on"]:
        return False
    github = namespace(
        event_name=event,
        repository="aditzel/hermes-muninndb-plugin",
        event=namespace(pull_request=namespace(
            head=namespace(repo=namespace(full_name=head_repository)),
            base=namespace(ref=base),
        )),
    )
    expression = WORKFLOW["jobs"]["test"]["if"].replace("&&", " and ").replace("||", " or ")
    tree = ast.parse(" ".join(expression.split()), mode="eval")
    # This fixture intentionally supports only the boolean/equality expression
    # used for routing, not arbitrary Python execution or general Actions syntax.
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq,
               ast.NotEq, ast.Attribute, ast.Name, ast.Load, ast.Constant)
    assert all(isinstance(node, allowed) for node in ast.walk(tree))
    assert all(not isinstance(node, ast.Attribute) or not node.attr.startswith("_")
               for node in ast.walk(tree))
    return eval(compile(tree, "ci.yml job guard", "eval"), {"__builtins__": {}}, {"github": github})


@pytest.mark.parametrize("base", ["main", "release/maintenance", "ci/stack-parent"])
def test_same_repository_prs_use_buildkite_and_forks_keep_hosted_checks(base):
    assert not hosted_job_runs("pull_request", base=base)
    assert hosted_job_runs("pull_request", "contributor/hermes-muninndb-plugin", base)
    assert WORKFLOW["on"]["pull_request"] is None  # All base branches remain covered.


def test_manual_recovery_runs_and_automatic_push_has_no_hosted_duplicate():
    assert hosted_job_runs("workflow_dispatch")
    assert not hosted_job_runs("push")
    assert not hosted_job_runs("release")
    assert not hosted_job_runs("pull_request_target")
    assert set(WORKFLOW["on"]) == {"pull_request", "workflow_dispatch"}


def test_hosted_recovery_keeps_both_independent_read_only_lanes_and_stages():
    job = WORKFLOW["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["strategy"] == {"fail-fast": False, "matrix": {"python-version": ["3.11", "3.12"]}}
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert "permissions" not in job and "environment" not in job
    steps = job["steps"]
    assert steps[0]["with"]["persist-credentials"] is False
    assert steps[1]["with"]["python-version"] == "${{ matrix.python-version }}"
    commands = [step["run"] for step in steps if "run" in step]
    assert "python -m pip install -e .[dev]" in commands[0]
    assert commands[1] == "python -m pytest"
    assert commands[2].startswith("python -m py_compile ")
    assert commands[3] == "python -m build"
    assert "--no-activate --json" in commands[4]
    assert "spec.loader.exec_module(module)" in commands[4]
