"""The Workshop's state-changing-request guard (W2): a same-origin/CSRF check that
blocks a malicious web page from POSTing to a localhost workshop, plus an optional
shared token for when the server is exposed beyond localhost.
"""

from __future__ import annotations

from webapp.server import _authorize_mutation


def _h(**kw: str) -> dict[str, str]:
    return dict(kw)


# --- CSRF / same-origin (always enforced) ------------------------------------- #

def test_same_origin_post_allowed() -> None:
    h = _h(Origin="http://127.0.0.1:8765", Host="127.0.0.1:8765")
    assert _authorize_mutation(h, token=None) is None


def test_cross_origin_post_rejected() -> None:
    h = _h(Origin="http://evil.example", Host="127.0.0.1:8765")
    reason = _authorize_mutation(h, token=None)
    assert reason is not None and "CSRF" in reason


def test_no_origin_header_allowed() -> None:
    # curl / same-origin fetch may omit Origin; the token gate (if any) still applies.
    assert _authorize_mutation(_h(Host="127.0.0.1:8765"), token=None) is None


# --- shared token (enforced when configured) ---------------------------------- #

def test_token_required_when_configured() -> None:
    h = _h(Host="h:1")  # no token header
    assert _authorize_mutation(h, token="secret") is not None


def test_correct_token_accepted() -> None:
    h = _h(Host="h:1")
    h["X-Workshop-Token"] = "secret"
    assert _authorize_mutation(h, token="secret") is None


def test_wrong_token_rejected() -> None:
    h = _h(Host="h:1")
    h["X-Workshop-Token"] = "nope"
    assert _authorize_mutation(h, token="secret") is not None


def test_token_and_cross_origin_both_enforced() -> None:
    # A cross-origin request is rejected on the CSRF check before the token even.
    h = _h(Origin="http://evil.example", Host="h:1")
    h["X-Workshop-Token"] = "secret"
    assert _authorize_mutation(h, token="secret") is not None
