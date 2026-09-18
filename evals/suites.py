"""Which conversations a run holds, and how a worker rebuilds each one.

A job is plain data, (suite, key, phrase, run, patient_id), because workers are
separate processes and a case holds functions that cannot cross that boundary.
Wordings are generated once, in the parent, so the whole run tests the same set.
"""

from __future__ import annotations

import asyncio
from typing import Any

SUITES = ("personas", "probes", "clinical", "redteam", "regressions")

Job = tuple[str, str, str, int, str]


def build(suite: str, key: str, phrase: str) -> Any:
    if suite == "personas":
        from evals import personas
        return next(p for p in personas.PERSONAS if p.id == key)
    if suite == "probes":
        from evals import probes
        return probes.case(key, phrase)
    if suite == "clinical":
        from evals import adversarial
        return adversarial.clinical_case(key, phrase)
    if suite == "redteam":
        from evals import adversarial
        return adversarial.redteam_case(key)
    if suite == "regressions":
        from evals import from_log
        return from_log.case(key)
    raise ValueError(f"unknown suite {suite}")


async def _wordings(items: list[tuple[str, str, list[str], str, str]],
                    n: int) -> dict[str, tuple[list[str], bool]]:
    """{key: (wordings, fresh)} for (key, intent, seeds, keep, context) items, generated in parallel."""
    from evals import phrasings
    out = await asyncio.gather(*(phrasings.generate(intent, seeds, n, keep, context)
                                 for _, intent, seeds, keep, context in items))
    return {item[0]: result for item, result in zip(items, out)}


def plan(suites: list[str], *, runs: int, phrasings: int, only: set[str], patients: str,
         every_patient: list[str]) -> tuple[list[Job], list[str]]:
    """(jobs, notes). Notes say anything a reader of the results should know,
    such as wordings that fell back to the seeds."""
    jobs: list[Job] = []
    notes: list[str] = []
    pool = every_patient if patients in ("rotate", "all") else [x.strip() for x in patients.split(",")]

    def patient(i: int) -> str:
        return pool[i % len(pool)]

    def wanted(case_id: str) -> bool:
        return not only or case_id in only

    if "personas" in suites:
        from evals import personas
        ids = [p.id for p in personas.PERSONAS if wanted(p.id)]
        if patients == "all":
            jobs += [("personas", cid, "", run, pid) for run in range(1, runs + 1) for cid in ids for pid in pool]
        else:
            # Persona i on run r meets patient i + r, so repeated runs meet different patients.
            jobs += [("personas", cid, "", run, patient(i + run - 1))
                     for run in range(1, runs + 1) for i, cid in enumerate(ids)]

    generate: list[tuple[str, str, str, list[str], str, str]] = []  # suite, key, intent, seeds, keep, context
    if "probes" in suites:
        from evals import probes
        generate += [("probes", p.id, p.intent, p.seeds, p.keep, probes.ASKED.get(p.id, ""))
                     for p in probes.POINTS if wanted(f"P:{p.id}")]
    if "clinical" in suites:
        from evals import adversarial
        generate += [("clinical", q.id, q.intent, q.seeds, "none", "the agent has just told them their results")
                     for q in adversarial.QUESTIONS if wanted(f"C:{q.id}")]
    if generate:
        got = asyncio.run(_wordings([(f"{s}:{k}", intent, seeds, keep, ctx)
                                     for s, k, intent, seeds, keep, ctx in generate], phrasings))
        for suite, key, *_ in generate:
            lines, fresh = got[f"{suite}:{key}"]
            if not fresh:
                notes.append(f"{suite} {key}: generation failed, used the seed wordings")
            jobs += [(suite, key, line, n, patient(len(jobs) + n)) for n, line in enumerate(lines, 1)]

    if "redteam" in suites:
        from evals import adversarial
        tactics = [t.id for t in adversarial.TACTICS if wanted(f"R:{t.id}")]
        jobs += [("redteam", tid, "", run, patient(i + run)) for run in range(1, runs + 1)
                 for i, tid in enumerate(tactics)]

    if "regressions" in suites:
        from evals import from_log
        jobs += [("regressions", r["id"], "", 1, r["patient_id"]) for r in from_log.load()
                 if wanted(f"G:{r['id']}")]
    return jobs, notes
