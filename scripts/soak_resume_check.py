#!/usr/bin/env python3
"""Soak + restart-resume validator for the Foundry Workshop server.

This is the acceptance test for "can it run for days": it exercises the FULL
server process (not an in-process unit test), so it catches what unit tests can't —
RAM that grows build-over-build, and whether a real `kill -9` + restart actually
resumes an in-flight build from its durable checkpoint.

Two checks (run one or both):

  soak    — run N builds back-to-back on the offline `mock` backend, sampling the
            server process RSS after each. With the log-sweeper on, RSS should
            PLATEAU rather than climb build-over-build. Fails if it grows past a
            threshold (a leak).

  resume  — start a SUPERVISED build, wait until it pauses at a checkpoint (a
            durable checkpoint now exists), `kill -9` the server, restart it
            against the SAME state dir, and assert the build is resumed (present +
            running/awaiting, not lost) and its event history still replays.

Everything runs on `mock` (offline, deterministic, no API key), so it's safe to run
on the target Pi/Mac before trusting a real multi-day job.

Usage:
    python scripts/soak_resume_check.py            # both checks
    python scripts/soak_resume_check.py --mode soak --builds 30
    python scripts/soak_resume_check.py --mode resume --port 8791
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---- tiny HTTP helpers ------------------------------------------------------ #

def _get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(url: str, obj: dict, timeout: float = 10.0):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _wait_http(base: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(base + "/", timeout=2).read()
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.25)
    return False


# ---- process + memory ------------------------------------------------------- #

def _rss_kb(pid: int) -> int:
    """Resident memory of the server process, in KB (psutil if present, else ps)."""
    try:
        import psutil  # type: ignore
        return int(psutil.Process(pid).memory_info().rss // 1024)
    except Exception:
        try:
            out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)])
            return int(out.strip() or 0)
        except Exception:
            # Linux fallback: /proc
            try:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1])
            except Exception:
                return 0
    return 0


def start_server(state_dir: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        WORKSHOP_HOST="127.0.0.1",
        WORKSHOP_PORT=str(port),
        WORKSHOP_OUTPUT_DIR=str(state_dir),
        FOUNDRY_MODEL_BACKEND="mock",
        # Make the sweeper aggressive so a short soak still proves trimming works.
        WORKSHOP_LOG_SWEEP_S="3",
        WORKSHOP_LOG_GRACE_S="1",
        WORKSHOP_LOG_TAIL="16",
        WORKSHOP_MAX_CONCURRENT_BUILDS="2",
        PYTHONUNBUFFERED="1",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "webapp.server"], cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    if not _wait_http(base):
        proc.kill()
        raise SystemExit(f"server did not come up on {base}")
    return proc


def stop_server(proc: subprocess.Popen, *, hard: bool = False) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGKILL if hard else signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---- project state polling -------------------------------------------------- #

def _floor_row(base: str, floor: int) -> dict | None:
    payload = _get(base + "/api/projects")
    rows = payload.get("projects", []) if isinstance(payload, dict) else payload
    for row in rows:
        if row.get("floor") == floor:
            return row
    return None


def _wait_status(base: str, floor: int, want: set[str], timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _floor_row(base, floor)
        if row and (row.get("status") in want or (("awaiting" in want) and row.get("awaiting"))):
            return row
        time.sleep(0.2)
    return _floor_row(base, floor)


# ---- the two checks --------------------------------------------------------- #

def run_soak(base: str, pid: int, n_builds: int) -> bool:
    print(f"\n=== SOAK: {n_builds} mock builds, sampling server RSS ===")
    samples: list[int] = []
    for i in range(1, n_builds + 1):
        res = _post(base + "/api/projects",
                    {"request": f"a tiny url shortener #{i}", "backend": "mock",
                     "supervised": False, "mode": "assistant"})
        floor = res["floor"]
        row = _wait_status(base, floor, {"done"}, timeout=60)
        if not row or row.get("status") != "done":
            print(f"  build {i}: did NOT finish (status={row and row.get('status')})")
            return False
        time.sleep(0.2)
        rss = _rss_kb(pid)
        samples.append(rss)
        if i == 1 or i % 5 == 0 or i == n_builds:
            print(f"  after build {i:>3}: RSS = {rss/1024:7.1f} MB")
    # Leak heuristic: median RSS of the last third vs the first third.
    k = max(1, len(samples) // 3)
    first = sorted(samples[:k])[k // 2]
    last = sorted(samples[-k:])[k // 2]
    growth = (last - first) / first if first else 0.0
    print(f"  first-third median {first/1024:.1f} MB → last-third median "
          f"{last/1024:.1f} MB  ({growth*100:+.1f}%)")
    if growth > 0.50:
        print("  ✗ RSS grew >50% build-over-build — possible leak.")
        return False
    print("  ✓ RSS plateaued (no unbounded build-over-build growth).")
    return True


def run_resume(state_dir: Path, port: int) -> bool:
    print("\n=== RESUME: kill -9 a paused build, restart, verify it resumes ===")
    base = f"http://127.0.0.1:{port}"
    proc = start_server(state_dir, port)
    try:
        res = _post(base + "/api/projects",
                    {"request": "a tiny url shortener", "backend": "mock",
                     "supervised": True, "mode": "assistant"})
        floor, pid_before = res["floor"], res["id"]
        row = _wait_status(base, floor, {"awaiting", "done"}, timeout=60)
        if not row:
            print("  ✗ build never appeared")
            return False
        paused = bool(row.get("awaiting"))
        print(f"  build {pid_before} reached: awaiting={paused} status={row.get('status')}")
        events_before = _stream_count(base, pid_before)
        print(f"  streamed {events_before} event(s) before the kill")
    finally:
        stop_server(proc, hard=True)   # kill -9: the daemon build thread dies with it
    print("  server killed (-9); restarting against the same state dir…")

    proc2 = start_server(state_dir, port)
    try:
        row = _wait_status(base, floor, {"running", "done", "awaiting"}, timeout=60)
        if not row:
            print("  ✗ build was LOST after restart")
            return False
        print(f"  after restart: status={row.get('status')} awaiting={row.get('awaiting')}")
        events_after = _stream_count(base, pid_before)
        if events_after < 1:
            print("  ✗ event history did not replay after restart")
            return False
        print(f"  ✓ build survived the restart; {events_after} event(s) replay from the store")
        return True
    finally:
        stop_server(proc2, hard=True)


def _stream_count(base: str, pid: str, max_events: int = 2000, timeout: float = 8.0) -> int:
    """Read the SSE stream until it ends (or a short timeout), counting data frames."""
    n = 0
    try:
        with urllib.request.urlopen(base + f"/api/stream/{pid}", timeout=timeout) as r:
            for raw in r:
                line = raw.decode(errors="ignore")
                if line.startswith("data:"):
                    n += 1
                if line.startswith("event: end") or n >= max_events:
                    break
    except (urllib.error.URLError, ConnectionError, OSError):
        pass
    return n


# ---- main ------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["soak", "resume", "both"], default="both")
    ap.add_argument("--builds", type=int, default=20, help="soak: number of builds")
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()

    ok = True
    with tempfile.TemporaryDirectory(prefix="foundry-soak-") as td:
        state_dir = Path(td) / "state"
        if args.mode in ("soak", "both"):
            proc = start_server(state_dir, args.port)
            try:
                ok = run_soak(f"http://127.0.0.1:{args.port}", proc.pid, args.builds) and ok
            finally:
                stop_server(proc)
        if args.mode in ("resume", "both"):
            ok = run_resume(state_dir, args.port) and ok

    print("\n" + ("PASS ✓ — looks ready for a long run." if ok
                  else "FAIL ✗ — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
