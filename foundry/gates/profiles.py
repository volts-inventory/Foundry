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

__all__ = ["PROFILES", "VerificationProfile", "installed_profiles", "profile_for_request"]


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


# The pytest harness also executes the tickets' acceptance scenarios.
_BDD_CONTRACT = (
    " BDD: every Scenario in features/<ticket>.feature (named by its criterion id) "
    "MUST be bound with pytest-bdd `scenarios(...)` and pass; the feature files "
    "are read-only."
)


PROFILES: dict[str, VerificationProfile] = {
    "python": VerificationProfile(
        name="python",
        contract_text=(
            "GATES: build = `python -m compileall -q .`; test = pytest (>=1 test "
            "must pass)." + _BDD_CONTRACT
        ),
    ),
    "python-app": VerificationProfile(
        name="python-app",
        smoke_cmd=(sys.executable, "smoke.py"),
        contract_text=(
            "GATES: build = compileall; test = pytest; smoke = `python smoke.py` — "
            "the repo MUST ship smoke.py that exercises the REAL app headless "
            "(start the service and hit one real endpoint in-process, or run the "
            "CLI end-to-end on sample input) and exits 0 only on success." + _BDD_CONTRACT
        ),
    ),
    "node-web": VerificationProfile(
        name="node-web",
        build_cmd=_sh("npm ci --no-audit --no-fund && npm run build --if-present"),
        test_cmd=_sh("npm test --silent"),
        smoke_cmd=("node", "smoke.mjs"),
        contract_text=(
            "GATES: build = `npm ci && npm run build`; test = `npm test`; smoke = "
            "`node smoke.mjs` (the package.json MUST define working build/test "
            "scripts, and the repo MUST ship smoke.mjs that boots the REAL app "
            "headless — start the server and hit one real route in-process, or "
            "run the CLI end-to-end — and exits 0 only on success)."
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
            "no exception was raised." + _BDD_CONTRACT
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
            "modes, or KDFs; no ECB; no static IVs/nonces." + _BDD_CONTRACT
        ),
    ),
    "go": VerificationProfile(
        name="go",
        build_cmd=_sh("go vet ./... && go build ./..."),
        test_cmd=_sh("go test ./..."),
        smoke_cmd=_sh("go run ./smoke"),
        contract_text=(
            "GATES: build = `go vet ./... && go build ./...`; test = `go test "
            "./...`; smoke = `go run ./smoke` — a go.mod MUST exist at the repo "
            "root, >=1 test must pass, and the repo MUST ship a ./smoke package "
            "(main) that exercises the REAL app headless (boot the server and hit "
            "one real endpoint in-process, or run the CLI end-to-end) and exits 0 "
            "only on success."
        ),
        notes=("requires the go toolchain on the host; missing => ERROR, never PASS",),
    ),
    "rust": VerificationProfile(
        name="rust",
        build_cmd=_sh("cargo build --quiet"),
        test_cmd=_sh("cargo test --quiet"),
        smoke_cmd=_sh("cargo run --quiet --bin smoke"),
        contract_text=(
            "GATES: build = `cargo build`; test = `cargo test`; smoke = `cargo run "
            "--bin smoke` — a Cargo.toml MUST exist at the repo root, >=1 test "
            "must pass, and the crate MUST ship a `smoke` bin (src/bin/smoke.rs) "
            "that exercises the REAL app headless and exits 0 only on success."
        ),
        notes=("requires cargo on the host; missing => ERROR, never PASS",),
    ),
    "dotnet": VerificationProfile(
        name="dotnet",
        build_cmd=_sh("dotnet build --nologo -v q"),
        test_cmd=_sh("dotnet test --nologo -v q"),
        smoke_cmd=_sh("dotnet run --project Smoke"),
        contract_text=(
            "GATES: build = `dotnet build`; test = `dotnet test`; smoke = `dotnet "
            "run --project Smoke` — a .sln or .csproj MUST exist at the repo root, "
            ">=1 test must pass, and the solution MUST ship a Smoke console "
            "project that exercises the REAL app headless (for MonoGame: dummy "
            "video driver, advance the game loop) and exits 0 only on success."
        ),
        notes=("requires the dotnet SDK on the host; missing => ERROR, never PASS",),
    ),
    "java-maven": VerificationProfile(
        name="java-maven",
        build_cmd=_sh("mvn -q -B -DskipTests package"),
        test_cmd=_sh("mvn -q -B test"),
        smoke_cmd=_sh("mvn -q -B -DskipTests compile && java -cp target/classes Smoke"),
        contract_text=(
            "GATES: build = `mvn -B -DskipTests package`; test = `mvn -B test`; "
            "smoke = `java -cp target/classes Smoke` (after compile) — a pom.xml "
            "MUST exist at the repo root, >=1 test must pass, and the repo MUST "
            "ship a Smoke class (default package) whose main() exercises the REAL "
            "app headless and exits 0 only on success."
        ),
        notes=("requires maven + a JDK on the host; missing => ERROR, never PASS",),
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
_DOTNET_WORDS = ("c#", "csharp", ".net", "dotnet", "monogame", "asp.net", "xna")
_RUST_WORDS = ("rust", "cargo", "actix", "tokio", "bevy")
_JAVA_WORDS = ("java ", "java,", "java.", "spring boot", "springboot", "maven",
               "libgdx")
# "go" is too short to match bare — require the unambiguous forms.
_GO_WORDS = ("golang", "go api", "go service", "go server", "go cli",
             "go backend", "go microservice", "in go", "with go", "ebitengine")


# Binaries each profile's pinned gates shell out to. A profile whose toolchain is
# absent still RUNS (fail-closed ERROR at the gate) — this map exists so a STACK
# CHOOSER (the Architect picking for "auto") only offers stacks that can actually
# go green on this host. Python-based profiles need only the interpreter.
_REQUIRED_BINARIES: dict[str, tuple[str, ...]] = {
    "node-web": ("node", "npm"),
    "phaser-web": ("node", "npm"),
    "love2d": ("luac",),
    "go": ("go",),
    "rust": ("cargo",),
    "dotnet": ("dotnet",),
    "java-maven": ("mvn", "java"),
    "iac-terraform": ("terraform",),
}


def installed_profiles() -> list[str]:
    """Profile names whose toolchains are PRESENT on this host (sorted).

    ``pygame`` additionally requires the pygame package (its smoke imports it).
    Python-only profiles are always available (we are running under Python)."""
    import importlib.util
    import shutil

    out = []
    for name in PROFILES:
        if not all(shutil.which(b) for b in _REQUIRED_BINARIES.get(name, ())):
            continue
        if name == "pygame" and importlib.util.find_spec("pygame") is None:
            continue
        out.append(name)
    return sorted(out)


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
        if any(w in low for w in _DOTNET_WORDS):        # MonoGame / C# / XNA game
            return PROFILES["dotnet"]
        if any(w in low for w in _RUST_WORDS):          # Bevy / Rust game
            return PROFILES["rust"]
        if any(w in low for w in _GO_WORDS):            # Ebitengine / Go game
            return PROFILES["go"]
        if any(w in low for w in _JAVA_WORDS):          # libGDX / Java game
            return PROFILES["java-maven"]
        if any(w in low for w in ("phaser", "html5", "canvas", "browser", "javascript", "web")):
            return PROFILES["phaser-web"]
        return PROFILES["love2d"]
    if any(w in low for w in _DOTNET_WORDS):
        return PROFILES["dotnet"]
    if any(w in low for w in _RUST_WORDS):
        return PROFILES["rust"]
    if any(w in low for w in _GO_WORDS):
        return PROFILES["go"]
    if any(w in low for w in _JAVA_WORDS):
        return PROFILES["java-maven"]
    if any(w in low for w in _NODE_WORDS):
        return PROFILES["node-web"]
    return PROFILES["python"]
