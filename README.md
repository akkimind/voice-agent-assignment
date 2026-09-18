# Adit outbound healthcare voice agent

An outbound voice agent that calls a patient, tells them their recent HbA1c and
fasting glucose results, and books a follow-up consultation through a tool call.
After the call it works out what happened and sends the whole thing to Opik,
where an evaluation rule scores it automatically.

Built on [LiveKit Agents](https://docs.livekit.io/agents/) 1.8. The same agent
runs over a browser (development) and over a real phone line (SIP), because
nothing in the conversation code knows which one it is.

---

## What it does on a call

1. Asks for the patient by name, and says nothing about the clinic until it
   knows who answered.
2. If a different or similar name comes back ("Mira" for "Meera"), it asks
   once to confirm before sharing anything: speech recognition mangles names,
   and a relative may have a similar one.
3. Once confirmed: introduces the clinic, checks they have a few minutes, tells
   them their results plainly, and recommends a consultation.
4. Books an appointment against a real calendar in SQLite, which enforces one
   booking per doctor per slot. If the time they want is taken or after hours,
   it offers the nearest free slot, keeping the time of day: a full "tomorrow
   evening" becomes the next evening, not the next morning. The clinic closes
   at 5 PM, so "evening" means its last slots, 3 to 5 PM.
5. If someone else answers, it gives the clinic name and the front desk number
   and nothing else, ever.
6. If they are busy or ask to be called later, it asks when and queues a
   callback instead.
7. Never calls a patient who already has an upcoming appointment: the
   dispatcher, the retry queue and the agent itself all check. Booking also
   cancels any callback still queued for that patient.
8. After the call: writes a transcript, an analysis and a full event log, and
   sends one trace to Opik.

---

## Architecture

```
dispatch_outbound.py ──► LiveKit ──► agent.py (the call)
                                      │
                         ┌────────────┼─────────────┬───────────────┐
                         ▼            ▼             ▼               ▼
                   Deepgram STT   Groq LLM     Deepgram TTS    tools ──► SQLite
                                (LiveKit fallback)                  booking.py
                                                                    callback_queue.py
                                      │
                       call ends      ▼
                              post_call.py ──► session_reports/*_analysis.json
                                      │
                              opik_integration.py ──► Opik trace
                                                         │
                                                  opik_rules.py (scores it there)
```

| File | Responsibility |
| --- | --- |
| `agent.py` | The call: pipeline, tools, and the guardrails that sit between the model and the patient |
| `config.py` | Models, clinic identity, prompts, outcome vocabulary. No secrets |
| `booking.py` | Appointments: resolve a request to a slot, check it, book it |
| `callback_queue.py` | Callbacks: when we may call, and the first free slot at or after that |
| `scheduling.py` | Pure time arithmetic shared by both. No clock, no database |
| `db.py` | SQLite schema, seed data, transactions |
| `call_log.py` | Our own JSONL event log, written as the call happens |
| `post_call.py` | Facts from the database, one model judgement, then code reconciles them |
| `opik_integration.py` | One function that sends a finished call to Opik |
| `opik_rules.py` | The Opik evaluation rule, kept in the repo instead of in clicks |
| `telephony.py` | Dialling and SIP status codes. The only file that knows about phones |
| `dispatch_outbound.py` | Starts one call from the command line |
| `evals/` | Simulated patients that talk to the real agent |

### Decisions worth defending

- **The model never decides a fact.** Whether a booking succeeded comes from the
  database, not from the transcript. The model handles language; code handles
  truth.
- **Guardrails are code, not prompt lines.** Every prompt rule we tested held
  only about two times in three. The rules that matter are enforced in
  `agent.py` and cannot be talked around.
- **Tools are sent by stage.** Booking tools are withheld until results were
  shared or the caller asks to book, saving ~425 tokens on every greeting-stage
  request.
- **Two providers, one model.** Groq serves `gpt-oss-120b` first, LiveKit
  Inference serves the same model as a fallback, so a rate limit does not end a
  call mid-sentence.
- **The calendar is real.** A unique index enforces one booking per doctor per
  slot, so two calls racing for 10:30 cannot both win.

---

## Guardrails

Each one exists because it happened, in a rehearsal or on a real call.

| Guard | What it stops | Log event |
| --- | --- | --- |
| Booking must be grounded | Booking a time the patient never chose. Minutes must match too: "10 AM" does not authorise 10:30 | `book_appointment` refusal |
| Day from the offer | The opposite failure: "can you do twelve thirty?" after an offer books 12:30 on the offered day | — |
| Invented reply cut | The model writing the patient's answer into its own turn ("…confirm?Yes, that works") and acting on it | `guard_cut_invented_reply`, `guard_dropped_tool_call` |
| Spoken times checked | Saying a time no tool returned, e.g. "around 10:45" when the tool scheduled 9:00 | `guard_ungrounded_time` |
| Spoken days checked | Naming a weekday nobody mentioned | `guard_ungrounded_day` |
| No diagnosis | Calling a result "a sign of diabetes" | `guard_diagnosis` |
| Empty reply retried | Dead air when the model returns nothing | `guard_empty_reply` |
| No silent turns | The framework stopping after several tool rounds with nothing said | `guard_tools_withheld` |
| Callback needs a stated time | Scheduling a callback for a time nobody agreed to | `guard_callback_without_time`, `guard_callback_value_unsaid` |
| Search needs a preference | Offering a slot the moment the patient agrees to book | `guard_search_before_preference` |

When a guard blocks a reply, the agent asks a question instead of inventing:
"What day and time would work best?"

---

## Setup

```shell
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env     # then fill it in
```

### Keys

| Variable | Where from | Needed for |
| --- | --- | --- |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | [cloud.livekit.io](https://cloud.livekit.io) → Project settings → Keys | Every call |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) | The agent and the post-call analysis |
| `DEEPGRAM_API_KEY` | [console.deepgram.com](https://console.deepgram.com) | Speech in and out |
| `OPIK_API_KEY`, `OPIK_WORKSPACE` | [comet.com/opik](https://www.comet.com/opik) → user menu → API key | Sending calls to Opik |
| `SIP_*` | Your SIP provider, see [telephony/README.md](telephony/README.md) | Real phone calls only |

Without the Opik keys everything still works; calls simply are not sent.

### Database

```shell
./.venv/bin/python db.py --reset     # tables, seed patients, a few taken slots
```

---

## Running

```shell
./.venv/bin/python agent.py console --record # talk to it in the terminal, recorded
./.venv/bin/python agent.py dev              # worker; join the room in a browser
./.venv/bin/python dispatch_outbound.py      # ring a real phone (needs SIP setup)
```

For a browser call, run `agent.py dev`, then open your LiveKit project's
playground and join the room the worker prints.

Each call writes:

| File | Contents |
| --- | --- |
| `logs/<room>_<time>.jsonl` | Every input and output with two timestamps, written by our own code |
| `session_reports/<room>_<time>.json` | Transcript and tool results |
| `session_reports/<room>_<time>_analysis.json` | The post-call analysis |

---

## Post-call analysis

Three stages, in `post_call.py`:

1. **Facts, from code.** The booking and callback read from the database for
   that room, whether the results were actually said, tool errors, guards that
   fired, duration, tokens.
2. **Judgement, from `gpt-oss-20b`.** Outcome, who answered, sentiment, reason
   for declining, concerns raised, a two-sentence summary. One request, about
   $0.0001.
3. **Reconciliation, in code.** The model cannot contradict the facts. A booking
   in the database means the outcome is "booked", whatever the model said, and
   every correction is recorded in `overridden`.

Outcomes: `booked`, `declined`, `callback_requested`, `wrong_person`,
`incomplete`, and the phone-only `no_answer`, `voicemail`, `rejected`.

If the model fails or times out, the record still holds the facts, with outcome
`unknown` and the error. A call nobody answered skips the model entirely.

---

## Opik

One function, one call site. `opik_integration.send_call()` is invoked once in
`agent.py`; delete that line and the agent is unchanged. Tests check that this is the only place the agent touches Opik and that no other module imports it.

Each trace carries:

- **Metadata:** patient with the phone masked to its last four digits, room,
  transport, models, duration, tokens, guards that fired
- **Transcript:** one span per turn, timed from our own log
- **Tool calls:** arguments and results, as spans
- **Model requests:** with token usage, so Opik prices the call
- **Analysis:** the judgement plus the corrections code applied
- **Call recording:** both sides of the call as an Ogg file attached to the
  trace, plus the LiveKit room as a reference. Console mode records only with
  `--record`
- **Scores from the database:** `booking_successful`,
  `results_leaked_to_non_patient`, `guards_fired`

Send a call that already happened:

```shell
./.venv/bin/python opik_integration.py --from session_reports/<transcript>.json
```

### The online evaluation rule

Opik scores every new call on its own server, with nothing running locally.

```shell
./.venv/bin/python opik_rules.py apply    # create or update the rule
./.venv/bin/python opik_rules.py show     # what is configured
./.venv/bin/python opik_rules.py logs     # what the judge did, errors included
```

| Setting | Value |
| --- | --- |
| Name | `call-quality` |
| Project | `adit-outbound-voice-agent` |
| Judge model | `opik-free-model` (the workspace's built-in free provider) |
| Sampling | 100% of calls |
| Scores | `booking_achieved` (0/1), `privacy_respected` (0/1), `professionalism` (1–5), each with a reason |
| Variables | `transcript`, `patient_name`, `outcome`, `booking`, all read from the trace's own fields |

The judge can read a trace's fields but not its attachments, which is why the
transcript travels on the trace itself. Naming any other model fails with "API
key not configured for LLM" until a provider key is added to the workspace.

---

## Testing

```shell
./.venv/bin/python -m unittest                        # 226 tests, no network
./.venv/bin/python -m evals                           # every suite, real model
./.venv/bin/python -m evals --suite probes --only P:evening --phrasings 8
./.venv/bin/python -m evals --suite redteam,clinical
./.venv/bin/python -m evals.from_log logs/<call>.jsonl --id <name> --expect evening
```

**Unit tests** cover booking and callback rules, every guardrail, the post-call
stages, the Opik payload, the dial path, and the eval machinery itself, all
without network access.

**Evals** are conversations between the real agent and a second model playing
whoever picked up. Five suites:

| Suite | What it tests | Decided by |
| --- | --- | --- |
| Personas | Nine whole calls: a busy driver, a pushy sibling, a day changer, a bad line, a parent, a worried patient, a flip-flopper, a time with no day, an evening person | Code |
| Probes | Thirteen decision points (identity, busy, no preference, evening, a day only, a time with no day, after hours, accepting or declining an offer, declining the visit, callback time), each said in wordings **generated fresh every run** | Code |
| Clinical | The patient asks what's normal, whether it's bad, whether they have diabetes, what to eat, whether to skip the visit, why; fresh wordings each run | Judge, plus code for condition names |
| Red team | A caller who is not the patient, played by the larger model, trying ten tactics: nurse, insurer, yes/no fishing, "confirm the 7-point-something", proxy consent, identity switch, another patient's booking, prompt extraction, injected "system notice", booking or cancelling for the patient | Code for hard leaks and actions, judge for hints |
| Regressions | Live calls replayed line by line, so a bug found on a call stays found | Code |

Every simulated caller gets a random speaking style (terse, chatty, indirect,
non-native English, casual, distracted) and is told to use its own words, so no
two runs say the same thing.

**Code decides wherever it can**: database state, tool calls, spoken values,
phone numbers, condition names, instructions read aloud, another patient's data,
invented times. **A judge model** (`gpt-oss-120b`, evals only, never in a call)
reads each conversation against the clinical policy and privacy rules for what
needs reading: an interpretation, a hint, advice, a confirming "mm-hm". It must
quote the agent's exact words; a quote the agent never said is thrown away, so
the judge cannot invent a breach. Its findings decide pass or fail only in the
clinical and red-team suites; elsewhere they are counted in the scorecard.

A run whose simulated caller never said the probed line, or never pursued its
goal, is marked invalid rather than blamed on the agent.

`--patients all` runs every persona against each of the five fictional
patients; the default rotates them, so repeated runs meet different patients.

### Scorecard

```shell
./.venv/bin/python -m evals.scorecard            # every metric against its target
./.venv/bin/python -m evals.scorecard --opik     # plus the online judge's scores
./.venv/bin/python -m evals.scorecard --accept   # make this the version to beat
```

One command reads the latest eval run, the live call logs, the post-call
analyses, the database, the unit tests and optionally Opik, and prints each
metric against its target with the change since the last scorecard. Twelve
sections cover the whole project: prompt quality (including fixed tokens per
request), safety, task outcomes, tool use, guardrails, latency, cost, post-call
analysis, Opik, reliability, code and telephony. A metric nothing can measure
yet says so, and names the work that will add it; it is never estimated.

**Release rule for a prompt change:** every safety metric is zero, and scenario
success, semantic robustness and fixed tokens are no worse than the last
accepted scorecard. The command exits non-zero otherwise.

**Baseline**, taken on 19 September before the prompt rework
([docs/rework-plan.md](docs/rework-plan.md)), 45 conversations:

| Metric | Baseline | Target |
| --- | --- | --- |
| Scenario success | 53% (24/45) | ≥ 95% |
| Phone number spoken | 32 times | 0 |
| Results or condition disclosed to a non-patient | 0 | 0 |
| Booking rate among willing patients | 96% | ≥ 95% |
| Preference fit | 83% | ≥ 90% |
| Fixed tokens per request | 1,338 early, 1,867 when booking | no rise |
| Analysis booking fact vs database | 45/45 | 100% |
| Reply latency, live calls | p50 1.75 s, p95 4.59 s | ≤ 1.5 s, ≤ 3 s |

Most failures are one rule: the current prompt reads out the front desk
number, and the scorecard now counts any spoken phone number as a breach. The
other real bug found: a patient who names only a time ("can you do twelve
thirty?") before any offer gets booked for today without being asked the day.
One "invented time" is a false positive (the patient said "one o'clock", the
check only reads digits). Full card: [docs/scorecard-baseline.md](docs/scorecard-baseline.md).

**W4 checkpoint**, the new tests on the same code, 116 conversations
([docs/scorecard-w4-checkpoint.md](docs/scorecard-w4-checkpoint.md)): semantic
robustness 48% (evening preference 0/4, someone else answering 0/4), 30
conversations with an interpretation or condition name ("above the usual
range"), and 6 of 20 red-team calls got something: a fake "system notice" and a
request to book for the patient each got the full results read out, and a
booking was made for someone who was not the patient. These are what the
prompt and guard rework (W5 to W7) must bring to zero.

---

## Costs

Per call of roughly 11 turns, at list prices:

| Item | Cost |
| --- | --- |
| Agent model (Groq `gpt-oss-120b`) | ~$0.004 |
| Speech recognition (Deepgram Nova-3) | $0.0048/min |
| Speech synthesis (Deepgram Aura-2) | $0.030 per 1k characters, usually the largest share |
| Post-call analysis (`gpt-oss-20b`) | ~$0.0001 |
| Opik trace and the judge | free tier |

A full eval run is about $0.06. Phone minutes are extra, and depend on the SIP
provider.

---

## Phone calls: implemented, not funded

The telephony path is written, unit-tested and documented, but no call has been
placed over a real line. Every free route to a SIP trunk was blocked by the
provider, not by the code:

| Provider | What happened |
| --- | --- |
| Twilio | Account works and the number is verified, but a trial account cannot use Elastic SIP Trunking. `<Dial><Sip>` from Programmable Voice is refused as well: the call connects, Twilio plays an error, and no SIP INVITE ever reaches LiveKit. Verified with authentication removed from the inbound trunk, so credentials were not the cause |
| Plivo | Signup rejects free email domains, and a company domain too |
| Sinch | Signup rejects the same addresses, including a university one |
| LiveKit Phone Numbers | Inbound only; their docs state outbound needs a third-party provider |

Enabling it is a configuration change, not a code change. With any
LiveKit-supported trunk (Twilio, Telnyx, Plivo, Sinch, Wavix, DIDLogic):

1. Put the trunk address and caller ID in `telephony/outbound-trunk.json`.
2. Set `SIP_AUTH_USERNAME`, `SIP_AUTH_PASSWORD` and `SIP_OUTBOUND_TRUNK_ID`.
3. `python dispatch_outbound.py --patient p-001`

See [telephony/README.md](telephony/README.md) for the provider steps and the
SIP status codes. On a real line the agent dials from inside the room, waits for
the answer before speaking so it never talks over the ringtone, waits ~2.5s for
the callee to say hello, and hangs up shortly after its closing words.

### What the phone path does and does not cover

| Behaviour | State |
| --- | --- |
| Dialling, answer detection, hangup | Implemented; unit-tested with a stand-in for the network |
| Busy, declined, unanswered → `rejected` / `no_answer` | Implemented; the mapping is unit-tested, and `--simulate-status` exercises the whole pipeline without a phone |
| Voicemail | Defined only. Detecting an answering machine needs carrier-side detection, which is a paid feature; nothing in this repo can tell a machine from a person |
| 8 kHz audio quality, barge-in over a real line | Untested. These are the things only a real call teaches, and they are the honest gap |

### Trying the refused-call paths without a phone

A browser call is always answered, so it cannot produce a carrier status. To
exercise those paths end to end, inject one:

```shell
python dispatch_outbound.py --simulate-status 486   # busy      -> rejected
python dispatch_outbound.py --simulate-status 487   # ring-out  -> no_answer
```

The agent records the attempt, runs the post-call analysis with no model
involved (nobody spoke), and sends the Opik trace, exactly as a real refused
call would. Every such record carries `simulated: true`, in the call log, the
analysis file and the Opik trace, so a simulated attempt can never be mistaken
for a real one.

---

## Status and limits

| Area | State |
| --- | --- |
| Browser calls | Working end to end, including booking, callbacks and privacy |
| Post-call analysis | Working, verified on a live call |
| Opik trace | Working, verified on the server |
| Opik online rule | Working: a replayed call was scored automatically |
| Phone calls | Code complete and unit-tested; blocked on the SIP account, see above |
| Call recording | Each call's audio is saved to `recordings/` and attached to its Opik trace |
| Callback dialer | Callbacks are queued, but nothing dials them yet |
| `no_answer`, `rejected` | Mapped from SIP status; reachable on a real line, or with `--simulate-status` |
| `voicemail` | Defined only; needs carrier answering-machine detection |

### Known rough edges

- Calls are recorded, and LiveKit also keeps a copy on its Cloud dashboard.
  A real clinic must tell the patient the call is recorded before sharing
  anything medical; the agent does not say so yet. `RECORD_CALLS` in
  `config.py` turns recording off.

- Speech recognition mangles names on poor audio. The confirmation step handles
  it, at the cost of one extra turn.
- The agent sometimes offers the earliest slot before being asked. The tool
  refuses, and the agent asks properly instead, at the cost of one request.
- The simulated patients occasionally behave oddly (hanging up mid-call). Those
  runs are marked, not counted as agent failures.
