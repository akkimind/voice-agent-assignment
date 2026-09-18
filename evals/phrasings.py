"""Fresh wordings of one intent, generated each run.

The seed examples show the model the kind of line wanted; it is told not to
reuse them, so every run tests wordings nobody wrote down. If generation fails,
the seeds are used, and the run says so.
"""

from __future__ import annotations

import json
import random
import re
from typing import Any

_PROMPT = """\
A patient is on a phone call with a clinic. {context}Write {n} different \
things they might say out loud that mean: {intent}

Keep these facts exactly: {keep}
Vary the wording as real people do: very short, rambling, indirect, polite, \
casual, hesitant, plain English from a non-native speaker. Some may be a single \
word. Use no names. Each line must mean exactly that, clearly enough that a \
receptionist would understand it. Do not reuse these examples: {seeds}

Reply with only a JSON list of {n} strings."""


def parse(text: str, n: int) -> list[str]:
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise ValueError("no JSON list in reply")
    lines = [str(x).strip() for x in json.loads(match.group(0)) if str(x).strip()]
    unique = list(dict.fromkeys(lines))
    if len(unique) < n:
        raise ValueError(f"asked for {n} wordings, got {len(unique)}")
    return unique[:n]


async def generate(intent: str, seeds: list[str], n: int, keep: str = "none", context: str = "",
                   model: Any = None) -> tuple[list[str], bool]:
    """(wordings, fresh). fresh is False when the seeds were used instead."""
    import post_call
    from evals import patient_sim
    try:
        text, _ = await post_call._complete(model or patient_sim.build_llm(),
                                            _PROMPT.format(n=n, intent=intent, keep=keep, seeds=json.dumps(seeds),
                                                          context=f"They are answering: {context} " if context else ""))
        return parse(text, n), True
    except Exception:
        return random.sample(seeds, min(n, len(seeds))), False
