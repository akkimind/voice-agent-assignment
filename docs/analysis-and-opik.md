# Post-call analysis and Opik

What happens after a call ends: the analysis, the Opik trace, and the online evaluation rule.

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
