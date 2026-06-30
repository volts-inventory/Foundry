"""``python -m foundry.platform "<request>" --out <dir>`` — build a platform.

A thin arg parser over :func:`foundry.platform.build_platform`. Mirrors the main
foundry CLI's posture: ``mock`` is the keyless default (skeletons, for plumbing);
pass ``--backend claude-cli`` (or ``anthropic``) for real, working code per service.
"""

from __future__ import annotations

import argparse
import sys

from foundry.platform.orchestrator import DEFAULT_MAX_PARALLEL, build_platform
from foundry.platform.planner import plan_platform


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m foundry.platform",
        description="Build a MULTI-SERVICE platform (web + API + backend + data + worker) "
                    "by composing per-service Foundry builds into one monorepo.",
    )
    p.add_argument("request", nargs="?", help="one-line platform request")
    p.add_argument("--out", help="write the stitched monorepo here")
    p.add_argument("--backend", default="mock",
                   help="model egress: mock (keyless skeletons) | claude-cli | anthropic | "
                        "openai | codex  (default: mock — use claude-cli for real code)")
    p.add_argument("--mode", default="assistant",
                   help="per-service trust mode: assistant | gated | autonomous (def assistant)")
    p.add_argument("--name", help="platform / monorepo name (default: slug of request)")
    p.add_argument("--services", help="comma-separated subset of roles to build "
                                      "(web,api,backend,data,worker); default: inferred")
    p.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL,
                   help=f"services built concurrently (default: {DEFAULT_MAX_PARALLEL})")
    p.add_argument("--max-slots-per-service", type=int, default=6,
                   help="task parallelism inside each service build (default: 6)")
    p.add_argument("--passes", default="auto",
                   help="deepening per service: 'auto' (default) keeps extending each "
                        "service until it passes its gates AND stops growing (so simple "
                        "services stop early, complex ones run longer); or a fixed integer")
    p.add_argument("--max-passes", type=int, default=6,
                   help="failsafe ceiling on auto deepening passes per service (default: 6)")
    p.add_argument("--no-critic", action="store_true",
                   help="disable the LLM completeness critic; auto-deepening then stops "
                        "on the structural plateau signal alone (mock always skips it)")
    p.add_argument("--cost-cap-microusd", type=int, default=None,
                   help="per-service spend envelope in micro-USD (metered backends)")
    p.add_argument("--plan-only", action="store_true",
                   help="print the decomposition and exit (no build)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.request:
        _parser().print_help()
        return 2
    only = [s for s in (args.services or "").split(",") if s.strip()] or None
    passes: int | str = args.passes if args.passes == "auto" else int(args.passes)

    if args.plan_only:
        plan = plan_platform(args.request, name=args.name, only=only)
        print(f"platform: {plan.name}")
        print(f"request:  {plan.request!r}\nservices:")
        for s in plan.services:
            deps = f"  → {', '.join(s.depends_on)}" if s.depends_on else ""
            print(f"  - {s.name:<10} [{s.role}]{deps}")
            print(f"      {s.request}")
        return 0

    if not args.out:
        print("error: --out is required to build (use --plan-only to preview)", file=sys.stderr)
        return 2
    if args.backend == "mock":
        print("note: --backend mock writes SKELETONS only; use --backend claude-cli "
              "for real, working code per service.", file=sys.stderr)

    result = build_platform(
        args.request,
        out_dir=args.out,
        backend=args.backend,
        mode=args.mode,
        name=args.name,
        only=only,
        max_parallel=args.max_parallel,
        max_slots_per_service=args.max_slots_per_service,
        cost_cap_microusd=args.cost_cap_microusd,
        passes=passes,
        max_passes=args.max_passes,
        critic=not args.no_critic,
    )
    print(result.summary())
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
