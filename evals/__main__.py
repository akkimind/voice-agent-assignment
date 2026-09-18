"""Command line entry.

    python -m evals                                  # every suite
    python -m evals --suite personas --runs 1 --only S2,S6
    python -m evals --suite probes --phrasings 8 --only P:no-preference
    python -m evals --suite redteam,clinical
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from evals import suites, worker

RESULTS_DIR = Path(__file__).parent / "results"
# Groq list prices per 1M tokens (input, output). Requests that fall back to
# LiveKit Inference are cheaper, so these are upper-bound estimates.
PRICE_AGENT = (0.15, 0.60)     # gpt-oss-120b
PRICE_SIM = (0.075, 0.30)      # gpt-oss-20b
MARK = {"pass": "✓", "fail": "✗", "invalid": "?", "crash": "!"}


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument("--suite", default=",".join(suites.SUITES),
                        help="comma-separated: " + ", ".join(suites.SUITES) + " (default: all)")
    parser.add_argument("--runs", type=int, default=3, help="repeats per persona and red-team tactic (default 3)")
    parser.add_argument("--phrasings", type=int, default=4,
                        help="fresh wordings per probe and clinical question (default 4)")
    parser.add_argument("--only", default="", help="comma-separated case ids, e.g. S2,P:evening,R:nurse")
    parser.add_argument("--patients", default="rotate",
                        help="rotate (default: each run of a persona meets a different patient), all "
                             "(every persona with every patient), or comma-separated ids, e.g. p-002,p-005")
    # Four at once made both providers refuse; three keeps the run under their limits.
    parser.add_argument("--workers", type=int, default=3, help="conversations in parallel (default 3)")
    args = parser.parse_args()

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    chosen = [x.strip() for x in args.suite.split(",") if x.strip()]
    unknown = set(chosen) - set(suites.SUITES)
    if unknown:
        print(f"Unknown suite: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    db_dir = tempfile.mkdtemp(prefix="evals-db-")
    worker.init(db_dir)  # the parent also needs the project importable, and generates the wordings
    jobs, notes = suites.plan(chosen, runs=args.runs, phrasings=args.phrasings, only=only,
                              patients=args.patients, every_patient=worker.patient_ids())
    if not jobs:
        print("No cases match.", file=sys.stderr)
        return 2
    print(f"{len(jobs)} conversations ({', '.join(chosen)}), {args.workers} in parallel", flush=True)
    for note in notes:
        print(f"  note: {note}", flush=True)

    started = time.monotonic()
    results = []
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx,
                             initializer=worker.init, initargs=(db_dir,)) as pool:
        futures = {pool.submit(worker.run_job, *job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            suite, key, phrase, run, pid = futures[future]
            try:
                r = future.result()
            except Exception as exc:
                r = {"id": key, "name": "?", "kind": "simulated", "run": run, "patient_id": pid, "status": "crash",
                     "suite": suite, "phrase": phrase, "errors": [f"worker: {exc}"], "transcript": [],
                     "guards": [], "agent_tokens": [0, 0], "sim_tokens": [0, 0], "seconds": 0}
            results.append(r)
            said = f"  {r['phrase']!r}" if r.get("phrase") else ""
            print(f"  [{done}/{len(jobs)}] {MARK[r['status']]} {r['id']:>18} run {r['run']} "
                  f"{r.get('patient_id', '')}  {r['seconds']}s{said}", flush=True)

    elapsed = time.monotonic() - started
    out = RESULTS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=1))
    summary = _summary(results, elapsed) + "".join(f"note: {n}\n" for n in notes)
    (out / "summary.md").write_text(summary)
    (out / "failures.md").write_text(_failures(results))
    print(summary)
    print(f"Saved to {out}")
    return 0 if all(r["status"] in ("pass", "invalid") for r in results) else 1


def _summary(results: list[dict], elapsed: float) -> str:
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_case[r["id"]].append(r)
    lines = ["| case | runs | patients | passed | analysis outcomes | first problem |", "|---|---|---|---|---|---|"]
    for cid, rs in sorted(by_case.items()):
        rs.sort(key=lambda r: r["run"])
        marks = "".join(MARK[r["status"]] for r in rs)
        passed = sum(r["status"] == "pass" for r in rs)
        valid = sum(r["status"] != "invalid" for r in rs)
        problem = next((e for r in rs if r["status"] != "pass" for e in r["errors"]), "")
        seen = [r["analysis"]["outcome"] if r.get("analysis") else "-" for r in rs]
        who = ", ".join(r.get("patient_id", "") for r in rs)
        lines.append(f"| {cid} {rs[0]['name']} | {marks} | {who} | {passed}/{valid} | {', '.join(seen)} | {problem[:120]} |")

    agent_in = sum(r["agent_tokens"][0] for r in results)
    agent_out = sum(r["agent_tokens"][1] for r in results)
    sim_in = sum(r["sim_tokens"][0] for r in results)
    sim_out = sum(r["sim_tokens"][1] for r in results)
    big = [r for r in results if r.get("suite") == "redteam"]
    big_in = sum(r["sim_tokens"][0] for r in big)
    big_out = sum(r["sim_tokens"][1] for r in big)
    judge_in = sum((r.get("judge_tokens") or [0, 0])[0] for r in results)
    judge_out = sum((r.get("judge_tokens") or [0, 0])[1] for r in results)
    cost = (agent_in * PRICE_AGENT[0] + agent_out * PRICE_AGENT[1]
            + (sim_in - big_in) * PRICE_SIM[0] + (sim_out - big_out) * PRICE_SIM[1]
            + (big_in + judge_in) * PRICE_AGENT[0] + (big_out + judge_out) * PRICE_AGENT[1]) / 1e6
    counts = defaultdict(int)
    for r in results:
        counts[r["status"]] += 1
    guards = defaultdict(int)
    for r in results:
        for g in r["guards"]:
            guards[g] += 1
    outcome_checks = [r["analysis_checks"].get("outcome") for r in results if r.get("analysis_checks")]
    fact_checks = [r["analysis_checks"].get("booking_fact") for r in results if r.get("analysis_checks")]
    scored = [c for c in outcome_checks if c is not None]
    a_in = sum((r.get("analysis") or {}).get("analysis_tokens", {}).get("input", 0) for r in results)
    a_out = sum((r.get("analysis") or {}).get("analysis_tokens", {}).get("output", 0) for r in results)
    cost += (a_in * PRICE_SIM[0] + a_out * PRICE_SIM[1]) / 1e6
    overrides = sum(len((r.get("analysis") or {}).get("overridden", [])) for r in results)
    analysis_errors = [r["id"] for r in results if (r.get("analysis") or {}).get("error")]
    return "\n".join([
        "", *lines, "",
        f"analysis: outcome matched {sum(scored)}/{len(scored)} passing calls · booking fact matched "
        f"{sum(bool(c) for c in fact_checks)}/{len(fact_checks)} · overrides {overrides} · "
        f"model errors {len(analysis_errors)} · tokens {a_in:,} in / {a_out:,} out",
        f"pass {counts['pass']} · fail {counts['fail']} · invalid {counts['invalid']} · crash {counts['crash']}"
        f"  ({len(results)} conversations in {elapsed / 60:.1f} min)",
        f"tokens: agent {agent_in:,} in / {agent_out:,} out · simulated caller {sim_in:,} in / {sim_out:,} out"
        f" · judge {judge_in:,} in / {judge_out:,} out"
        f" · ≈${cost:.3f} at Groq prices",
        "guards fired: " + (", ".join(f"{k} ×{v}" for k, v in sorted(guards.items())) or "none"),
        "",
    ])


def _failures(results: list[dict]) -> str:
    parts = []
    for r in sorted(results, key=lambda r: (r["id"], r["run"])):
        if r["status"] == "pass" and all(v is not False for v in r.get("analysis_checks", {}).values()):
            continue
        parts.append(f"## {r['id']} {r['name']} · run {r['run']} · {r.get('patient_id', '')} · {r['status']}\n")
        if r.get("phrase"):
            parts.append(f"- probed line: {r['phrase']!r}")
        if r.get("style"):
            parts.append(f"- caller style: {r['style']}")
        parts += [f"- {e}" for e in r["errors"]]
        if r.get("analysis"):
            a = r["analysis"]
            parts.append(f"- analysis: {a['outcome']} · overridden {a['overridden']} · flags {a['flags']} · "
                         f"error {a['error']} · {(a.get('judgement') or {}).get('summary')}")
        parts.append("\n```\n" + "\n".join(r["transcript"]) + "\n```\n")
    return "\n".join(parts) or "No failures.\n"


if __name__ == "__main__":
    sys.exit(main())
