"""Runs one conversation between the real agent and a simulated patient.

Runs inside a worker process whose CLINIC_DB_PATH points at its own database,
so conversations in parallel never see each other's bookings.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from livekit.agents import AgentSession

import agent as agent_module
import booking
import config
import db
import post_call
from call_log import NullLog
from evals import checks
from evals.checks import Turn


class ListLog(NullLog):
    """Keeps the agent's call-log events in memory so turns can be inspected."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []

    def event(self, kind: str, /, **fields: Any) -> float:
        self.rows.append({"event": kind, **fields})
        return self.elapsed_ms()


@dataclass
class Result:
    id: str
    name: str
    kind: str
    run: int
    patient_id: str = ""
    status: str = "pass"                 # pass, fail, invalid, crash
    errors: list[str] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)
    agent_tokens: list[int] = field(default_factory=lambda: [0, 0])
    sim_tokens: list[int] = field(default_factory=lambda: [0, 0])
    seconds: float = 0.0
    analysis: dict[str, Any] | None = None
    analysis_checks: dict[str, bool | None] = field(default_factory=dict)  # booking_fact, outcome
    facts: dict[str, Any] = field(default_factory=dict)  # what the scorecard counts; see _facts


def _reset_db() -> None:
    with db.session() as conn:
        conn.execute("DELETE FROM appointments")
        conn.execute("DELETE FROM callbacks")
        db.init_db(conn)


def _db_state(patient_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with db.session() as conn:
        appts = [dict(r) for r in conn.execute(
            "SELECT * FROM appointments WHERE patient_id=? AND status='booked' AND reference != 'ADT-EXIST'",
            (patient_id,))]
        cbs = [dict(r) for r in conn.execute("SELECT * FROM callbacks WHERE patient_id=?", (patient_id,))]
    return appts, cbs


async def _agent_turn(session: AgentSession, log: ListLog, patient_id: str, say: str,
                      result: Result) -> Turn:
    before = len(log.rows)
    res = await session.run(user_input=say)
    turn = Turn(user=say)
    for ev in res.events:
        if ev.type == "message" and ev.item.role == "assistant":
            turn.texts.append(ev.item.text_content or "")
        elif ev.type == "function_call":
            turn.calls.append((ev.item.name, ev.item.arguments))
        elif ev.type == "function_call_output":
            turn.outputs.append(ev.item.output)
    for row in log.rows[before:]:
        if row["event"] == "llm_request":
            turn.sent.append(row.get("tool_names") or [])
        elif row["event"] == "llm_response":
            result.agent_tokens[0] += row.get("prompt_tokens") or 0
            result.agent_tokens[1] += row.get("completion_tokens") or 0
        elif row["event"].startswith("guard_") or row["event"] == "booked_from_search":
            detail = row.get("sentence") or row.get("removed") or row.get("unsaid") or ""
            turn.guards.append(f"{row['event']} {detail!r}" if detail else row["event"])
    turn.appts, turn.cbs = _db_state(patient_id)

    result.transcript.append(f"PATIENT: {say}")
    for name, args in turn.calls:
        result.transcript.append(f"    tool {name}({args})")
    for out in turn.outputs:
        result.transcript.append(f"    -> {out[:220]}")
    for g in turn.guards:
        result.transcript.append(f"    guard {g}")
        result.guards.append(g.split(" ", 1)[0])
    for text in turn.texts:
        result.transcript.append(f"AGENT: {text}")
    return turn


async def run(case: Any, run_no: int, patient_id: str) -> Result:
    result = Result(case.id, case.name, case.kind, run_no, patient_id)
    started = time.monotonic()
    with db.session() as conn:
        db.init_db(conn)
        patient = db.get_patient(conn, patient_id)
    _reset_db()

    log = ListLog()
    agent = agent_module.HealthcareAgent(patient, room_name="eval", call_log=log)
    opening = config.opening_line(patient)
    ctx = agent.chat_ctx.copy()
    ctx.add_message(role="assistant", content=opening)
    await agent.update_chat_ctx(ctx)
    result.transcript.append(f"AGENT: {opening}")

    turns: list[Turn] = []
    try:
        async with AgentSession(llm=agent_module._build_llm()) as session:
            await session.start(agent)
            from evals import patient_sim
            person = patient_sim.SimulatedPerson(case.brief_for(patient), patient_sim.build_llm())
            last_agent = opening
            for _ in range(case.max_turns):
                say = await person.reply(last_agent)
                if say is None:
                    result.transcript.append("PATIENT: [hangs up]")
                    break
                turn = await _agent_turn(session, log, patient["id"], say, result)
                turns.append(turn)
                last_agent = " ".join(turn.texts)
            result.sim_tokens = person.tokens
            if not any(re.search(case.valid_if, t.user, re.I) for t in turns):
                result.status = "invalid"
                result.errors.append(f"simulated person never pursued their goal (/{case.valid_if}/)")
            now = booking.clinic_now()
            outcome_errors = [e for o in case.outcomes if (e := o(turns, now, patient))]
            result.errors += outcome_errors
            result.facts["outcome_errors"] = len(outcome_errors)

            record = post_call.CallRecord(room="eval", patient=patient,
                                          history=session.history.to_dict()["items"],
                                          tool_results=agent.tool_results, log_rows=log.rows)
            result.analysis = await post_call.analyze(record)
    except Exception as exc:  # a crash is a result, not a harness failure
        result.status = "crash"
        result.errors.append(f"{type(exc).__name__}: {exc}")

    found = checks.violations(turns, patient, answerer=case.answerer)
    result.errors += [e for errors in found.values() for e in errors]
    result.facts.update(_facts(turns, log.rows), violations={k: len(v) for k, v in found.items()},
                        answerer=case.answerer, expect=list(case.expect_outcomes))
    if result.status == "pass" and result.errors:
        result.status = "fail"
    if result.analysis:
        booked_in_db = bool(turns and turns[-1].appts)
        result.analysis_checks["booking_fact"] = result.analysis["booking_successful"] == booked_in_db
        # The expected outcome only holds if the conversation went as the persona intends.
        result.analysis_checks["outcome"] = (result.analysis["outcome"] in case.expect_outcomes
                                             if result.status == "pass" and case.expect_outcomes else None)
    result.seconds = round(time.monotonic() - started, 1)
    return result


# A tool reply that refuses: nothing was searched, booked or scheduled.
REFUSED = re.compile(r"^(Not |Could not|Error)")
SEARCH_TOOLS = ("find_earliest_slot",)


def _facts(turns: list[Turn], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts the scorecard needs, taken from what happened, not from wording."""
    calls = [{"tool": name, "turn": i, "refused": bool(REFUSED.match(out)), "rejected": "Error parsing" in out}
             for i, t in enumerate(turns) for (name, _), out in zip(t.calls, t.outputs)]
    booked_at = next((i for i, t in enumerate(turns) if t.appts), None)
    searches = [c for c in calls if c["tool"] in SEARCH_TOOLS and not c["refused"]]
    first_search = searches[0]["turn"] if searches else None
    requests = [r for r in rows if r["event"] == "llm_response"]
    return {
        "turns": len(turns),
        "tool_calls": calls,
        "booked": booked_at is not None,
        "callback": any(c["status"] == "pending" for c in (turns[-1].cbs if turns else [])),
        # Searches that led to the booking, and turns from the first one to it.
        "offers_to_book": sum(c["turn"] <= booked_at for c in searches) if booked_at is not None else None,
        "turns_to_book": (booked_at - first_search + 1
                          if booked_at is not None and first_search is not None else None),
        "prompt_tokens": [r.get("prompt_tokens") or 0 for r in requests],
        "completion_tokens": [r.get("completion_tokens") or 0 for r in requests],
    }


def as_dict(result: Result) -> dict[str, Any]:
    return asdict(result)
