# Outbound healthcare voice agent

An AI voice agent that calls a patient, tells them their HbA1c and fasting
glucose results, and books a follow-up with a doctor through a tool call. After
the call it works out what happened, and sends the call to Opik, where an
online evaluation scores it.

Built on [LiveKit Agents](https://docs.livekit.io/agents/), with Groq
(`gpt-oss-120b`) for the conversation and Deepgram for speech. It works over a
browser or a real phone line; the conversation code does not know which.

---

## Demonstration

Two live calls on the finished agent, each from the outbound call to a scored
Opik trace. Full transcripts, every tool call, the analysis and the scores are
in [docs/demo-calls.md](docs/demo-calls.md).

| Call | What happens | Analysis | Opik evaluation |
| --- | --- | --- | --- |
| Booking | The patient confirms who she is, hears her results, asks for "day after tomorrow morning", then "make it eleven", and books 11:30 after saying yes | `booked` | booking 1, privacy 1, professionalism 5/5 |
| Someone else | The patient's brother answers and asks what it is about; he hears only that the clinic is calling for the patient | `wrong_person` | booking 0, privacy 1, professionalism 3/5 |

---

## Quick start

You need Python 3.11+, and free accounts at [LiveKit Cloud](https://cloud.livekit.io),
[Groq](https://console.groq.com), [Deepgram](https://console.deepgram.com) and,
optionally, [Opik](https://www.comet.com/opik).

```shell
git clone https://github.com/akkimind/voice-agent-assignment.git
cd voice-agent-assignment
./install.sh
```

The installer creates a virtual environment, installs the packages, and asks
for each key, telling you where to find it and checking that it works. It then
creates the database and the Opik evaluation rule. Details, and how to do it by
hand: [docs/setup.md](docs/setup.md).

Then, to make a call:

```shell
./.venv/bin/python agent.py dev                       # 1. start the agent; leave it running
./.venv/bin/python dispatch_outbound.py --browser     # 2. in another terminal: a call you join in the browser
```

Or talk to it straight away in the terminal: `./.venv/bin/python agent.py console`.

---

## Commands

`py` is `./.venv/bin/python`.

| Command | What it does |
| --- | --- |
| `./install.sh` | Setup, once |
| `py agent.py dev` | Starts the agent worker |
| `py dispatch_outbound.py --browser` | A call you join in the browser, as the patient |
| `py dispatch_outbound.py` | A real phone call ([needs a SIP trunk](telephony/README.md)) |
| `py agent.py console` | Talk to the agent in the terminal |
| `py install.py --check` | Tests the keys in `.env` |
| `py db.py --reset` | A clean calendar |
| `py -m unittest` | Unit tests, no network |
| `py -m evals` | Simulated calls against the real model |
| `py -m evals.scorecard` | Every quality metric against its target |

Every option: [docs/setup.md](docs/setup.md#every-command).

---

## What it does on a call

1. Asks for the patient by name, saying nothing about the clinic or the reason
   until the person confirms who they are. The results are not in the agent's
   prompt at all; a tool hands them over only after that confirmation.
2. Tells the patient their values, says the doctor would like to go over them,
   and never interprets them, names a condition or gives advice.
3. Books against a real calendar: it searches for the time the patient wants,
   reads the slot back, and books only after they say yes.
4. If someone else answers, shares nothing medical and does nothing on the
   patient's behalf. If the patient is busy, queues a callback.
5. After the call, analyses what happened, with the booking read from the
   database rather than guessed, and sends everything to Opik.

How and why: [docs/how-it-works.md](docs/how-it-works.md).

---

## The assignment, and where each part is

| Asked for | Where |
| --- | --- |
| Outbound agent on LiveKit that shares the results and books through a tool | `agent.py`; [how it works](docs/how-it-works.md) |
| Post-call analysis, including whether a booking succeeded | `post_call.py`; [analysis and Opik](docs/analysis-and-opik.md) |
| Opik: metadata, transcript, audio, tool calls, analysis | `opik_integration.py`, one module plugged in with one line |
| An online evaluation in Opik | `opik_rules.py`; scores every call |
| README with setup and usage | This page and [docs/setup.md](docs/setup.md) |
| Demonstration of the complete flow | [docs/demo-calls.md](docs/demo-calls.md) |

---

## Quality, in brief

A second model plays callers, from ordinary patients to people trying to get
the results out of the agent, and code checks what happened. Before and after
the last round of work, 116 simulated calls each:

| Metric | Target | Before | After |
| --- | --- | --- | --- |
| Understands any wording (fresh phrasings each run) | ≥ 95% | 48% | **96%** |
| Phone number spoken | 0 | 52 | **0** |
| Results interpreted or a condition named | 0 | 30 | **0** |
| Red-team calls that got something out of the agent | 0 of 20 | 6 | **1** (0 in the targeted re-runs after its fix) |
| Booking rate when the patient wants one | ≥ 95% | 90% | **100%** |

Full scorecard, the test suites and what is still short of target:
[docs/testing.md](docs/testing.md).

---

## Status

| Part | State |
| --- | --- |
| Browser calls | Working; see the demonstration |
| Post-call analysis, Opik trace, online evaluation | Working |
| Real phone calls | Code complete and tested; needs a paid SIP trunk ([details](telephony/README.md)) |
| Callbacks | Queued, but nothing dials them yet |

---

## Documentation

| Document | Contents |
| --- | --- |
| [docs/setup.md](docs/setup.md) | Installing, every key, running calls, every command |
| [docs/how-it-works.md](docs/how-it-works.md) | The call flow, the code layout, design decisions, safety checks, known limits |
| [docs/analysis-and-opik.md](docs/analysis-and-opik.md) | Post-call analysis, the Opik trace, the evaluation rule |
| [docs/testing.md](docs/testing.md) | Unit tests, evals, the scorecard and results, costs |
| [telephony/README.md](telephony/README.md) | Real phone calls: providers, the SIP trunk, failure codes |
| [docs/demo-calls.md](docs/demo-calls.md) | The two demonstration calls in full |
| [docs/demo-script.md](docs/demo-script.md) | A script for showing it live |
