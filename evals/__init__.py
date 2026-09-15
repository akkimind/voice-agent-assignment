"""Conversation evals for the voice agent, run against the live LLM without audio.

A second, smaller model plays a patient persona with a fixed goal and free
wording, so every run can take a different path. Pass or fail is decided by
code: database state, tool calls, which tools were sent, and what the agent
said. No model grades the agent.

    python -m evals                 # every persona, 3 runs each
    python -m evals --runs 1 --only S2,S6
"""
