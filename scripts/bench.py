"""Benchmark a Foundry build: run fixed cases, then summarize calls, tokens, stages, outcome."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from foundry.proxy.model_provider_proxy import (
    ANSWER_COUNT_PREFIX,
    EFFORT_COUNT_PREFIX,
    THINK_COUNT_PREFIX,
)

CASES = {
    "dice": "a Python command-line tool that rolls dice given notation like 2d6+3",
    "todo": "a small Python REST API for a todo list with create, list, complete and delete",
    "csv": (
        "a Python command-line tool that prints summary statistics "
        "for each numeric column of a CSV file"
    ),
    "node": "a small Node.js web app (Express) for a shopping list with add, check off and clear",
    "go": "a Go command-line tool that counts word frequencies in text files and prints the top N",
    "rust": "a Rust command-line tool that converts temperatures between C, F and Kelvin",
    "notes": (
        "a Python web app for personal notes: user sign-up and login with hashed passwords, "
        "each user can create, edit, search and delete only their own notes, stored in SQLite"
    ),
    "shop": (
        "a Python web shop: customer accounts with login, a product catalog with search, "
        "a cart, checkout that records orders and decrements stock, order history, "
        "and an admin area to manage products and view orders, stored in SQLite"
    ),
}

START = re.compile(r"start model call: (\S+) \((\w+), ([^)]*)\)")
REUSE = re.compile(r"reuse model call: ")
DONE = re.compile(r"done\s+model call: (\S+) \([^)]*\) in ([\d.]+)([sm])\b")
STAGE = re.compile(r"·\s+\S+\s+▸\s+(\w+)")
RUN_MARK = "=== foundry bench run ==="
VERDICTS = {0: "pass", 1: "fail", 3: "stopped", 2: "usage-error"}
HINT_COUNTS = "hint_counts.json"


def run_case(
    mode: str,
    backend: str,
    case: str,
    cap: int,
    out_dir: Path,
    scope: str = "demo",
    plan_reuse: bool = False,
) -> None:
    root = out_dir / mode / case
    root.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "foundry.cli", CASES[case],
        "--backend", backend, "--model-mode", mode, "--scope", scope,
        "--time-cap", str(cap), "--out", str(root / "out"), "--state-dir", str(root / "state"),
    ]  # fmt: skip
    # A bench measures planning: a checked plan from an earlier round in this state
    # dir is replayed only on --plan-reuse, whatever the caller's env says.
    env = {**os.environ, "FOUNDRY_PROGRESS": "1", "FOUNDRY_PLAN_REUSE": "1" if plan_reuse else "0"}
    # Rerunning a case resumes its saved state, so its log and wall time carry on.
    meta = root / "run.json"
    prior = json.loads(meta.read_text()) if meta.exists() else {}
    # Each CLI process writes only its own counts: a stale file from an earlier
    # segment would be summed twice if this one dies before writing.
    for stale in (root / "state" / "calls").glob(f"*/{HINT_COUNTS}"):
        stale.unlink()
    start = time.monotonic()
    with (root / "run.log").open("a") as log:
        log.write(f"\n{RUN_MARK}\n")
        log.flush()
        code = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
    meta.write_text(
        json.dumps(
            {
                "wall_s": round(prior.get("wall_s", 0.0) + time.monotonic() - start, 1),
                "exit_code": code,
                "hint_counts": add_counts(prior.get("hint_counts") or {}, root / "state"),
            }
        )
    )


def add_counts(prior: dict[str, int], state: Path) -> dict[str, int]:
    total: Counter[str] = Counter(prior)
    for path in (state / "calls").glob(f"*/{HINT_COUNTS}"):
        try:
            total.update(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return dict(sorted(total.items()))


def sum_tokens(state: Path) -> dict[str, int]:
    tin = tout = 0
    for path in (state / "calls").glob("*/*.json"):
        try:
            usage = json.loads(path.read_text()).get("usage")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(usage, dict):
            tin += int(usage.get("input_tokens") or 0)
            tout += int(usage.get("output_tokens") or 0)
    return {"input": tin, "output": tout}


def summarize_case(root: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if (root / "run.json").exists():
        meta = json.loads((root / "run.json").read_text())
    log = (root / "run.log").read_text(errors="replace") if (root / "run.log").exists() else ""
    last_run = log.rsplit(RUN_MARK, 1)[-1]
    by_node: Counter[str] = Counter()
    by_tier: Counter[str] = Counter()
    secs_by_node: dict[str, float] = {}
    stages: Counter[str] = Counter()
    reused = 0
    for line in log.splitlines():
        if m := START.search(line):
            by_node[m[1]] += 1
            by_tier[m[2]] += 1
        elif REUSE.search(line):
            reused += 1
        elif m := DONE.search(line):
            secs_by_node[m[1]] = secs_by_node.get(m[1], 0.0) + float(m[2]) * (
                60 if m[3] == "m" else 1
            )
        elif m := STAGE.search(line):
            stages[m[1]] += 1
    out = root / "out"
    files = (
        [p for p in out.rglob("*") if p.is_file() and ".git" not in p.relative_to(out).parts]
        if out.is_dir()
        else []
    )
    code = meta.get("exit_code")
    counts = meta.get("hint_counts") or {}
    return {
        "wall_s": meta.get("wall_s"),
        "exit_code": code,
        "stopped_by_time_cap": meta.get("exit_code") == 3 and "time cap reached" in last_run,
        "model_calls": sum(by_node.values()),
        "calls_by_node": dict(by_node),
        "calls_by_tier": dict(by_tier),
        "reused_calls": reused,
        "call_seconds_by_node": {k: round(v, 1) for k, v in secs_by_node.items()},
        "tokens": sum_tokens(root / "state"),
        "fix_passes": stages["fixer"],
        "wave_gates": stages["wave_gate"],
        "out_files": len(files),
        "verdict": VERDICTS.get(code) if code is not None else None,
        "released": bool(files) and code == 0,
        "calls_by_effort": {
            k.removeprefix(EFFORT_COUNT_PREFIX): v
            for k, v in counts.items()
            if k.startswith(EFFORT_COUNT_PREFIX)
        },
        "think_tokens_by_node": _by_node(counts, THINK_COUNT_PREFIX),
        "answer_tokens_by_node": _by_node(counts, ANSWER_COUNT_PREFIX),
        "hint_counts": {k: v for k, v in counts.items() if not k.startswith(_SPLIT_PREFIXES)},
    }


_SPLIT_PREFIXES = (EFFORT_COUNT_PREFIX, THINK_COUNT_PREFIX, ANSWER_COUNT_PREFIX)


def _by_node(counts: dict[str, int], prefix: str) -> dict[str, int]:
    return {k.removeprefix(prefix): v for k, v in counts.items() if k.startswith(prefix)}


def render(summary: dict[str, dict[str, Any]]) -> str:
    head = (
        f"{'case':<6} {'wall_m':>7} {'exit':>4} {'calls':>5} {'reuse':>5} {'tok_in':>9} "
        f"{'tok_out':>8} {'fix':>3} {'wg':>3} {'files':>5} {'verdict':<8} rel"
    )
    rows = [head]
    for case, s in summary.items():
        wall = f"{s['wall_s'] / 60:.1f}" if s["wall_s"] is not None else "-"
        rows.append(
            f"{case:<6} {wall:>7} {s['exit_code'] if s['exit_code'] is not None else '-':>4} "
            f"{s['model_calls']:>5} {s['reused_calls']:>5} {s['tokens']['input']:>9} "
            f"{s['tokens']['output']:>8} {s['fix_passes']:>3} {s['wave_gates']:>3} "
            f"{s['out_files']:>5} {s['verdict'] or '-':<8} {'Y' if s['released'] else 'N'}"
            f"{' (time cap)' if s['stopped_by_time_cap'] else ''}"
        )
    for case, s in summary.items():
        counts = s.get("hint_counts") or {}
        fired = ", ".join(f"{k}={v}" for k, v in counts.items()) or "none"
        rows.append(f"{case} hints: {fired}")
        if efforts := s.get("calls_by_effort"):
            rows.append(f"{case} effort: " + ", ".join(f"{k}={v}" for k, v in efforts.items()))
        think = s.get("think_tokens_by_node") or {}
        answer = s.get("answer_tokens_by_node") or {}
        if think or answer:
            nodes = sorted(think.keys() | answer.keys())
            rows.append(
                f"{case} tokens: "
                + ", ".join(f"{n} think={think.get(n, 0)}/answer={answer.get(n, 0)}" for n in nodes)
            )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["local", "hybrid", "cloud"], required=True)
    ap.add_argument("--backend", default="claude-cli")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--cases", default="dice,todo,csv")
    ap.add_argument("--scope", choices=["auto", "demo", "standard"], default="demo")
    ap.add_argument(
        "--time-cap",
        type=int,
        default=None,
        help="minutes per case (default: 120 cloud, 600 with a local model)",
    )
    ap.add_argument("--summarize-only", action="store_true")
    ap.add_argument(
        "--plan-reuse",
        action="store_true",
        help="replay a checked plan from an earlier round (off: a bench measures planning)",
    )
    args = ap.parse_args(argv)
    cases = [c for c in args.cases.split(",") if c]
    unknown = [c for c in cases if c not in CASES]
    if unknown:
        ap.error(f"unknown cases: {', '.join(unknown)}")
    if args.time_cap is None:
        args.time_cap = 120 if args.mode == "cloud" else 600
    if not args.summarize_only:
        for case in cases:
            run_case(
                args.mode,
                args.backend,
                case,
                args.time_cap,
                args.out_dir,
                args.scope,
                args.plan_reuse,
            )
    mode_dir = args.out_dir / args.mode
    summary = {c: summarize_case(mode_dir / c) for c in cases if (mode_dir / c).is_dir()}
    mode_dir.mkdir(parents=True, exist_ok=True)
    (mode_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
