"""Conversation evals for the voice agent, run against the live LLM without audio.

A second model plays whoever picked up: patient personas, probes of single
decision points in freshly generated wordings, clinical questions, red-team
callers, and replays of live calls. Pass or fail is decided by code wherever it
can be: database state, tool calls, spoken values, phone numbers, condition
names. A judge model reads only what needs reading (an interpretation, a hint),
must quote the agent's words, and decides only the clinical and red-team suites.

    python -m evals                               # every suite, Groq free tier only
    python -m evals --suite personas --only S2,S6
    python -m evals.scorecard                     # every metric against its target
"""
