"""CLI in-process auto-resume (parity with the Workshop server).

A DURABLE ``foundry run --state-dir …`` must survive a TRANSIENT
provider failure (a spent Max usage/session window surfaces as
``ProxyUnavailable``): back off and RE-INVOKE from the durable checkpoint instead
of dying and needing a manual restart. A NON-durable run has no checkpoint, so
the wrapper is a transparent single invoke. A NON-transient error always
re-raises. The backoff sleep is monkeypatched so the test is instant.
"""

from __future__ import annotations

from typing import Any

import foundry.cli as cli


class _Boom(Exception):
    """Stand-in whose class name contains 'ProxyUnavailable' so the predicate
    (which matches on the type name, as the real one does) treats it as transient."""


ProxyUnavailable = _Boom
ProxyUnavailable.__name__ = "ProxyUnavailable"


def _no_sleep(monkeypatch: Any) -> list[float]:
    """Swallow the backoff sleeps and record how long each would have been."""
    waited: list[float] = []
    import time as _t

    monkeypatch.setattr(_t, "sleep", lambda s: waited.append(s))
    return waited


def _call(seed: Any = None, **kw: Any) -> Any:
    return cli._invoke_with_autoresume(
        object(),  # app is never touched — _invoke is patched
        seed or {"request": "x"},
        {"configurable": {"thread_id": "t"}},
        progress_cb=None,
        **kw,
    )


def test_transient_failure_is_retried_from_checkpoint(monkeypatch: Any) -> None:
    """A durable run re-invokes after a transient failure and eventually returns."""
    waited = _no_sleep(monkeypatch)
    calls = {"n": 0}

    def _fake(*_a: Any, **_k: Any) -> dict[str, str]:
        calls["n"] += 1
        if calls["n"] < 3:  # fail twice, succeed on the third invoke
            raise ProxyUnavailable("provider ...; judgment nodes must pause-and-escalate")
        return {"done": "yes"}

    monkeypatch.setattr(cli, "_invoke", _fake)
    final = _call(resumable=True)

    assert final == {"done": "yes"}
    assert calls["n"] == 3  # two retries + the success
    assert waited == [300.0, 600.0]  # 5 min, then 10 min — growing backoff


def test_non_durable_run_does_not_retry(monkeypatch: Any) -> None:
    """With no durable checkpoint the budget is 0 — the failure propagates at once."""
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def _fake(*_a: Any, **_k: Any) -> Any:
        calls["n"] += 1
        raise ProxyUnavailable("pause-and-escalate")

    monkeypatch.setattr(cli, "_invoke", _fake)
    try:
        _call(resumable=False)
        raise AssertionError("expected the transient failure to propagate")
    except _Boom:
        pass
    assert calls["n"] == 1  # invoked once, never retried


def test_non_transient_error_always_reraises(monkeypatch: Any) -> None:
    """A real error (not a proxy pause) is never swallowed, even on a durable run."""
    _no_sleep(monkeypatch)

    def _fake(*_a: Any, **_k: Any) -> Any:
        raise ValueError("a genuine bug")

    monkeypatch.setattr(cli, "_invoke", _fake)
    try:
        _call(resumable=True)
        raise AssertionError("expected the real error to propagate")
    except ValueError:
        pass


def test_budget_is_bounded(monkeypatch: Any) -> None:
    """A never-recovering transient failure gives up after the budget (default 6)
    rather than looping forever — the last raise escapes."""
    waited = _no_sleep(monkeypatch)
    monkeypatch.setenv("FOUNDRY_CLI_AUTORESUME", "3")
    calls = {"n": 0}

    def _fake(*_a: Any, **_k: Any) -> Any:
        calls["n"] += 1
        raise ProxyUnavailable("pause-and-escalate")

    monkeypatch.setattr(cli, "_invoke", _fake)
    try:
        _call(resumable=True)
        raise AssertionError("expected exhaustion to re-raise")
    except _Boom:
        pass
    assert calls["n"] == 4  # initial + 3 retries
    assert waited == [300.0, 600.0, 1200.0]  # 5, 10, 20 min
