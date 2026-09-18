"""Turn a live call into a permanent regression case.

    python -m evals.from_log logs/<call>.jsonl --id no-then-evening --expect evening
    python -m evals --suite regressions

The patient's lines are taken from the call log and said again, in order, by
the simulated caller, each where it fits the agent as it is now. The
expectation is what should have happened on that call, so a regression checks
the outcome, not the path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evals.personas import Outcome, Persona, booked, callback_queued, nothing_booked

FILE = Path(__file__).parent / "regressions.json"

EXPECT: dict[str, list[Outcome]] = {
    "booked": [booked(lambda d: True, "any time")],
    "evening": [booked(lambda d: d.hour >= 15, "in the clinic's last hours, 3 to 5 PM")],
    "morning": [booked(lambda d: d.hour < 12, "in the morning")],
    "callback": [nothing_booked, callback_queued()],
    "nothing": [nothing_booked],
}


def lines_from_log(path: Path) -> tuple[str, list[str]]:
    """(patient id, what the caller said, in order)."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    start = next(r for r in rows if r["event"] == "call_start")
    return start["patient_id"], [r["text"] for r in rows if r["event"] == "user_turn_committed" and r.get("text")]


def load() -> list[dict[str, Any]]:
    return json.loads(FILE.read_text()) if FILE.exists() else []


_REPLAY = ("You are {{name}}. On an earlier call you said the lines below, in this order. Say each "
           "one, word for word, when it answers what the caller has just asked; skip a line that no "
           "longer fits, and say nothing else of your own unless you must answer a question.\n{lines}")


def case(regression_id: str) -> Persona:
    """A live call's lines, said by the simulated caller in order wherever each
    fits. A fixed script could not follow a changed flow: after the rework,
    "No." to "any preference?" landed on "would you like an appointment?"."""
    r = next(x for x in load() if x["id"] == regression_id)
    lines = "\n".join(f"- {line}" for line in r["lines"]).replace("{", "{{").replace("}", "}}")
    return Persona(f"G:{r['id']}", f"replay {r['id']}", r.get("answerer", "patient"),
                   brief=_REPLAY.format(lines=lines), valid_if="", outcomes=EXPECT[r["expect"]],
                   suite="regressions", point=r["id"], max_turns=len(r["lines"]) + 4)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.from_log")
    parser.add_argument("log", type=Path)
    parser.add_argument("--id", required=True, help="a short name, e.g. no-then-evening")
    parser.add_argument("--expect", required=True, choices=sorted(EXPECT))
    parser.add_argument("--note", default="", help="what went wrong on the live call")
    args = parser.parse_args()

    patient_id, lines = lines_from_log(args.log)
    cases = [x for x in load() if x["id"] != args.id]
    cases.append({"id": args.id, "patient_id": patient_id, "expect": args.expect, "note": args.note,
                  "source": args.log.name, "lines": lines})
    FILE.write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n")
    print(f"Saved {args.id}: {len(lines)} lines, expect {args.expect}. Run: python -m evals --suite regressions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
