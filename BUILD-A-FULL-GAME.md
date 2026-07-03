# Build a full game — the complete recipe

The end-to-end runbook for producing a **complete, playable, Stardew-style
game** with Foundry on a Claude Max plan (`claude -p`, no API billing). Two
commands, run back-to-back, fully unattended. Works the same with
`ANTHROPIC_API_KEY` (metered) — no command changes.

**Prerequisites:** the [Setup](README.md#1-setup) steps done once, the
`claude` CLI installed and authenticated (`claude /login`, or
`CLAUDE_CODE_OAUTH_TOKEN` in `.env` for a headless box).

---

## 1. Write the spec — "full" is whatever you write here

The request line picks the stack; the **spec defines completeness**. The
acceptance criteria, the milestone ladder, and the gates all derive from it —
the richer this file, the fuller the game.

```bash
cat > ~/moon-spec.md <<'EOF'
A complete stardew-valley-style 2D farming/life sim in pygame, set on the moon.
Must include: tile-based moon surface with a scrolling camera; day/night + sol
cycle; player movement and tool use; tilling, planting, watering, and harvesting
at least 6 crop types with growth stages; oxygen/energy management; a colony hub
with at least 4 NPCs with daily schedules and dialog; an economy (sell crops,
buy seeds/upgrades at a store); base-building (place/upgrade modules);
save/load with 3 slots; a title screen, pause menu, and controls screen;
sound hooks (procedural or silent-safe); keyboard controls documented in-game.
Code-drawn graphics (shapes/procedural tiles) are fine — no external assets.
EOF
```

> Asset generation (sprites/audio files) is a deliberate roadmap gap — ask for
> **code-drawn / procedural graphics** as above and the game is fully playable
> without it.

## 2. Build the whole game — the milestone ladder

```bash
python -m foundry.cli milestones \
  "a complete stardew-style farming game about colonizing the moon, pygame" \
  --spec ~/moon-spec.md --out ~/moon-colony \
  --backend claude-cli --adapter claude-code --mode autonomous \
  --max-milestones 10 --max-slots 4 --max-fix-attempts 8 \
  --state-dir ~/foundry/state --thread-id moon-colony
```

The output IS a git repository: `~/moon-colony` with one tagged commit per
milestone (`git diff m04..m05`, `git checkout m03` to play an earlier
increment, push it anywhere). The per-increment trees stage under
`~/moon-colony/.stages/` (durable — resume needs them; excluded from the repo
tree). Pass `--no-git` for plain sibling `mNN` folders instead (no git needed).

What happens, unattended:

- the model **plans the ladder** (engine loop → world/tilemap → farming →
  NPCs/dialog → economy → base-building → save/load → polish);
- each milestone runs the **full gated factory**: agentic `claude` sessions
  implement the tasks inside sandboxes (each task sees the spec, its
  acceptance criteria, and its predecessors' code), and the request selects the
  **pygame verification profile** — build (compileall), tests (pytest), and a
  runtime **smoke gate** (`python smoke.py`: the game must boot headless with
  `SDL_VIDEODRIVER=dummy` and survive ≥120 frames, or the boundary routes back
  to fix with the failure in the next attempt's prompt);
- each **released** increment becomes a tagged commit (staged under
  `~/moon-colony/.stages/mNN`) and seeds the next
  milestone; the ladder **stops fail-closed** at the first milestone the gates
  refuse — everything below it is still a released, working game.

Expect **hours of wall-clock and a meaningful chunk of Max quota** — start it
in the evening. Cost on the subscription path is plan usage, not dollars;
turn budgets + wall-clock hard-kills bound each task.

## 3. Keep it growing — autopilot (Director + design panel)

```bash
python -m foundry.cli autopilot \
  "a complete stardew-style farming game about colonizing the moon, pygame" \
  --out ~/moon-colony-plus --backend claude-cli --adapter claude-code \
  --mode autonomous --iterations 5 --state-dir ~/foundry/state
```

Between releases, a **Director** proposes 3 candidate next actions and a
**panel** — staff software engineer, usability designer, product/game
designer, QA lead — scores each 0-10 with optional vetoes and amendments. The
tally is deterministic (majority veto kills; highest mean above the floor
wins), the winner builds next, and the ballot is recorded on each release. If
the panel declines everything, the loop stops and hands steering back to you.
Like the milestone ladder, the output is one git repository (tagged `i01`…).

## 4. Play it

```bash
cd ~/moon-colony            # the repo HEAD is the latest release
# (or: git checkout m03 — play any earlier increment)
pip install pygame
python main.py
```

---

## Variants

**Gated, with you approving each idea (Workshop UI):** start
`python -m webapp.server`, create the project with backend `claude-cli`
(substrate auto-selects `claude-code`), packs `game`, mode **gated**,
Supervised on, **Autopilot: 5**. Each panel pick pauses as an **AUTOPILOT
PROPOSAL** checkpoint — approve it, **edit the request in place** and approve
your version, or reject to stop the loop. Stage checkpoints and the
playability sign-off surface as usual; walking away auto-approves, so the AI
keeps driving either way.

**Explicit ladder instead of a planned one:**

```bash
python -m foundry.cli milestones "…the same request…" --out ~/moon-colony \
  --backend claude-cli --mode autonomous --state-dir ~/foundry/state \
  --milestones "engine loop + player movement; tilemap world + camera; \
farming (6 crops, growth stages); oxygen/energy systems; NPCs + dialog; \
economy + store; base building; save/load + menus"
```

**Crashed or interrupted?** Re-run the *identical* command —
`--state-dir` + the thread id resume at the milestone (or checkpoint) the run
died in. Milestones already released are never rebuilt.

**First time?** Validate the pipeline cheaply before an overnight run — the
benchmark ladder's rung 1 is designed for exactly this (public test ROMs make
the output objectively scoreable):

```bash
python -m foundry.cli milestones "a CHIP-8 emulator in python with pytest \
tests against public test ROMs" --out ~/chip8 --backend claude-cli \
--mode autonomous --state-dir ~/foundry/state
```

---

## What to expect from the first real run

The pipeline is fully implemented and harness-verified (2,000+ tests), but the
first real-model build is the true acceptance test. Likely rough edges: a
milestone scoped too big (the fix loop absorbs some of this; `--max-fix-attempts`
bounds it), a forgotten `smoke.py` on an early milestone (the smoke gate
refuses, the failure feeds the retry), and taste — pacing and feel are yours
to steer. Use the gated variant for the first run if you want to watch it
work; use steering directives ("no placeholder art", "cozy pacing") to pin
preferences across all future iterations.
