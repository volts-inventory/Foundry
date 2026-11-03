"""The Python install step: declared deps go into a per-build venv, never Foundry's.

Offline throughout: dependencies are wheels built in ``tmp_path`` and named by
``file://`` URL, and the "bad dependency" cases pin ``--no-index``.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata as im
import importlib.util
import json
import site
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

import foundry.gates.pydeps as pydeps
from foundry.agents._common import emit_artifact
from foundry.gates.attestation import ZoneBSigner
from foundry.gates.build import BuildGate
from foundry.gates.profiles import PROFILES, stack_profile
from foundry.gates.pydeps import (
    SCAFFOLD_HEADER,
    declared_requirements,
    install_failure_is_project,
    python_build_env,
)
from foundry.gates.runner import TwoZoneGateRunner
from foundry.gates.test import BDD_KEY, TestGate
from foundry.graph.gate_wiring import (
    CRITERION_KIND,
    FEATURE_DIFF_KIND,
    materialize_workdir,
    run_build_test_gates,
)
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import FeatureDiff, StackDecision, scenario_ref
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq


def _seq(node: str, local: int = 0) -> Seq:
    return Seq(run_id="run-1", emission_epoch=1, superstep_index=0, node_id=node, local=local)


def _wheel(dest: Path, dist: str, files: dict[str, str], version: str = "0.1") -> Path:
    """A minimal pure-Python wheel built offline."""
    info = f"{dist.replace('-', '_')}-{version}.dist-info"
    files = {
        **files,
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n",
    }
    record = []
    for path, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=")
        record.append(f"{path},sha256={digest.decode()},{len(text.encode())}")
    files[f"{info}/RECORD"] = "\n".join([*record, f"{info}/RECORD,,"]) + "\n"
    dest.mkdir(parents=True, exist_ok=True)
    whl = dest / f"{dist.replace('-', '_')}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        for path, text in files.items():
            zf.writestr(path, text)
    return whl


@pytest.fixture()
def tinydep(tmp_path: Path) -> str:
    whl = _wheel(tmp_path / "wheels", "tinydep", {"tinydep/__init__.py": "VALUE = 42\n"})
    return f"tinydep @ {whl.as_uri()}"


@pytest.fixture()
def allow_install(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite runs the gates in-process (conftest), where the networked install
    # step is refused unless explicitly accepted.
    monkeypatch.setenv("FOUNDRY_ALLOW_UNSANDBOXED_INSTALL", "1")


def _foundry_has_no(module: str) -> None:
    assert importlib.util.find_spec(module) is None
    for root in site.getsitepackages():
        assert not list(Path(root).glob(f"{module}*"))


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


# ---- what counts as a declared dependency ----------------------------------------


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({}, ()),
        ({"requirements.txt": "# nothing yet\n\n"}, ()),
        ({"requirements.txt": SCAFFOLD_HEADER + "foundry-absent-pkg\n"}, ()),
        ({"requirements.txt": f"pytest=={im.version('pytest')}\npytest-bdd  # ours\n"}, ()),
        ({"requirements.txt": "pytest==0.0.1\n"}, ("-r", "requirements.txt")),
        ({"requirements.txt": "foundry-absent-pkg\n"}, ("-r", "requirements.txt")),
        ({"pyproject.toml": '[project]\nname = "x"\ndependencies = ["pytest"]\n'}, ()),
        (
            {"pyproject.toml": '[project]\nname = "x"\ndependencies = ["flask>=3"]\n'},
            ("flask>=3",),
        ),
    ],
)
def test_declared_requirements(tmp_path: Path, files: dict[str, str], expected: tuple) -> None:
    assert declared_requirements(_write(tmp_path, files)) == expected


def test_install_failure_classification() -> None:
    no_match = (
        "ERROR: Could not find a version that satisfies the requirement nope (from "
        "versions: none)\nERROR: No matching distribution found for nope\n"
    )
    assert install_failure_is_project(no_match)
    assert install_failure_is_project("ERROR: Invalid requirement: 'a ==='\n")
    assert install_failure_is_project("ERROR: ResolutionImpossible: for help visit ...\n")
    # The same words after pip's retries ran out are the network's fault.
    offline = (
        "WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, "
        "status=None)) after connection broken by 'NewConnectionError(...)': /simple/nope/\n"
    ) + no_match
    assert not install_failure_is_project(offline)
    assert not install_failure_is_project("ERROR: HTTP error 503 while getting ...\n")
    assert not install_failure_is_project("Failed to build lxml\n")


# ---- the gates over a per-build venv ---------------------------------------------


def _runner() -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(b"k" * 32, "kid"), GlobalRecords(), "rev", "rev")


def _state(files: dict[str, str], criteria: tuple[AcceptanceCriterion, ...] = ()):
    artifacts: list[Artifact] = []
    by_uri: dict[str, Any] = {}
    for c in criteria:
        h = emit_artifact(c, CRITERION_KIND, "planner", c.seq)
        artifacts.append(h)
        by_uri[h.uri] = c
    diff = FeatureDiff(diff_uri="cas://sha256/d1", task_id="T1", branch="task/T1",
                       base_tree_sha="base", seq=_seq("junior_engineer"))
    h = Artifact(kind=FEATURE_DIFF_KIND, uri="cas://fd/T1", produced_by="junior_engineer",
                 task_id="T1", seq=diff.seq)
    artifacts.append(h)
    by_uri[h.uri] = diff
    body = json.dumps({"kind": "full-file-snapshot", "files": files}).encode()
    return {"artifacts": artifacts}, (lambda a: by_uri[a.uri]), (lambda u: body)


class _Rt:
    def seq(self, config: Any, node: str, key: str) -> Seq:
        return _seq(node)


_APP = {
    "app.py": "import tinydep\n\nVALUE = tinydep.VALUE\n",
    # Imported inside the test: a collection error is a harness ERROR, not a FAIL.
    "test_app.py": "def test_value():\n    from app import VALUE\n\n    assert VALUE == 42\n",
    "smoke.py": "import app\n\nassert app.VALUE == 42\n",
}


def _gates(tmp_path: Path, files: dict[str, str]) -> dict[str, GateStatus]:
    state, resolve, blob = _state(files)
    out = run_build_test_gates(
        state, {"configurable": {}}, rt=_Rt(), runner=_runner(),  # type: ignore[arg-type]
        resolve=resolve, resolve_blob=blob, workdir=tmp_path / "wd",
        profile=PROFILES["python-app"],
    )
    return {g.name: g for g in out["gates"]}


def test_declared_dependency_installs_into_the_build_venv_only(
    tmp_path: Path, tinydep: str, allow_install: None
) -> None:
    gates = _gates(tmp_path, {**_APP, "requirements.txt": f"{tinydep}\n"})
    assert {n: g.status for n, g in gates.items()} == {
        "build": GateStatus.PASS, "test": GateStatus.PASS, "smoke": GateStatus.PASS,
    }, [f for g in gates.values() for f in g.findings]
    _foundry_has_no("tinydep")


def test_without_the_dependency_the_same_tree_fails(tmp_path: Path) -> None:
    gates = _gates(tmp_path, _APP)  # no requirements.txt: nothing installed
    assert gates["build"].status is GateStatus.PASS
    assert gates["test"].status is GateStatus.FAIL
    assert gates["smoke"].status is GateStatus.FAIL


def test_unknown_dependency_is_a_build_fail(tmp_path: Path, allow_install: None) -> None:
    reqs = "--no-index\nfoundry-no-such-package-xyz\n"
    gates = _gates(tmp_path, {**_APP, "requirements.txt": reqs})
    build = gates["build"]
    assert build.status is GateStatus.FAIL
    assert "cannot be installed — FAIL" in build.findings[0]
    assert any("foundry-no-such-package-xyz" in f for f in build.findings)
    # The later gates still judge the tree as it is.
    assert gates["test"].status is GateStatus.FAIL


def test_network_failure_is_an_error_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _offline(*_a: Any, **_k: Any) -> tuple[int, str, str]:
        return 1, "", (
            "WARNING: Retrying (Retry(total=0)) after connection broken by "
            "'NewConnectionError(...)'\nERROR: No matching distribution found for flask\n"
        )

    monkeypatch.setattr(pydeps, "ground_untrusted", _offline)
    gates = _gates(tmp_path, {**_APP, "requirements.txt": "flask\n"})
    assert {n: g.status for n, g in gates.items()} == {
        "build": GateStatus.ERROR, "test": GateStatus.ERROR, "smoke": GateStatus.ERROR,
    }


def test_no_declared_dependency_skips_the_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_venv(*_a: Any, **_k: Any) -> None:
        raise AssertionError("no venv without a declared dependency")

    monkeypatch.setattr(pydeps, "build_python_env", _no_venv)
    files = {"app.py": "VALUE = 42\n", "test_app.py": _APP["test_app.py"],
             "smoke.py": "import app\n", "requirements.txt": "pytest\n"}
    gates = _gates(tmp_path, files)
    assert all(g.status is GateStatus.PASS for g in gates.values())


def test_non_python_profile_never_gets_a_venv(tmp_path: Path) -> None:
    node = stack_profile(StackDecision(language="javascript", test="npm test"))
    assert not node.python
    with python_build_env(_write(tmp_path, {"requirements.txt": "flask\n"}),
                          python=node.python) as env:
        assert env is None


def test_a_dependency_cannot_shadow_the_pinned_harness(
    tmp_path: Path, tinydep: str, allow_install: None
) -> None:
    # A dependency shipping a `pytest` that reports a green run: the project's code
    # may import it, the gate's harness never does, so the red suite stays red.
    fake = "def console_main():\n    print('1 passed in 0.01s')\n    return 0\n"
    evil = _wheel(tmp_path / "wheels", "evil-harness", {
        "pytest/__init__.py": fake, "pytest_bdd/__init__.py": "",
    })
    files = {**_APP, "test_app.py": "def test_red():\n    assert False\n",
             "requirements.txt": f"{tinydep}\nevil-harness @ {evil.as_uri()}\n"}
    gates = _gates(tmp_path, files)
    assert gates["build"].status is GateStatus.PASS
    assert gates["test"].status is GateStatus.FAIL


_STEPS = '''\
import tinydep
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../features/T1.feature")


@given("an empty list", target_fixture="items")
def _empty():
    return []


@when(parsers.parse('I add "{x}"'))
def _add(items, x):
    items.append(x)


@then(parsers.parse("the list has {n:d} item"))
def _count(items, n):
    assert len(items) == n and tinydep.VALUE == 42
'''


def test_bdd_harness_runs_scenarios_against_installed_deps(
    tmp_path: Path, tinydep: str, allow_install: None
) -> None:
    criterion = AcceptanceCriterion(
        id="AC-1", text="Given … When … Then …", ticket_id="T1",
        oracle_hint=scenario_ref("T1", "AC-1"), seq=_seq("planner"),
        given=("an empty list",), when=('I add "milk"',), then=("the list has 1 item",),
    )
    state, resolve, blob = _state(
        {"tests/test_t1.py": _STEPS, "requirements.txt": f"{tinydep}\n"}, (criterion,)
    )
    wd = tmp_path / "wd"
    materialize_workdir(state, wd, resolve=resolve, resolve_blob=blob)
    with python_build_env(wd, python=True) as env:
        assert env is not None
        assert BuildGate(pyenv=env).execute(wd).exit_codes == (0, 0)
        gate = TestGate(pyenv=env)
        obs = gate.execute(wd)
    assert obs.structured[BDD_KEY] == {"features/T1.feature::AC-1": "passed"}, obs.stdout
    assert gate.decide(obs) is GateStatus.PASS
    _foundry_has_no("tinydep")
    assert sys.prefix not in env.python
