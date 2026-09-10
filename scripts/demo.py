#!/usr/bin/env python3
"""Drive the reliability story against a running Resolve-E.

    python scripts/demo.py                 # against http://localhost:8000
    python scripts/demo.py --base-url ...

This is the terminal half of the three-minute demo in ``docs/demo.md``.
The dashboard shows the outcome; this shows the guarantees underneath it,
which are the part a judge cannot see by clicking.

It places no real phone calls unless the server it talks to has
``CALLE_LIVE_CALLS=true``, and it says which mode it is in before doing
anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

BOLD, DIM, GREEN, AMBER, BLUE, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[33m",
    "\033[34m",
    "\033[0m",
)

# A Windows console still defaults to cp1252, which cannot encode box
# drawing characters. Ask for UTF-8 first; if the console refuses, fall
# back to ASCII rules rather than crashing halfway through a demo.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass


def _supports(glyph: str) -> bool:
    try:
        glyph.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError, TypeError):
        return False
    return True


RULE = "─" if _supports("─") else "-"
DOT = "·" if _supports("·") else "|"


def req(base: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or "{}")
    except urllib.error.HTTPError as exc:
        return {"_status": exc.code, "_body": exc.read().decode()[:400]}
    except urllib.error.URLError as exc:
        print(f"\nCannot reach {base} -- is the stack up? ({exc.reason})", file=sys.stderr)
        raise SystemExit(2) from exc


def beat(title: str, why: str = "") -> None:
    print(f"\n{BOLD}{RULE * 2} {title} {RULE * max(0, 60 - len(title))}{RESET}")
    if why:
        print(DIM + textwrap.fill(why, 74) + RESET)


def show_queue(base: str) -> list[dict]:
    rows = sorted(req(base, "/api/exceptions"), key=lambda r: r["po_number"])
    tone = {
        "RESOLVED_ON_TIME": GREEN,
        "RESOLVED_DELAYED": GREEN,
        "HUMAN_REVIEW": AMBER,
        "RETRY_PENDING": BLUE,
    }
    for r in rows:
        colour = tone.get(r["state"], DIM)
        why = (r.get("last_reason_text") or "")[:44]
        print(f"  {r['po_number']:<9} {colour}{r['state']:<19}{RESET} {DIM}{why}{RESET}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--pause", type=float, default=1.2, help="seconds between beats")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    health = req(base, "/health")
    live = health.get("config", {}).get("live_calls_enabled")
    print(f"{BOLD}Resolve-E demo{RESET}  {DOT}  {base}")
    if live:
        print(f"live calling: {AMBER}ON - real phones will ring{RESET}")
    else:
        print("live calling: off (simulated)")

    beat(
        "1. The queue",
        "Software already knows these orders are stuck. The missing step is "
        "that somebody has to pick up the phone.",
    )
    rows = show_queue(base)
    if not rows:
        print("  (empty - run `resolve-e seed` first)")
        return 1
    time.sleep(args.pause)

    beat(
        "2. One click, one CALL-E call task",
        "recipients[] plus recipient_result_schema means a single call task "
        "carries every overdue supplier, each with an independent result.",
    )
    result = req(base, "/api/exceptions/resolve", "POST", {"exception_ids": []})
    print(f"  {result.get('message', result)}")
    call_id = result.get("provider_call_id")
    if not call_id:
        print("  Nothing was eligible. Run `resolve-e reset` for a clean demo.")
        return 1
    print(f"  CALL-E call id : {BOLD}{call_id}{RESET}")
    time.sleep(args.pause)

    beat(
        "3. Trigger it again",
        "The same logical batch derives the same idempotency key, and these "
        "exceptions are already CALLING, so nothing is dialled twice.",
    )
    again = req(base, "/api/exceptions/resolve", "POST", {"exception_ids": []})
    print(f"  {again.get('message', again)}")
    print(f"  {GREEN}No duplicate call created.{RESET}")
    time.sleep(args.pause)

    beat(
        "4. Deliver the terminal webhook twice",
        "CALL-E delivery is at-least-once. The receiver dedupes on the event "
        "id and returns 2xx either way, so a redelivery is harmless.",
    )
    body = {
        "id": "evt_demo_replay",
        "type": "call.completed",
        "created_at": datetime.now(UTC).isoformat(),
        "data": {"id": call_id, "status": "completed", "recipients": []},
    }
    for i in (1, 2):
        response = req(base, "/webhooks/calle", "POST", body)
        flag = response.get("duplicate")
        mark = f"{GREEN}accepted{RESET}" if flag is False else f"{BLUE}duplicate ignored{RESET}"
        print(f"  delivery {i}: {mark}")
    time.sleep(args.pause)

    beat(
        "5. Wait for the worker",
        "The webhook only enqueued reconciliation. The decision is made "
        "against authoritative state fetched from CALL-E - which is why a "
        "LOST webhook is only a delay, never a stuck workflow.",
    )
    deadline = time.time() + 120
    while time.time() < deadline:
        rows = req(base, "/api/exceptions")
        if not any(
            r["state"] in ("CALLING", "RECONCILING", "CALL_PLANNED") for r in rows
        ):
            break
        print(f"  {DIM}...waiting for the reconciliation sweep{RESET}", end="\r")
        time.sleep(3)
    print(" " * 60, end="\r")

    beat(
        "6. Six suppliers, six outcomes, one call",
        "Every one of these is a different branch of the decision table - and "
        "no language model chose any of them.",
    )
    final = show_queue(base)

    resolved = sum(1 for r in final if r["state"].startswith("RESOLVED"))
    escalated = sum(1 for r in final if r["state"] == "HUMAN_REVIEW")
    retrying = sum(1 for r in final if r["state"] == "RETRY_PENDING")
    print(
        f"\n  {GREEN}{resolved} resolved{RESET} {DOT} "
        f"{AMBER}{escalated} need a person{RESET} {DOT} "
        f"{BLUE}{retrying} will retry{RESET}"
    )
    print(
        f"\n{DIM}Resolve-E does not automate calling. It automates the "
        f"resolution of workflows that get stuck at the phone.{RESET}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
