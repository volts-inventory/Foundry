"""The private Python environment untrusted project code runs in.

One builder for both cages — the agent's execution sandbox and the gates' per-build
dependency install — so the agent and the gates see the same interpreter, the same
read-only view of Foundry's packages, and the same rule: a project's installs land
in ITS venv, never in Foundry's.
"""

from __future__ import annotations

import importlib.util
import os
import site
import subprocess
import sys
import sysconfig
import venv
import zipfile
from collections.abc import Sequence
from pathlib import Path

__all__ = ["activation_env", "build_python_env", "foundry_venv_bin", "site_packages"]


def site_packages(env: Path) -> Path:
    """The ``purelib`` directory of the venv at ``env``."""
    return Path(sysconfig.get_path("purelib", vars={"base": str(env), "platbase": str(env)}))


def foundry_venv_bin() -> Path | None:
    """Foundry's own virtualenv ``bin`` (None when Foundry runs on a bare interpreter).

    Under ``uv run`` it heads the inherited PATH, so a sandboxed ``python -m pip`` /
    ``uv pip`` would install into FOUNDRY'S environment."""
    if sys.prefix == sys.base_prefix:
        return None
    return Path(sys.prefix) / "bin"


def activation_env(env: Path, path: str) -> dict[str, str]:
    """The variables that put a command on the venv at ``env``: its ``bin`` first on
    ``path`` with Foundry's own venv ``bin`` removed, and ``VIRTUAL_ENV`` (which
    ``uv pip`` also targets) pointing at it."""
    host_bin = foundry_venv_bin()
    if host_bin is not None:
        target = os.path.realpath(host_bin)
        path = os.pathsep.join(
            p for p in path.split(os.pathsep) if p and os.path.realpath(p) != target
        )
    return {
        "PATH": os.pathsep.join(p for p in (str(env / "bin"), path) if p),
        "VIRTUAL_ENV": str(env),
        # Foundry's site-packages are on the venv's sys.path: an import must not
        # write __pycache__ into them.
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _write_shim(path: Path, argv: Sequence[str]) -> None:
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in argv)
    path.write_text(f'#!/bin/sh\nexec {quoted} "$@"\n', encoding="utf-8")
    path.chmod(0o755)


def _is_python_script(path: Path) -> bool:
    # Covers a plain ``#!.../python`` shebang and the ``#!/bin/sh`` + ``'''exec'``
    # polyglot installers write when the interpreter path is too long for a shebang.
    try:
        with path.open("rb") as f:
            head = f.read(512)
    except OSError:
        return False
    return head.startswith(b"#!") and b"python" in b"\n".join(head.split(b"\n")[:2])


def _bundled_pip_dir() -> Path | None:
    """``ensurepip``'s bundled-wheel directory; None when the interpreter ships no
    ``ensurepip`` at all (Debian/Ubuntu without ``python3-venv``)."""
    try:
        import ensurepip
    except ImportError:
        return None
    return Path(ensurepip.__file__).parent / "_bundled"


def _host_has_pip() -> bool:
    return importlib.util.find_spec("pip") is not None


def _seed_pip(python: str, purelib: Path) -> None:
    """Give the venv a pip, cheapest real source first.

    1. Unpack the interpreter's bundled pip wheel (~40ms; ``ensurepip`` itself
       takes ~1s, and every ticket builds a sandbox).
    2. Foundry's own environment has pip: the venv already imports it through the
       host-site ``.pth``, and pip installs into ``sys.prefix`` (the venv).
    3. ``python -m ensurepip``: Debian/Ubuntu's patched ensurepip finds its wheels
       in /usr/share/python-wheels, not in the (empty) bundled directory.

    (uv is no option here: seeding pip through it needs the network, and ``uv pip``
    does not take pip's flags, which both the agent and the gates use.)
    """
    bundled = _bundled_pip_dir()
    wheels = sorted(bundled.glob("pip-*.whl")) if bundled is not None else []
    if wheels:
        with zipfile.ZipFile(wheels[-1]) as zf:
            zf.extractall(purelib)
        return
    if _host_has_pip():
        return
    detail = "the interpreter ships no ensurepip"
    if bundled is not None:
        r = subprocess.run(
            [python, "-m", "ensurepip", "--default-pip"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if r.returncode == 0:
            return
        detail = f"`python -m ensurepip` failed: {r.stderr.strip()[-300:]}"
    raise RuntimeError(
        f"no pip for the project's Python environment ({sys.executable}): no bundled "
        f"pip wheel, no pip in Foundry's environment, and {detail}. Install pip for "
        "this interpreter (Debian/Ubuntu: `apt install python3-venv python3-pip`)."
    )


def build_python_env(dest: Path) -> None:
    """A private virtualenv at ``dest`` layered READ-ONLY over Foundry's packages.

    * Foundry's site-packages are appended to the venv's ``sys.path`` by a ``.pth``
      of plain paths, so the project's tools (pytest, pytest-bdd, mypy) import from
      the same packages the gates run on. Plain paths, not ``addsitedir``: the
      ``.pth`` files inside (Foundry's editable install) are not processed, so
      ``foundry`` itself stays off the path, as it does in the gate harness.
    * Installs land in the venv and shadow those packages; pip never uninstalls a
      distribution outside ``sys.prefix``, so Foundry's copies are left alone.
    * pip: see :func:`_seed_pip`.
    * Foundry's console scripts get shims that run them on the venv's python, so
      ``pytest`` / ``mypy`` see the venv's installs; native tools are linked.
    """
    venv.EnvBuilder(with_pip=False, symlinks=True).create(dest)
    purelib = site_packages(dest)
    host_sites = [p for p in dict.fromkeys(site.getsitepackages()) if Path(p).is_dir()]
    (purelib / "_foundry_host_site.pth").write_text(
        "".join(f"{p}\n" for p in host_sites), encoding="utf-8"
    )
    bin_dir = dest / "bin"
    python = str(bin_dir / "python")
    _seed_pip(python, purelib)
    for name in ("pip", "pip3", f"pip{sys.version_info[0]}.{sys.version_info[1]}"):
        _write_shim(bin_dir / name, (python, "-m", "pip"))
    host_bin = foundry_venv_bin()
    if host_bin is None:
        return
    for entry in sorted(host_bin.iterdir()):
        target = bin_dir / entry.name
        if target.exists() or not entry.is_file() or not os.access(entry, os.X_OK):
            continue
        if _is_python_script(entry):
            _write_shim(target, (python, str(entry)))
        else:
            target.symlink_to(entry)
