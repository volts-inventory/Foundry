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

import re
import shlex
import sys
from dataclasses import dataclass, field

from foundry.schemas.implementation import StackDecision

__all__ = [
    "BDD_RUNNERS",
    "DEFAULT_INSTALL",
    "PROFILES",
    "VerificationProfile",
    "bdd_runner_hint",
    "installed_profiles",
    "lockfile_tolerant_install",
    "pinned_stack",
    "profile_examples",
    "profile_for_request",
    "pytest_bdd_binds",
    "stack_install",
    "stack_profile",
]


@dataclass(frozen=True, slots=True)
class VerificationProfile:
    """A named, pinned gate-command bundle. ``None`` = that gate's default."""

    name: str
    # Dependency install (shell line), run by BuildGate FIRST and the only gate step
    # with network; build/test/smoke then run offline. None = nothing to install.
    install_cmd: str | None = None
    # A Python stack: its declared dependencies (requirements.txt / pyproject) are
    # pip-installed into a per-build venv instead (``foundry.gates.pydeps``).
    python: bool = False
    build_cmd: tuple[str, ...] | None = None
    test_cmd: tuple[str, ...] | None = None
    smoke_cmd: tuple[str, ...] | None = None  # runtime boot gate; None = no smoke gate
    contract_text: str = ""  # advisory: what the gates will run (prompt-side)
    notes: tuple[str, ...] = field(default=())
    # A planner-chosen stack's Cucumber runner (shell line, writes $FOUNDRY_BDD_REPORT).
    bdd_cmd: str | None = None
    # Why this profile cannot gate on this host (e.g. its toolchain is absent): every
    # gate is an attested infra ERROR naming it. Empty = the profile can run.
    unavailable: str = ""
    # Why the TEST gate cannot run (a non-Python stack with no test and no bdd
    # command): that gate alone is an ERROR, never the pytest harness.
    test_unavailable: str = ""
    # The language whose static app checks (``foundry.gates.app_checks``) BUILD runs:
    # None = the language this profile builds, "" = none.
    app_checks: str | None = None
    # The launch gate (``foundry.gates.launch``): the shell line that starts the app
    # and how it is driven ("cli" | "http"; "" = nothing to launch). None = the gate
    # derives it from the plan's entrypoint ticket.
    launch_cmd: str | None = None
    launch_kind: str = ""
    # The language whose step-definition lint the Test gate runs over a Cucumber
    # stack's step files (``javascript`` | ``typescript`` | ``go``; "" = none).
    step_lint: str = ""


def _sh(script: str) -> tuple[str, ...]:
    return ("/bin/sh", "-c", script)


# A lockfile-strict install and its fallback: (command pattern, lockfiles, the
# replacement for the strict part when no lockfile exists).
_STRICT_INSTALLS: tuple[tuple[re.Pattern[str], tuple[str, ...], str], ...] = (
    (re.compile(r"npm\s+ci\b"), ("package-lock.json", "npm-shrinkwrap.json"), "npm install"),
    (
        re.compile(r"yarn(\s+install)?\s+--(frozen-lockfile|immutable)\b"),
        ("yarn.lock",),
        "yarn install",
    ),
    (re.compile(r"pnpm\s+(install|i)\s+--frozen-lockfile\b"), ("pnpm-lock.yaml",), "pnpm install"),
)
_SHELL_META = re.compile(r"[;&|<>`$()]")


def lockfile_tolerant_install(install: str) -> str:
    """``install`` made to work on a tree without a lockfile: a bare lockfile-strict
    install (``npm ci``, ``yarn --frozen-lockfile``, ``pnpm i --frozen-lockfile``) runs
    as is when its lockfile exists and as the plain install otherwise. Agents write
    the manifest, rarely the lockfile, and ``npm ci`` without one fails the same way
    on every retry. A compound or unrecognized command is returned unchanged."""
    cmd = install.strip()
    if _SHELL_META.search(cmd):
        return install
    for strict, lockfiles, loose in _STRICT_INSTALLS:
        m = strict.match(cmd)
        if m is None:
            continue
        rest = cmd[m.end() :]
        test = " || ".join(f"[ -f {f} ]" for f in lockfiles)
        return f"if {test}; then {cmd}; else {loose}{rest}; fi"
    return install


# The pytest harness also executes the tickets' acceptance scenarios.
_PY_INSTALL_CONTRACT = (
    " INSTALL: the dependencies declared in requirements.txt / pyproject.toml "
    "[project].dependencies are pip-installed into a per-build venv first (the ONLY "
    "step with network); declare every third-party package the code imports."
)
_BDD_CONTRACT = (
    " BDD: every Scenario in features/<ticket>.feature (named by its criterion id) "
    "MUST be bound with pytest-bdd `scenarios(...)` and pass; the feature files "
    "are read-only."
)


# godog has no Cucumber Messages writer: its Cucumber JSON goes to the report and
# the gate converts it. The runner chdirs to the repo root so the report's feature
# URIs are ``features/<ticket>.feature``, the paths the gate trusts.
_GODOG_RUNNER = """\
package bdd

import (
	"os"
	"testing"

	"github.com/cucumber/godog"
)

var registrars []func(*godog.ScenarioContext)

func register(f func(*godog.ScenarioContext)) { registrars = append(registrars, f) }

func TestFeatures(t *testing.T) {
	if err := os.Chdir(".."); err != nil {
		t.Fatal(err)
	}
	format := "pretty"
	if p := os.Getenv("FOUNDRY_BDD_REPORT"); p != "" {
		format = "cucumber:" + p
	}
	suite := godog.TestSuite{
		ScenarioInitializer: func(sc *godog.ScenarioContext) {
			for _, f := range registrars {
				f(sc)
			}
		},
		Options: &godog.Options{
			Format: format, Paths: []string{"features"}, Strict: true, TestingT: t,
		},
	}
	if suite.Run() != 0 {
		t.Fatal("feature scenarios failed")
	}
}
"""

_CUCUMBER_RS_RUNNER = """\
use cucumber::{given, then, when, writer, World as _};

#[derive(Debug, Default, cucumber::World)]
pub struct World { /* scenario state */ }

// step definitions: #[given("exact text")], #[when(expr = "I add {string}")],
// #[then(regex = r"^the list has (\\d+) items?$")] fn ...(w: &mut World, ...)

#[tokio::main]
async fn main() {
    match std::env::var("FOUNDRY_BDD_REPORT") {
        Ok(path) => {
            let out = std::fs::File::create(path).expect("report file");
            World::cucumber().with_writer(writer::Json::new(out)).run("features").await;
        }
        Err(_) => World::run("features").await,
    }
}
"""

# cucumber-js config: `import` loads CommonJS and ESM step files alike.
_CUCUMBER_JSON = (
    '{{"default": {{"paths": ["features/**/*.feature"], '
    '"import": ["features/step_definitions/**/*.{steps}"], "forceExit": true}}}}'
)

# What each language's standard Cucumber runner needs to write the report the gate
# reads (``foundry.gates.cucumber``), keyed by StackDecision.language. Runner paths
# match the planner's default harness files.
BDD_RUNNERS: dict[str, str] = {
    "go": (
        "godog (github.com/cucumber/godog v0.16+) has no Cucumber Messages writer; the "
        "gate reads its Cucumber JSON instead. bdd = `go test -count=1 ./bdd`. "
        "bdd/godog_test.go MUST be exactly:\n"
        + _GODOG_RUNNER
        + "It compiles with no step files (the harness wave is checked alone). Each "
        "feature ticket's step file in package bdd (e.g. bdd/<feature>_steps_test.go) "
        "registers its steps in `func init() { register(func(sc *godog.ScenarioContext) "
        "{ ... }) }`, each as sc.Step(`^...$`, fn) with a string-literal regex (the gate reads the "
        "patterns from source to reject catch-all matchers; a pattern built at run "
        "time counts as one). Require godog in go.mod (install fetches it)."
    ),
    "rust": (
        "cucumber-rs has no Cucumber Messages writer; the gate reads its Cucumber "
        "JSON (the crate's `output-json` feature) instead. bdd = `cargo test --quiet "
        "--test cucumber`. Cargo.toml: [dev-dependencies] cucumber = { version = "
        '"0.23", features = ["output-json"] }, tokio = { version = "1", '
        'features = ["macros", "rt-multi-thread"] }; and [[test]] name = '
        '"cucumber", harness = false. Declare no [lib] or [[bin]] section: Cargo '
        "finds src/lib.rs, src/main.rs and src/bin/*.rs once the feature tickets write "
        "them, while a declared target whose file is not written yet fails every "
        "build before them (a manifest with only the [[test]] target builds); the "
        "default binary is named after [package] name, so match the run command's "
        "--bin. tests/cucumber.rs:\n"
        + _CUCUMBER_RS_RUNNER
        + "Step attributes MUST use string literals (the gate reads the patterns from "
        "source to reject catch-all matchers)."
    ),
    "javascript": (
        'bdd = `npx cucumber-js --format "message:$FOUNDRY_BDD_REPORT"` exactly (double '
        "quotes: single quotes stop $FOUNDRY_BDD_REPORT expanding), with "
        "@cucumber/cucumber as a devDependency; `--format json:` is the legacy JSON "
        "report and is rejected. cucumber.json MUST be exactly:\n"
        + _CUCUMBER_JSON.format(steps="js")
        + '\nStep files go under features/step_definitions/. "forceExit": true is '
        "required: cucumber-js "
        "otherwise waits for every server, socket or timer a step left open, and the "
        "gate kills the hung run; still stop what the steps start in an AfterAll hook. In "
        "a Cucumber Expression a literal `/`, `(`, `)`, `{` or `}` MUST be escaped "
        "with a backslash (`I GET \\/health`) or use a /regex/."
    ),
    "java": (
        "cucumber-jvm writes Cucumber Messages natively: with cucumber-java + "
        "cucumber-junit-platform-engine + junit-platform-suite as test dependencies "
        'and a @Suite @IncludeEngines("cucumber") class in '
        "src/test/java/RunCucumberTest.java, bdd = `mvn -q -B test "
        "-Dtest=RunCucumberTest -Dsurefire.failIfNoSpecifiedTests=false "
        "-Dcucumber.features=features -Dcucumber.glue=<step package> "
        "-Dcucumber.plugin=message:$FOUNDRY_BDD_REPORT`."
    ),
    "csharp": (
        "Reqnroll 3.0+ writes Cucumber Messages (SpecFlow cannot): bdd = "
        '`REQNROLL_FORMATTERS="{\\"formatters\\":{\\"message\\":'
        '{\\"outputFilePath\\":\\"$FOUNDRY_BDD_REPORT\\"}}}" dotnet test '
        "<the Reqnroll test project>` with the repo-root features/*.feature files "
        "linked into that project."
    ),
}
BDD_RUNNERS["typescript"] = (
    BDD_RUNNERS["javascript"]
    .replace("bdd = `npx cucumber-js", 'bdd = `NODE_OPTIONS="--import tsx" npx cucumber-js')
    .replace(_CUCUMBER_JSON.format(steps="js"), _CUCUMBER_JSON.format(steps="ts"))
    + " TypeScript step files need the tsx loader (tsx as a devDependency): node's "
    "`--import tsx` registers it before cucumber-js imports any step file."
)


# The install a stack gets when the planner gives none: the offline build otherwise
# has no way to resolve the harness's dev-dependencies.
DEFAULT_INSTALL: dict[str, str] = {"rust": "cargo fetch"}
# A Rust install that downloads no crates (`cargo update` only resolves the lockfile)
# passes a build with no [dependencies] and leaves the offline tests without theirs.
_CARGO_DOWNLOADS = re.compile(r"\bcargo\s+(fetch|vendor)\b")


def stack_install(stack: StackDecision) -> str:
    """The install line the gates run for ``stack``: its own, with ``cargo fetch``
    added to a Rust one that downloads nothing; the language default when empty."""
    install = stack.install.strip() or DEFAULT_INSTALL.get(stack.language, "")
    if stack.language == "rust" and install and not _CARGO_DOWNLOADS.search(install):
        install = f"{install} && cargo fetch"
    return install


def bdd_runner_hint(language: str) -> str:
    """How ``language``'s standard Cucumber runner writes $FOUNDRY_BDD_REPORT ("" for
    a language with no Cucumber runner the gate reads: Python uses pytest-bdd)."""
    return BDD_RUNNERS.get(language, "")


def pytest_bdd_binds(stack: StackDecision | None) -> bool:
    """The trusted pytest-bdd harness runs the scenarios (Python with no ``bdd``)."""
    return stack is None or (stack.language == "python" and not stack.bdd)


PROFILES: dict[str, VerificationProfile] = {
    "python": VerificationProfile(
        name="python",
        python=True,
        contract_text=(
            "GATES: build = `python -m compileall -q .`; test = pytest (>=1 test "
            "must pass)." + _PY_INSTALL_CONTRACT + _BDD_CONTRACT
        ),
    ),
    "python-app": VerificationProfile(
        name="python-app",
        python=True,
        smoke_cmd=(sys.executable, "smoke.py"),
        contract_text=(
            "GATES: build = compileall; test = pytest; smoke = `python smoke.py` — "
            "the repo MUST ship smoke.py that exercises the REAL app headless "
            "(start the service and hit one real endpoint in-process, or run the "
            "CLI end-to-end on sample input) and exits 0 only on success."
            + _PY_INSTALL_CONTRACT
            + _BDD_CONTRACT
        ),
    ),
    "node-web": VerificationProfile(
        name="node-web",
        install_cmd=lockfile_tolerant_install("npm ci --no-audit --no-fund"),
        build_cmd=_sh("npm run build --if-present"),
        test_cmd=_sh("npm test --silent"),
        smoke_cmd=("node", "smoke.mjs"),
        contract_text=(
            "GATES: install = `npm ci` (the ONLY step with network); build = `npm "
            "run build`; test = `npm test`; smoke = "
            "`node smoke.mjs` (the package.json MUST define working build/test "
            "scripts, and the repo MUST ship smoke.mjs that boots the REAL app "
            "headless — start the server and hit one real route in-process, or "
            "run the CLI end-to-end — and exits 0 only on success)."
        ),
    ),
    "phaser-web": VerificationProfile(
        name="phaser-web",
        install_cmd=lockfile_tolerant_install("npm ci --no-audit --no-fund"),
        build_cmd=_sh("npm run build --if-present"),
        test_cmd=_sh("npm test --silent"),
        smoke_cmd=("node", "smoke.mjs"),
        contract_text=(
            "GATES: install = `npm ci` (the ONLY step with network); build = `npm "
            "run build`; test = `npm test`; smoke = "
            "`node smoke.mjs` — the repo MUST ship a headless smoke.mjs that boots "
            "the game loop, advances >=100 simulated ticks with scripted input, and "
            "exits 0 only if no exception was thrown."
        ),
        notes=("production swaps the smoke for Playwright + pinned Chromium",),
    ),
    "pygame": VerificationProfile(
        name="pygame",
        python=True,
        smoke_cmd=(sys.executable, "smoke.py"),
        contract_text=(
            "GATES: build = compileall; test = pytest; smoke = `python smoke.py` — "
            "the repo MUST ship smoke.py that sets SDL_VIDEODRIVER=dummy, boots the "
            "game, advances >=120 frames with scripted input, and exits 0 only if "
            "no exception was raised." + _PY_INSTALL_CONTRACT + _BDD_CONTRACT
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
        python=True,
        contract_text=(
            "GATES: build = compileall; test = pytest — the suite MUST include "
            "known-answer tests (NIST/RFC vectors) for every primitive touched, a "
            "round-trip property (encrypt->decrypt identity), and a tamper test "
            "(modified ciphertext MUST fail authentication). Use vetted primitives "
            "(the `cryptography` package / libsodium); NEVER hand-roll ciphers, "
            "modes, or KDFs; no ECB; no static IVs/nonces." + _PY_INSTALL_CONTRACT + _BDD_CONTRACT
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


# Keyword tables for inferring a stack from the request words.
_GAME_WORDS = (
    "game",
    "nes",
    "snes",
    "arcade",
    "tetris",
    "pong",
    "pixel",
    "platformer",
    "roguelike",
    "sprite",
    "tilemap",
    "stardew",
    "metroidvania",
    "shoot-em-up",
    "sokoban",
    "breakout",
    "asteroids",
)
_CRYPTO_WORDS = (
    "encrypt",
    "decrypt",
    "cipher",
    "cryptograph",
    "password manager",
    "password vault",
    "key derivation",
    "signing service",
    "e2ee",
)
_IAC_WORDS = (
    "terraform",
    "cloudformation",
    "aws infra",
    "aws infrastructure",
    "iac",
    "infrastructure as code",
    "kubernetes manifests",
    "k8s cluster",
)
_NODE_WORDS = (
    "react",
    "next.js",
    "nextjs",
    "vue",
    "svelte",
    "node.js",
    "nodejs",
    "typescript",
    "javascript",
)
_DOTNET_WORDS = ("c#", "csharp", ".net", "dotnet", "monogame", "asp.net", "xna")
_RUST_WORDS = ("rust", "cargo", "actix", "tokio", "bevy")
_JAVA_WORDS = ("java ", "java,", "java.", "spring boot", "springboot", "maven", "libgdx")
# "go" is too short to match bare — require the unambiguous forms.
_GO_WORDS = (
    "golang",
    "go api",
    "go service",
    "go server",
    "go cli",
    "go backend",
    "go microservice",
    "in go",
    "with go",
    "ebitengine",
)


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
        if any(w in low for w in _DOTNET_WORDS):  # MonoGame / C# / XNA game
            return PROFILES["dotnet"]
        if any(w in low for w in _RUST_WORDS):  # Bevy / Rust game
            return PROFILES["rust"]
        if any(w in low for w in _GO_WORDS):  # Ebitengine / Go game
            return PROFILES["go"]
        if any(w in low for w in _JAVA_WORDS):  # libGDX / Java game
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


# The language each hand-written profile builds, for a --profile pin.
_PROFILE_LANGUAGE: dict[str, str] = {
    "python": "python",
    "python-app": "python",
    "pygame": "python",
    "crypto": "python",
    "node-web": "javascript",
    "phaser-web": "javascript",
    "love2d": "lua",
    "go": "go",
    "rust": "rust",
    "dotnet": "csharp",
    "java-maven": "java",
    "iac-terraform": "terraform",
}


def _script(cmd: tuple[str, ...] | None) -> str:
    if cmd is None:
        return ""
    if len(cmd) == 3 and cmd[:2] == ("/bin/sh", "-c"):
        return cmd[2]
    return shlex.join(cmd)


def pinned_stack(name: str) -> StackDecision:
    """The stack an operator pins with ``--profile NAME``: the gates run that
    profile unchanged (``profile`` names it); the commands are for the prompts."""
    p = PROFILES[name]
    return StackDecision(
        language=_PROFILE_LANGUAGE.get(name, name),
        install=p.install_cmd or "",
        build=_script(p.build_cmd),
        test=_script(p.test_cmd),
        run=_script(p.smoke_cmd),
        profile=name,
        notes=p.contract_text,
    )


# A third-party library without stubs (flask_login) is not the project's type error.
# The CLI flag beats a model-written global `ignore_missing_imports = false` or
# `strict`, but a per-module override beats the flag, so the error codes go too.
# The source is checked --strict; test code (step definitions, conftest, tests/) is
# excluded: --exclude prunes the directories mypy walks, and a test path named on the
# command line is dropped (mypy checks a named file whatever --exclude says).
_MYPY = re.compile(
    r"((?:^|&&|;|\|\|)\s*(?:uv\s+run\s+)?(?:(?:\S*/)?python[\d.]*\s+-m\s+)?)mypy(?=\s|$)"
)
_MYPY_CALL = re.compile(_MYPY.pattern + r"([^;&|]*)")
_TEST_PATH = re.compile(
    r"(^|/)(tests?|features|step_defs)(/|$)|(^|/)(test_[^/]*|[^/]*_test|conftest)\.py$"
)
_TEST_EXCLUDE = r"(^|/)(tests?|features|step_defs)/|(^|/)(test_[^/]*|[^/]*_test|conftest)\.py$"
_MYPY_LENIENT = (
    "mypy --ignore-missing-imports --disable-error-code import-not-found "
    "--disable-error-code import-untyped"
)
_MYPY_STRICT = f"{_MYPY_LENIENT} --strict --exclude {shlex.quote(_TEST_EXCLUDE)}"


def _strict_mypy(m: re.Match[str]) -> str:
    try:
        args = shlex.split(m.group(2))
    except ValueError:
        return m.group(1) + _MYPY_STRICT + m.group(2)
    kept = [a for a in args if a.startswith("-") or not _TEST_PATH.search(a.removeprefix("./"))]
    if args and not [a for a in kept if not a.startswith("-")]:
        kept.append(".")
    tail = m.group(2)[len(m.group(2).rstrip()) :]
    return m.group(1) + " ".join([_MYPY_STRICT, *map(shlex.quote, kept)]) + tail


def python_typecheck(cmd: str) -> str:
    """A Python stack's typecheck as the gate runs it: each mypy call --strict on the
    source, with stubless third-party imports tolerated and test code left out."""
    return _MYPY_CALL.sub(_strict_mypy, cmd)


_TSC = re.compile(r"(?<![\w-])tsc(?=\s|$)")


def typescript_typecheck(cmd: str) -> str:
    """``tsc`` --strict, whatever the tsconfig says."""
    return cmd if "--strict" in cmd else _TSC.sub("tsc --strict", cmd)


_LAUNCH_CONTRACT = (
    "launch = Foundry itself starts {cmd} as a user would (an http app on $PORT/$HOST, "
    "then GET / and the entrypoint scenarios' paths; a CLI with its scenario command "
    "or --help) and FAILs on a 5xx, a refused connection, an early exit or a traceback"
)
_STEP_LINTS = frozenset({"javascript", "typescript", "go"})


def stack_profile(stack: StackDecision) -> VerificationProfile:
    """The gate commands for a planner-chosen stack.

    BUILD runs the stack's install (the only step with network) and then build and
    typecheck as one offline command, so a ticket that breaks an agreed contract
    fails the build with the type checker's own message. With a ``bdd`` command TEST
    runs ``test`` and the Cucumber runner; Python without one keeps the trusted
    pinned pytest(-bdd) harness. Python's install is not a stack command: the
    project's declared dependencies go into a per-build venv (``pydeps``)."""
    python = stack.language == "python"
    build = stack.build or (
        f"{shlex.quote(sys.executable)} -m compileall -q ." if python and stack.typecheck else ""
    )
    typecheck = (
        python_typecheck(stack.typecheck)
        if python
        else typescript_typecheck(stack.typecheck)
        if stack.language == "typescript"
        else stack.typecheck
    )
    steps = [s for s in (build, typecheck) if s]
    build_cmd = _sh(" && ".join(steps)) if steps else None
    install_line = stack_install(stack)
    install = lockfile_tolerant_install(install_line) if install_line and not python else None
    test_unavailable = ""
    if stack.bdd:
        # Scenarios are judged from the Cucumber report; `true` when only bdd is set.
        test_cmd: tuple[str, ...] | None = _sh(stack.test or "true")
    else:
        test_cmd = None if python or not stack.test else _sh(stack.test)
        if not python and not stack.test:
            test_unavailable = (
                f"the {stack.language} stack declares neither a test nor a bdd command; "
                "the pytest harness cannot test it — re-plan the stack with at least one"
            )
    gates = [
        *([f"install = `{install}` (the ONLY step with network)"] if install else []),
        f"build = `{_script(build_cmd) or 'python -m compileall -q .'}` (offline)",
        f"test = `{_script(test_cmd) or 'pytest'}`",
        *([f"bdd = `{stack.bdd}`"] if stack.bdd else []),
        f"smoke = `{stack.run}`" if stack.run else "smoke = not pinned",
        _LAUNCH_CONTRACT.format(cmd=f"`{stack.launch}`" if stack.launch else "the app's entry"),
    ]
    return VerificationProfile(
        name=f"stack:{stack.language}",
        install_cmd=install,
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        smoke_cmd=_sh(stack.run) if stack.run else None,
        contract_text="GATES: "
        + "; ".join(gates)
        + "."
        + (_PY_INSTALL_CONTRACT if python else "")
        + (_BDD_CONTRACT if python and not stack.bdd else "")
        + (
            f" BDD: {bdd_runner_hint(stack.language)}"
            if stack.bdd and bdd_runner_hint(stack.language)
            else ""
        ),
        python=python,
        bdd_cmd=stack.bdd or None,
        test_unavailable=test_unavailable,
        launch_cmd=stack.launch or None,
        launch_kind=stack.kind,
        step_lint=stack.language if stack.bdd and stack.language in _STEP_LINTS else "",
    )


def profile_examples() -> str:
    """The hand-written profiles as EXAMPLES of gate commands (planner prompt), each
    with its language's bdd command."""
    return "\n".join(
        f"- {name}: {p.contract_text}" + _bdd_example(_PROFILE_LANGUAGE.get(name, ""))
        for name, p in PROFILES.items()
    )


# One line per language for the planner's examples; the full recipe (the godog
# runner file, the cucumber-rs main) is the harness ticket's and the gate's.
_BDD_EXAMPLES: dict[str, str] = {
    "python": "leave bdd empty (the built-in pytest-bdd harness runs the scenarios)",
    "javascript": 'bdd = `npx cucumber-js --format "message:$FOUNDRY_BDD_REPORT"`',
    "go": "bdd = `go test -count=1 ./bdd` (bdd/godog_test.go: godog writing its "
    "Cucumber JSON — `cucumber:$FOUNDRY_BDD_REPORT` — the gate converts it)",
    "rust": "bdd = `cargo test --quiet --test cucumber` (tests/cucumber.rs: cucumber-rs "
    "writer::Json to $FOUNDRY_BDD_REPORT, the gate converts it)",
    "java": "bdd = `mvn -q -B test -Dtest=RunCucumberTest "
    "-Dsurefire.failIfNoSpecifiedTests=false -Dcucumber.features=features "
    "-Dcucumber.plugin=message:$FOUNDRY_BDD_REPORT`",
    "csharp": "bdd = Reqnroll 3+ `dotnet test` with REQNROLL_FORMATTERS setting the "
    "message formatter's outputFilePath to $FOUNDRY_BDD_REPORT",
    "lua": "leave bdd empty (no Cucumber runner the gate reads; scenarios not executed)",
    "terraform": "leave bdd empty (no Cucumber runner the gate reads; scenarios not executed)",
}


def _bdd_example(language: str) -> str:
    example = _BDD_EXAMPLES.get(language)
    return f" BDD: {example}." if example else ""
