"""Sends a finished call to Opik, as one trace with everything the call produced.

This is the assignment's pluggable module: one function, one call site in
agent.py, and nothing else in the agent knows Opik exists. Remove the call and
the agent is unchanged.

One trace carries:

- metadata: patient (phone masked to the last four digits), room, transport,
  models, duration, tokens, guards that fired
- the transcript, one span per turn
- every tool call, with its arguments and result
- every model request, with token usage, so Opik can price the call
- the post-call analysis, with the corrections code applied to it
- an audio reference: the LiveKit room and session, since calls are not
  recorded yet
- feedback scores taken from the database, never from a model

Nothing here may disturb a call: every failure is caught and logged.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config
from post_call import CallRecord

logger = logging.getLogger("adit-agent.opik")

FLUSH_TIMEOUT_SECONDS = 15


def enabled() -> bool:
    return bool(os.environ.get("OPIK_API_KEY"))


def mask_phone(phone: str) -> str:
    digits = "".join(c for c in str(phone) if c.isdigit())
    return f"******{digits[-4:]}" if len(digits) >= 4 else "******"


def _client() -> Any:
    import opik
    # Batching is off: a call produces one short trace, and the SDK warns that
    # ending a batched trace moments after creating it can lose data.
    return opik.Opik(project_name=config.OPIK_PROJECT_NAME,
                     workspace=os.environ.get("OPIK_WORKSPACE") or None, batching=False)


def _rows(call: CallRecord, event: str) -> list[dict[str, Any]]:
    return [r for r in call.log_rows if r.get("event") == event]


def _when(row: dict[str, Any] | None) -> datetime | None:
    stamp = (row or {}).get("at")
    return datetime.fromisoformat(stamp) if stamp else None


def turn_spans(call: CallRecord) -> list[dict[str, Any]]:
    """One span per exchange: what they said, and everything the agent said back.

    Times come from the call log, which our own code wrote as the call happened.
    """
    said = _rows(call, "user_turn_committed")
    spoken = _rows(call, "tts_done")
    spans = []
    for index, turn in enumerate(said, start=1):
        start = _when(turn)
        nxt = _when(said[index]) if index < len(said) else None
        replies = [r for r in spoken
                   if start and _when(r) and _when(r) >= start and (nxt is None or _when(r) < nxt)]
        spans.append({
            "name": f"turn {index}",
            "type": "general",
            "input": {"patient": turn.get("text", "")},
            "output": {"agent": " ".join(r.get("text", "") for r in replies)},
            "start_time": start,
            "end_time": _when(replies[-1]) if replies else start,
        })
    return spans


def tool_spans(call: CallRecord) -> list[dict[str, Any]]:
    results = {r.get("seq"): r for r in _rows(call, "tool_result")}
    spans = []
    for started in _rows(call, "tool_call"):
        done = results.get(started.get("seq"), {})
        spans.append({
            "name": started.get("tool", "tool"),
            "type": "tool",
            "input": started.get("args", {}),
            "output": {"result": done.get("result", "")},
            "start_time": _when(started),
            "end_time": _when(done) or _when(started),
            "metadata": {"duration_ms": done.get("duration_ms")},
        })
    return spans


def llm_spans(call: CallRecord) -> list[dict[str, Any]]:
    """One span per model request, so Opik shows what the call cost."""
    requests = _rows(call, "llm_request")
    spans = []
    for index, response in enumerate(_rows(call, "llm_response")):
        request = requests[index] if index < len(requests) else {}
        usage = {"prompt_tokens": response.get("prompt_tokens") or 0,
                 "completion_tokens": response.get("completion_tokens") or 0}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        start = _when(request) or _when(response)
        spans.append({
            "name": f"llm request {response.get('turn', index + 1)}",
            "type": "llm",
            "model": config.LLM_MODEL,
            "provider": config.LLM_PRIMARY,
            "input": {"tools_sent": request.get("tool_names", []), "history_items": request.get("items")},
            "output": {"text": response.get("text", ""), "tool_calls": response.get("tool_calls", [])},
            "usage": usage,
            "start_time": start,
            "end_time": (start + timedelta(milliseconds=response.get("total_ms") or 0)) if start else None,
        })
    return spans


def analysis_span(call: CallRecord, analysis: dict[str, Any]) -> dict[str, Any]:
    tokens = analysis.get("analysis_tokens", {})
    usage = {"prompt_tokens": tokens.get("input", 0), "completion_tokens": tokens.get("output", 0)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return {
        "name": "post-call analysis",
        "type": "llm",
        "model": analysis.get("model"),
        "input": {"facts": analysis.get("facts")},
        "output": {"judgement": analysis.get("judgement"), "outcome": analysis["outcome"],
                   "overridden": analysis.get("overridden"), "flags": analysis.get("flags")},
        "usage": usage,
        "metadata": {"error": analysis.get("error")},
    }


def scores(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Scores taken from the database and the call log, never from a model."""
    facts = analysis.get("facts", {})
    return [
        {"name": "booking_successful", "value": 1.0 if analysis["booking_successful"] else 0.0,
         "reason": facts.get("booking", {}).get("reference") or "no appointment in the database"},
        {"name": "results_leaked_to_non_patient",
         "value": 1.0 if "results_disclosed_to_non_patient" in analysis.get("flags", []) else 0.0,
         "reason": "results spoken to someone other than the patient" if analysis.get("flags") else "none"},
        {"name": "guards_fired", "value": float(len(facts.get("guards", []))),
         "reason": ", ".join(facts.get("guards", [])) or "none"},
    ]


def audio_reference(call: CallRecord, audio: dict[str, Any] | None) -> dict[str, Any]:
    """Calls are not recorded yet, so this points at the call in LiveKit."""
    reference = {"recorded": False, "livekit_room": call.room,
                 "livekit_url": os.environ.get("LIVEKIT_URL", ""), "note": "session reference; no audio file yet"}
    return {**reference, **(audio or {})}


def build_payload(call: CallRecord, analysis: dict[str, Any], audio: dict[str, Any] | None = None,
                  files: list[Path] | None = None) -> dict[str, Any]:
    """Everything the trace will contain, as plain data so tests can read it."""
    facts = analysis.get("facts", {})
    patient = {"id": call.patient["id"], "name": call.patient["name"],
               "phone": mask_phone(call.patient.get("phone", "")),
               "hba1c": call.patient["hba1c"], "blood_glucose": call.patient["blood_glucose"]}
    start = _when(next(iter(_rows(call, "call_start")), None))
    return {
        "name": "outbound-call",
        "input": {"patient": patient, "opening": config.opening_line(call.patient)},
        "output": {"outcome": analysis["outcome"], "booking": facts.get("booking"),
                   "callback": facts.get("callback"), "summary": (analysis.get("judgement") or {}).get("summary")},
        "metadata": {
            "room": call.room,
            "transport": call.transport,
            "models": {"llm": config.LLM_MODEL, "stt": config.STT_MODEL, "tts": config.TTS_MODEL,
                       "analysis": config.ANALYSIS_MODEL},
            "duration_seconds": facts.get("duration_seconds"),
            "agent_tokens": facts.get("agent_tokens"),
            "tool_errors": facts.get("tool_errors"),
            "guards": facts.get("guards"),
            "audio": audio_reference(call, audio),
            "analysis_version": analysis.get("version"),
        },
        "tags": ["voice-agent", call.transport, analysis["outcome"]],
        "thread_id": call.room,
        "start_time": start,
        "end_time": _when(call.log_rows[-1]) if call.log_rows else None,
        "spans": turn_spans(call) + tool_spans(call) + llm_spans(call) + [analysis_span(call, analysis)],
        "scores": scores(analysis),
        "files": [p for p in (files or []) if p and Path(p).exists()],
    }


def _attachments(files: list[Path]) -> list[Any]:
    import opik
    types = {".json": "application/json", ".jsonl": "application/x-ndjson"}
    return [opik.Attachment(data=str(path), file_name=Path(path).name,
                            content_type=types.get(Path(path).suffix, "text/plain"))
            for path in files]


def send_payload(payload: dict[str, Any], client: Any | None = None) -> str | None:
    """Create the trace, its spans and its scores. Returns the trace id."""
    client = client or _client()
    # The trace is created complete, with its end time: calling Trace.end()
    # afterwards sent a second write that left the trace's own fields empty.
    trace = client.trace(
        name=payload["name"], input=payload["input"], output=payload["output"],
        metadata=payload["metadata"], tags=payload["tags"], thread_id=payload["thread_id"],
        start_time=payload["start_time"], end_time=payload["end_time"],
        attachments=_attachments(payload["files"]),
    )
    for span in payload["spans"]:
        trace.span(**span)
    for score in payload["scores"]:
        trace.log_feedback_score(**score)
    client.flush(timeout=FLUSH_TIMEOUT_SECONDS)
    return trace.id


def send_call(call: CallRecord, analysis: dict[str, Any], *, audio: dict[str, Any] | None = None,
              files: list[Path] | None = None, client: Any | None = None) -> str | None:
    """Send one finished call. Returns the trace id, or None if it was not sent.

    Never raises: observability must not break a call.
    """
    if client is None and not enabled():
        logger.info("Opik not configured (no OPIK_API_KEY); call not sent")
        return None
    try:
        trace_id = send_payload(build_payload(call, analysis, audio, files), client)
        logger.info("call sent to Opik project %s as trace %s", config.OPIK_PROJECT_NAME, trace_id)
        return trace_id
    except Exception:
        logger.exception("failed to send the call to Opik")
        return None


# --- replay a saved call ---------------------------------------------------------

def _load(transcript_path: Path) -> tuple[CallRecord, dict[str, Any], list[Path]]:
    saved = json.loads(transcript_path.read_text())
    room = saved["room"]
    directory = transcript_path.parent
    analysis_files = sorted(directory.glob(f"{room}_*_analysis.json"))
    if not analysis_files:
        raise SystemExit(f"no analysis file for {room} in {directory}")
    analysis = json.loads(analysis_files[-1].read_text())
    logs = sorted(Path(__file__).parent.glob(f"logs/{room}_*.jsonl"))
    rows = [json.loads(line) for line in logs[-1].read_text().splitlines() if line.strip()] if logs else []
    history = saved["history"]["items"] if isinstance(saved.get("history"), dict) else saved.get("history", [])
    call = CallRecord(room=room, patient=saved["patient"], history=history,
                      tool_results=saved.get("tool_results", []), log_rows=rows)
    return call, analysis, [transcript_path, analysis_files[-1], *logs[-1:]]


def main(argv: list[str] | None = None) -> int:
    import argparse

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")   # the agent loads this on import; a replay must too
    parser = argparse.ArgumentParser(description="Send a saved call to Opik.")
    parser.add_argument("--from", dest="path", required=True, help="a session_reports transcript JSON")
    args = parser.parse_args(argv)
    call, analysis, files = _load(Path(args.path))
    trace_id = send_call(call, analysis, files=files)
    print(f"sent trace {trace_id}" if trace_id else "not sent; see the log above")
    return 0 if trace_id else 1


if __name__ == "__main__":
    raise SystemExit(main())
