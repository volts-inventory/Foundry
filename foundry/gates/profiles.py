"""Verification profiles — stack-appropriate gate command bundles. (ROADMAP Phase 3)

"Green must mean it works." The two-zone runner takes pinned commands, so a
profile is nothing more than a NAMED, PINNED bundle of build/test/smoke argv
tuples selected from the request's stack inference and recorded in the run:
games are one profile; the same seam carries web services, crypto software, and
IaC. A ``None`` command keeps that gate's historical default (compileall /
pytest), so the ``python`` profile is byte-identical to the pre-profile factory.

Fail-closed posture is inherited from the gates themselves: a pinned command
whose binary is absent raises ``GroundingUnavailable`` in zone A, which the
runner signs as ERROR — a missing toolchain is loudly not-a-verdict, never a
silent PASS (e.g. the ``love2d`` profile on a host without ``love``).

``contract_text`` is the profile's promise TO the coding substrate: exactly
what the gates will execute, so the fix loop (and a smart adapter) can target
the real gate contract instead of guessing. It is advisory prompt text, never
a verdict input.

Dev-grade honesty (plan §0.3): these are the container-friendly stand-ins —
e.g. the Phaser smoke boots the game module under Node, not a real browser;
production swaps the command set (Playwright + pinned Chromium), not the seam.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

__all__ = ["PROFILES", "VerificationProfile", "profile_for_request"]


@dataclass(frozen=True, slots=True)
class VerificationProfile:
    """A named, pinned gate-command bundle. ``None`` = that gate's default."""

    name: str
    build_cmd: tuple[str, ...] | None = None
    test_cmd: tuple[str, ...] | None = None
    smoke_cmd: tuple[str, ...] | None = None  # runtime boot gate; None = no smoke gate
    contract_text: str = ""  # advisory: what the gates will run (prompt-side)
    notes: tuple[str, ...] = field(default=())


def _sh(script: str) -> tuple[str, ...]:
    return ("/bin/sh", "-c", script)


PROFILES: dict[str, VerificationProfile] = {
    "python": VerificationProfile(
        name="python",
        contract_text=(
            "GATES: build = `python -m compileall -q .`; test = pytest (>=1 test "
            "must pass)."
        ),
    ),
    "node-web": VerificationProfile(
        name="node-web",
        build_cmd=_sh("npm ci --no-audit --no-fund && npm run build --if-present"),
        test_cmd=_sh("npm test --silent"),
        contract_text=(
            "GATES: build = `npm ci && npm run build`; test = `npm test` (the "
            "package.json MUST define working build/test scripts)."
        ),
    ),
    "phaser-web": VerificationProfile(
        name="phaser-web",
        build_cmd=_sh("npm ci --no-audit --no-fund && npm run build --if-present"),
        test_cmd=_sh("npm test --silent"),
        smoke_cmd=("node", "smoke.mjs"),
        contract_text=(
            "GATES: build = `npm ci && npm run build`; test = `npm test`; smoke = "
            "`node smoke.mjs` — the repo MUST ship a headless smoke.mjs that boots "
            "the game loop, advances >=100 simulated ticks with scripted input, and "
            "exits 0 only if no exception was thrown."
        ),
        notes=("production swaps the smoke for Playwright + pinned Chromium",),
    ),
    "pygame": VerificationProfile(
        name="pygame",
        smoke_cmd=(sys.executable, "smoke.py"),
        contract_text=(
            "GATES: build = compileall; test = pytest; smoke = `python smoke.py` — "
            "the repo MUST ship smoke.py that sets SDL_VIDEODRIVER=dummy, boots the "
            "game, advances >=120 frames with scripted input, and exits 0 only if "
            "no exception was raised."
        ),
    ),
    "love2d": VerificationProfile(
        name="love2d",
        build_cmd=("luac", "-p", "main.lua"),
        contract_text=(
            "GATES: build = `luac -p main.lua` (syntax-checks the LOVE entrypoint). "
            "A runtime boot gate for LOVE is a production concern (headless LOVE); "
            "main.lua MUST exist at the repo root."
        ),
        notes=("no headless boot for LÖVE at dev-grade; toolchain missing => ERROR",),
    ),
    "crypto": VerificationProfile(
        name="crypto",
        contract_text=(
            "GATES: build = compileall; test = pytest — the suite MUST include "
            "known-answer tests (NIST/RFC vectors) for every primitive touched, a "
            "round-trip property (encrypt->decrypt identity), and a tamper test "
            "(modified ciphertext MUST fail authentication). Use vetted primitives "
            "(the `cryptography` package / libsodium); NEVER hand-roll ciphers, "
            "modes, or KDFs; no ECB; no static IVs/nonces."
        ),
    ),
    "iac-terraform": VerificationProfile(
        name="iac-terraform",
        build_cmd=_sh("terraform fmt -check -recursive"),
        test_cmd=_sh("terraform init -backend=false -input=false && terraform validate"),
        contract_text=(
            "GATES: build = `terraform fmt -check`; test = `terraform init "
            "-backend=false && terraform validate`. `terraform apply` is NEVER a "
            "gate — deploy is a post-gate, credentialed-phase action."
        ),
        notes=("plan/apply stay in the credentialed phase (ROADMAP 1b typology)",),
    ),
}


# Keyword tables mirror the platform planner's stack inference
# (`platform/planner.py` `_GAME_HINTS` / `_engine_hint`) so the CLI's single-repo
# path and the platform path infer the same stack from the same words.
_GAME_WORDS = (
    "game", "nes", "snes", "arcade", "tetris", "pong", "pixel", "platformer",
    "roguelike", "sprite", "tilemap", "stardew", "metroidvania", "shoot-em-up",
    "sokoban", "breakout", "asteroids",
)
_CRYPTO_WORDS = (
    "encrypt", "decrypt", "cipher", "cryptograph", "password manager",
    "password vault", "key derivation", "signing service", "e2ee",
)
_IAC_WORDS = (
    "terraform", "cloudformation", "aws infra", "aws infrastructure", "iac",
    "infrastructure as code", "kubernetes manifests", "k8s cluster",
)
_NODE_WORDS = ("react", "next.js", "nextjs", "vue", "svelte", "node.js", "nodejs",
               "typescript", "javascript")


def profile_for_request(request: str) -> VerificationProfile:
    """The verification profile inferred from the one-line request / spec text.

    Mirrors the planner's engine inference for games (pygame / Phaser-HTML5 /
    LÖVE default); crypto and IaC words take precedence over generic web words;
    everything unrecognized stays on the ``python`` default (byte-identical
    gates to the pre-profile factory).
    """
    low = request.lower()
    if any(w in low for w in _IAC_WORDS):
        return PROFILES["iac-terraform"]
    if any(w in low for w in _CRYPTO_WORDS):
        return PROFILES["crypto"]
    if any(w in low for w in _GAME_WORDS):
        if any(w in low for w in ("pygame", "python game", "python")):
            return PROFILES["pygame"]
        if any(w in low for w in ("phaser", "html5", "canvas", "browser", "javascript", "web")):
            return PROFILES["phaser-web"]
        return PROFILES["love2d"]
    if any(w in low for w in _NODE_WORDS):
        return PROFILES["node-web"]
    return PROFILES["python"]
