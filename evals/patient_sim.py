"""A second model that plays the person who picked up the phone."""

from __future__ import annotations

import os
import re

from livekit.agents import inference, llm
from livekit.plugins import openai

import config

SIM_MODEL = "openai/gpt-oss-20b"          # Groq; free tier
SIM_FALLBACK_MODEL = "openai/gpt-oss-120b"  # LiveKit Inference has no 20b
HANGUP = "[HANGUP]"

_RULES = """\
You are role-playing the person who answered a phone call from a clinic. Stay in \
character for the whole call.

{brief}

How you talk: {style}. Say things your own way; never copy the caller's phrasing.

It is now {now} where you live. Any day or time you suggest must make sense for that.

Rules:
- Reply with only what you say out loud: one or two short spoken sentences.
- Say one thing at a time and ask at most one question per reply; wait for the answer.
- Never write the caller's lines, stage directions, or notes.
- Never mention that this is a simulation or a test.
- Pursue your goal even if the caller steers elsewhere, but react naturally to what they say.
- When the call is clearly over (they said goodbye, or you are done), reply with exactly {hangup}
"""


# One is picked at random per conversation, so no two runs word things alike.
STYLES = [
    "terse, a few words at a time",
    "chatty, with a little small talk",
    "polite and indirect",
    "plain, simple English; English is your second language",
    "casual, with filler words like 'uh' and 'yeah'",
    "distracted, sometimes answering a moment late",
]


def build_llm(model: str = "") -> llm.LLM:
    model = model or SIM_MODEL
    groq = openai.LLM(model=model, base_url=config.GROQ_BASE_URL, api_key=os.environ["GROQ_API_KEY"],
                      reasoning_effort="low")
    if not config.paid_fallback():
        return config.groq_only(groq)
    return llm.FallbackAdapter([
        groq,
        inference.LLM(model=SIM_FALLBACK_MODEL, extra_kwargs={"reasoning_effort": "low"}),
    ], max_retry_per_llm=0)


class SimulatedPerson:
    def __init__(self, brief: str, model: llm.LLM, style: str = "") -> None:
        self._model = model
        # Roles are flipped: the agent's lines are what this model hears.
        self._ctx = llm.ChatContext()
        import booking
        now = booking.clinic_now().strftime("%A %d %B, %I:%M %p")
        self._ctx.add_message(role="system", content=_RULES.format(brief=brief, hangup=HANGUP, now=now,
                                                                         style=style or "naturally"))
        self.tokens = [0, 0]

    async def reply(self, agent_said: str) -> str | None:
        """The person's next line, or None when they hang up."""
        self._ctx.add_message(role="user", content=agent_said or "(silence)")
        text = ""
        usage = None
        async with self._model.chat(chat_ctx=self._ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    text += chunk.delta.content
                if chunk.usage:
                    usage = chunk.usage  # some providers repeat usage; keep the last

        if usage:
            self.tokens[0] += usage.prompt_tokens
            self.tokens[1] += usage.completion_tokens
        text = text.strip()
        if not text or text.startswith(HANGUP):
            return None
        text = text.replace(HANGUP, "").strip()
        # The model sometimes writes several turns at once ("Is it dangerous?Yes, book Monday").
        # A real person says one or two sentences and waits.
        text = " ".join(re.split(r"(?<=[.?!])\s*(?=[A-Z])", text)[:2]).strip()
        self._ctx.add_message(role="assistant", content=text)
        return text
