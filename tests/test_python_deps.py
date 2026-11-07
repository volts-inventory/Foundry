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
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

import foundry.gates.pydeps as pydeps
import foundry.python_env as python_env
from foundry.agents._common import emit_artifact
from foundry.gates.attestation import ZoneBSigner
from foundry.gates.build import BuildGate, build_findings
from foundry.gates.discriminator import HeldOutMutant, TestSuiteDiscriminator
from foundry.gates.profiles import PROFILES, stack_profile
from foundry.gates.pydeps import (
    SCAFFOLD_HEADER,
    PythonEnvCache,
    declared_requirements,
    install_failure_is_project,
    manifest_digest,
    python_build_env,
    undeclared_import_hints,
)
from foundry.gates.runner import TwoZoneGateRunner
from foundry.gates.sandbox import ToolchainCache
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


_NO_FLASK = (
    "ImportError while importing test module 'tests/test_t2.py'.\n"
    "E   ModuleNotFoundError: No module named 'flask'\n"
    "E   ModuleNotFoundError: No module named 'yaml.loader'\n"
    "E   ModuleNotFoundError: No module named 'app'\n"
    "E   ModuleNotFoundError: No module named 'sqlite3'\n"
)


@pytest.mark.parametrize(
    "files",
    [{}, {"requirements.txt": SCAFFOLD_HEADER + "flask==3.1.0\n"}],
)
def test_an_undeclared_import_names_the_dependency_to_add(
    tmp_path: Path, files: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The notes build imported flask with only the scaffold for a manifest: nothing
    # installed it, and the gate said only `No module named 'flask'`.
    monkeypatch.setattr(pydeps.im, "packages_distributions", dict)
    _write(tmp_path, {**files, "app/__init__.py": ""})
    hints = undeclared_import_hints(_NO_FLASK, tmp_path)
    assert hints[0] == (
        "the project imports `flask` but declares no dependency on it: add `flask` to "
        "requirements.txt (the Foundry-generated manifest is never installed)"
        + ("; requirements.txt still starts with Foundry's `" + SCAFFOLD_HEADER.strip()
           + "` header: rewrite it without that header so it is installed" if files else "")
    )
    assert "add `PyYAML`" in hints[1] and len(hints) == 2  # local and stdlib skipped


def test_a_manifest_missing_the_import_names_the_dependency_to_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The notes build declared Flask & co. but imported bcrypt, which the engineers
    # had only `pip install`ed in their sandbox: the gate's venv lacked it, the step
    # modules failed to import, and no finding said to declare it.
    monkeypatch.setattr(pydeps.im, "packages_distributions", dict)
    _write(tmp_path, {"requirements.txt": "Flask==3.0.0\nPyYAML==6.0.1\n",
                      "app/__init__.py": ""})
    output = _NO_FLASK + "E   ModuleNotFoundError: No module named 'bcrypt'\n"
    assert undeclared_import_hints(output, tmp_path) == (
        "the project imports `bcrypt` but its declared dependencies do not include it: "
        "add `bcrypt` to requirements.txt (a `pip install` you ran in your sandbox is "
        "not installed for the gates — only the declared dependencies are)",
    )  # flask and yaml (as PyYAML) are declared: their failure is something else


def test_a_manifest_line_that_is_not_a_plain_requirement_gets_no_hint(
    tmp_path: Path,
) -> None:
    _write(tmp_path, {"requirements.txt": "requests==2.31.0\n-e ./vendor/flask\n"})
    assert undeclared_import_hints(_NO_FLASK, tmp_path) == ()


def test_the_build_finding_carries_the_undeclared_import_hint(tmp_path: Path) -> None:
    findings = build_findings(1, _NO_FLASK, "", tmp_path, step="smoke")
    assert any(f.startswith("the project imports `flask`") for f in findings)


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
    # A declared sdist that does not build here: the project must pick another one.
    sdist = (
        "  error: subprocess-exited-with-error\n"
        "  src/lxml/etree.c:1:10: fatal error: libxml/xmlversion.h: No such file\n"
        "  error: command 'gcc' failed with exit code 1\n"
        "  ERROR: Failed building wheel for lxml\nFailed to build lxml\n"
    )
    assert install_failure_is_project(sdist)
    assert install_failure_is_project("Failed to build lxml\n")
    # ...unless fetching its build dependencies hit the network: infra still wins.
    assert not install_failure_is_project(sdist + "ReadTimeoutError: read timed out\n")


def test_unavailable_requirement_names_the_line_and_the_stdlib(tmp_path: Path) -> None:
    _write(tmp_path, {"requirements.txt": "Flask==3.0.0\nsqlite3==2.6.0\nnopepkg>=1\n"})
    out = (
        "ERROR: Could not find a version that satisfies the requirement sqlite3==2.6.0 "
        "(from versions: none)\nERROR: No matching distribution found for sqlite3==2.6.0\n"
        "ERROR: No matching distribution found for nopepkg>=1\n"
    )
    findings = build_findings(1, out, "", tmp_path, step="pip install")
    assert findings[0].startswith("pip install command exited 1")
    assert (
        "`sqlite3` is part of Python's standard library, not a PyPI package: delete the "
        "line `sqlite3==2.6.0` (line 2) from requirements.txt"
    ) in findings
    assert any(
        "`nopepkg>=1` (requirements.txt line 3" in f and "standard library" not in f
        for f in findings
    )
    # Explanation only: the stdlib name stays declared until pip itself rejects it.
    assert declared_requirements(tmp_path) == ("-r", "requirements.txt")


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
    def __init__(self) -> None:
        self.python_envs = PythonEnvCache()
        self.toolchain_cache = ToolchainCache()

    def seq(self, config: Any, node: str, key: str) -> Seq:
        return _seq(node)


_APP = {
    "app.py": "import tinydep\n\nVALUE = tinydep.VALUE\n",
    # Imported inside the test: a collection error is a harness ERROR, not a FAIL.
    "test_app.py": "def test_value():\n    from app import VALUE\n\n    assert VALUE == 42\n",
    "smoke.py": "import app\n\nassert app.VALUE == 42\n",
}


def _gates(tmp_path: Path, files: dict[str, str], rt: _Rt | None = None) -> dict[str, Any]:
    state, resolve, blob = _state(files)
    with tempfile.TemporaryDirectory(dir=tmp_path) as wd:
        out = run_build_test_gates(
            state, {"configurable": {}}, rt=rt or _Rt(),  # type: ignore[arg-type]
            runner=_runner(), resolve=resolve, resolve_blob=blob, workdir=Path(wd),
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


def test_a_test_spawning_sys_executable_runs_on_the_build_venv(
    tmp_path: Path, tinydep: str, allow_install: None
) -> None:
    # The local notes bench: SC-601 ran `[sys.executable, "smoke.py"]`, got Foundry's
    # interpreter (no flask), and failed with `No module named 'flask'` through six fix
    # passes that each saw it pass in the agent's own sandbox.
    test = (
        "import subprocess, sys\n\n\n"
        "def test_smoke():\n"
        "    r = subprocess.run([sys.executable, 'smoke.py'], capture_output=True, text=True)\n"
        "    assert r.returncode == 0, r.stderr\n"
    )
    gates = _gates(tmp_path, {**_APP, "test_app.py": test, "requirements.txt": f"{tinydep}\n"})
    assert gates["test"].status is GateStatus.PASS, gates["test"].findings


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


# ---- the harness's sys.path order --------------------------------------------------


def test_a_declared_module_cannot_replace_the_stdlib_but_overrides_foundrys_copy(
    tmp_path: Path, tinydep: str, allow_install: None
) -> None:
    pytest.importorskip("six")  # in Foundry's environment, so the override is observable
    host_six = im.version("six")
    # `colorsys`: a stdlib module neither pytest nor the bootstrap imports first.
    stdlib = _wheel(tmp_path / "wheels", "evil-stdlib",
                    {"colorsys/__init__.py": "rgb_to_hsv = None\n"})
    six = _wheel(tmp_path / "wheels", "six", {"six.py": "MARK = 'project'\n"}, version="99.0")
    test = (
        "def test_paths():\n"
        "    import colorsys, six\n"
        "    assert colorsys.rgb_to_hsv(1.0, 0.0, 0.0) == (0.0, 1.0, 1.0)\n"
        "    assert six.MARK == 'project'\n"
    )
    reqs = f"{tinydep}\nevil-stdlib @ {stdlib.as_uri()}\nsix @ {six.as_uri()}\n"
    gates = _gates(tmp_path, {**_APP, "test_app.py": test, "requirements.txt": reqs})
    assert gates["test"].status is GateStatus.PASS, gates["test"].findings
    assert im.version("six") == host_six  # Foundry's copy untouched


def test_a_declared_pytest_cannot_kill_the_discriminators_mutants(
    tmp_path: Path, allow_install: None
) -> None:
    # A `pytest` that always exits 1 would "kill" every mutant if it ran the suite.
    evil = _wheel(tmp_path / "wheels", "evil-pytest", {
        "pytest/__init__.py": "def console_main():\n    return 1\n",
        "pytest/__main__.py": "raise SystemExit(1)\n",
    })
    wd = _write(tmp_path / "wd", {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "test_calc.py": "def test_vacuous():\n    assert True\n",
        "requirements.txt": f"evil-pytest @ {evil.as_uri()}\n",
    })
    mutant = HeldOutMutant(mutant_id="m1", target="calc.py", find="a + b", replace="a - b")
    with python_build_env(wd, python=True) as env:
        assert env is not None
        assert env.install(wd, timeout_s=120)[0] == 0
        report = TestSuiteDiscriminator(mutants=[mutant]).evaluate(
            wd, site=env.site, binds=env.binds
        )
    assert report.survived == ("m1",), report.findings  # the vacuous suite killed nothing


# ---- seeding pip into the venv ---------------------------------------------------


def test_venv_reads_foundrys_pip_without_a_bundled_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(python_env, "_bundled_pip_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(python_env, "_host_has_pip", lambda: True)
    python_env.build_python_env(tmp_path / "venv")
    assert not (python_env.site_packages(tmp_path / "venv") / "pip").exists()


def test_venv_falls_back_to_ensurepip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Debian/Ubuntu: ensurepip finds its wheels elsewhere, the bundled dir is empty.
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(python_env, "_bundled_pip_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(python_env, "_host_has_pip", lambda: False)
    python_env.build_python_env(tmp_path / "venv")
    assert (python_env.site_packages(tmp_path / "venv") / "pip").is_dir()


def test_no_pip_source_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(python_env, "_bundled_pip_dir", lambda: None)  # no ensurepip
    monkeypatch.setattr(python_env, "_host_has_pip", lambda: False)
    with pytest.raises(RuntimeError, match="no pip for the project's Python environment"):
        python_env.build_python_env(tmp_path / "venv")


# ---- one install per manifest within a run ---------------------------------------


@pytest.fixture()
def built(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    seen: list[Path] = []
    real = pydeps.build_python_env

    def _spy(dest: Path) -> None:
        seen.append(dest)
        real(dest)

    monkeypatch.setattr(pydeps, "build_python_env", _spy)
    return seen


# `pytest>=1` installs offline ("already satisfied" from Foundry's copy) and is no
# exact pin, so it takes the install path.
_CACHEABLE = {"app.py": "VALUE = 42\n", "test_app.py": _APP["test_app.py"],
              "smoke.py": "import app\n", "requirements.txt": "pytest>=1\n"}


def test_the_run_reuses_one_install_per_manifest(
    tmp_path: Path, built: list[Path], allow_install: None
) -> None:
    rt = _Rt()
    for _ in range(2):
        gates = _gates(tmp_path, _CACHEABLE, rt)
        assert all(g.status is GateStatus.PASS for g in gates.values()), gates
    assert len(built) == 1
    changed = {**_CACHEABLE, "requirements.txt": "pytest>=2\n"}
    assert _gates(tmp_path, changed, rt)["build"].status is GateStatus.PASS
    assert len(built) == 2 and built[0] != built[1]
    _gates(tmp_path, _CACHEABLE, _Rt())  # another run never shares
    assert len(built) == 3
    root = built[0].parent.parent
    assert root.exists()
    rt.python_envs.close()
    assert not root.exists()


def test_a_failed_install_is_retried_on_the_next_check(
    tmp_path: Path, built: list[Path], allow_install: None
) -> None:
    rt = _Rt()
    files = {**_APP, "requirements.txt": "--no-index\nfoundry-no-such-package-xyz\n"}
    for _ in range(2):
        assert _gates(tmp_path, files, rt)["build"].status is GateStatus.FAIL
    assert len(built) == 2


def test_local_content_is_never_reused(
    tmp_path: Path, tinydep: str, built: list[Path], allow_install: None
) -> None:
    rt = _Rt()
    files = {**_APP, "requirements.txt": f"{tinydep}\n"}
    for _ in range(2):
        assert _gates(tmp_path, files, rt)["test"].status is GateStatus.PASS
    assert len(built) == 2
    wd = _write(tmp_path / "m", {"requirements.txt": "./vendor/pkg\n"})
    assert manifest_digest(wd, declared_requirements(wd)) is None
