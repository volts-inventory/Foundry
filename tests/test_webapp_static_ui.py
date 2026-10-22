"""Static-UI contract: the Workshop page and its script must stay wired together.

There is no JS test harness here, so these are the invariants worth pinning in
Python — the ones whose breakage is silent in the browser:

* every element ``game.js`` reaches for must exist in ``index.html`` (a dialog
  restructure that drops one field ID breaks a build option with no error);
* every mutation of the LIVE quest log must go through ``inLog`` (that is what
  keeps the log scrolled to the newest activity — a bare ``appendChild`` puts new
  lines under the fold and the log looks frozen);
* the working halo must be lit and dropped on the two seating paths.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "webapp" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "game.js").read_text(encoding="utf-8")

# Built by the script itself (the provider-limit banner), so never in the markup.
RUNTIME_IDS = {"limit-toast", "limit-toast-msg"}


def test_every_id_the_script_reads_exists_in_the_page() -> None:
    ids = set(re.findall(r'\bid="([^"]+)"', HTML))
    used = set(re.findall(r'getElementById\("([^"]+)"\)', JS))
    assert not (used - ids - RUNTIME_IDS)


def test_build_dialog_is_request_plus_collapsed_settings() -> None:
    # The settings fold holds only the worker limits + token cap, shut by default,
    # with a summary line that is recomputed whenever the dialog opens.
    assert re.search(r'<details[^>]*id="fold-scale"(?![^>]*\bopen\b)', HTML)
    assert 'id="scale-sum"' in HTML
    for gone in ("backend-select", "adapter-select", "profile-select", "fix-input",
                 "cap-input", "preset-select", "spec-text", "startfrom-select"):
        assert f'id="{gone}"' not in HTML
    show = JS.split("function showNewModal(")[1].split("\n}")[0]
    assert "syncSettingsSummary()" in show


def test_live_log_mutations_all_go_through_the_follow_helper() -> None:
    # Anything appending to (or replacing a node inside) #log must be wrapped in
    # inLog, which is what re-pins the view to the bottom.
    for fn in ("appendLogDom", "agentBox", "refreshAgentBox"):
        body = JS.split(f"function {fn}(")[1].split("\n}")[0]
        assert "inLog(" in body, fn
    assert 'id="log-jump"' in HTML          # the affordance for when you HAVE scrolled up


def test_the_working_halo_is_lit_at_the_desk_and_dropped_on_stand_down() -> None:
    for method, call in (("deskRest(a, working, busy)", "setGlow(a, working)"),
                         ("standDown(a)", "setGlow(a, false)")):
        body = JS.split(f"\n  {method}{{")[1].split("\n  }")[0]
        assert call in body, method
    # …and a sprite destroyed mid-scene must drop its halo first: destroy() does not
    # kill the repeating tween that writes to it.
    kill = JS.split("layoutEngineering(){")[1].split("\n    if(this.techLeadGroup)")[0]
    assert "setGlow(s, false)" in kill
