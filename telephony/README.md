# Phone calls

The agent does not use a phone provider's API. LiveKit places the call through a
**SIP trunk**, a connection to the phone network that the provider sells, and
the agent code does not care which provider it is: only four values in `.env`
change.

| `.env` value | What it is |
| --- | --- |
| `SIP_TRUNK_ADDRESS` | The provider's termination SIP address, e.g. `my-clinic.pstn.twilio.com` |
| `SIP_CALLER_ID` | The number calls come from, in international format, e.g. `+14155550100` |
| `SIP_AUTH_USERNAME`, `SIP_AUTH_PASSWORD` | The credential list attached to the provider's trunk |

From those, `install.py` creates the trunk in LiveKit and writes its id,
`SIP_OUTBOUND_TRUNK_ID`, which is the only one the agent reads at call time.

## Status: implemented, not funded

The telephony path is written, unit-tested and documented, but no call has been
placed over a real line. Every free route to a SIP trunk was blocked by the
provider, not by the code:

| Provider | What happened |
| --- | --- |
| Twilio | Account works and the number is verified, but a trial account cannot use Elastic SIP Trunking. `<Dial><Sip>` from Programmable Voice is refused as well: the call connects, Twilio plays an error, and no SIP INVITE ever reaches LiveKit. Verified with authentication removed from the inbound trunk, so credentials were not the cause |
| Plivo | Signup rejects free email domains, and a company domain too |
| Sinch | Signup rejects the same addresses, including a university one |
| LiveKit Phone Numbers | Inbound only; their docs state outbound needs a third-party provider |

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
./.venv/bin/python dispatch_outbound.py --simulate-status 486   # busy      -> rejected
./.venv/bin/python dispatch_outbound.py --simulate-status 487   # ring-out  -> no_answer
```

The agent records the attempt, runs the post-call analysis with no model
involved (nobody spoke), and sends the Opik trace, exactly as a real refused
call would. Every such record carries `simulated: true`, in the call log, the
analysis file and the Opik trace, so a simulated attempt can never be mistaken
for a real one.

---

## 1. Set up the trunk at the provider

### Twilio

At [console.twilio.com](https://console.twilio.com), on a **paid** account: a
trial account cannot use Elastic SIP Trunking.

1. **Communication → Voice → Manage → Credential lists → Create new credential
   list.** Choose a username and password: `SIP_AUTH_USERNAME` and
   `SIP_AUTH_PASSWORD`.
2. **Communication → Voice → Elastic SIP Trunking → Manage → Trunks → Create new
   SIP Trunk.**
3. In the trunk, **Termination** tab: set the **Termination SIP URI**, e.g.
   `my-clinic.pstn.twilio.com` (without `sip:`): `SIP_TRUNK_ADDRESS`. Same tab,
   **Authentication → Credential Lists**: select the list from step 1.
4. In the trunk, **Numbers** tab: add a number you own: `SIP_CALLER_ID`.
5. Skip **Origination**; that is for incoming calls.

### Plivo

At [console.plivo.com](https://console.plivo.com). The trial needs no card and
allows calls to India.

1. **Phone Numbers → Sandbox Numbers**: add and verify the phone you will call.
   A trial can only call verified numbers.
2. **Zentrunk → Trunk Authentication → Credentials List → Add New**: username
   5–20 letters and digits, password 5–20 characters including one of
   `~!@#$%^&*()_+`.
3. **Zentrunk → Outbound Trunks → Create New**, with that credentials list.
4. Copy its **Termination SIP Domain**, `<trunk_id>.zt.plivo.com`:
   `SIP_TRUNK_ADDRESS`.
5. The caller ID must be a number you rent from Plivo or have verified.

### Others

Any provider LiveKit supports ([docs.livekit.io/telephony](https://docs.livekit.io/telephony/)):
Telnyx, Sinch, Wavix, DIDLogic and more. You need the same four values.

---

## 2. Create the trunk in LiveKit

```shell
./.venv/bin/python install.py            # answer yes to phone calls; it asks for the four values
./.venv/bin/python install.py --trunk    # or: fill the four values into .env yourself, then this
```

Either one creates the outbound trunk through LiveKit's API, or updates it if it
already exists, and writes `SIP_OUTBOUND_TRUNK_ID` into `.env`. Change a value
and run `--trunk` again to update the trunk.

---

## 3. Call

```shell
./.venv/bin/python agent.py dev          # the worker, in one terminal
./.venv/bin/python dispatch_outbound.py  # a call, in another
```

In real use the agent calls the number in the patient's record. The five
patients in `patients.json` are fictional, with `+1 555 555 01xx` numbers, so a
demo needs a real phone: set `DEMO_DIAL_TO` in `.env`, or pass `--phone` for a
one-off. See [Running](../docs/setup.md#running).

### What happens on a call

| Step | Behaviour |
| --- | --- |
| Dialling | The agent joins the room first, then dials and waits for an answer, so it never speaks over the ringtone |
| Answered | It waits about 2.5 s for "hello", then asks for the patient |
| Nobody answers | Recorded as `no_answer` or `rejected`, analysed, and sent to Opik with no model involved |
| Goodbye | The agent ends the call itself, about 2 s after its closing words |
| Limits | 30 s of ringing, 15 minutes per call |

---

## Failure decoder

| Symptom | Cause |
| --- | --- |
| `ServerError`, no SIP traffic | malformed request fields |
| 401 / 403 | the SIP username or password does not match the provider's credential list |
| 404 | wrong `SIP_TRUNK_ADDRESS` |
| 503 | trunk address unreachable |
| 486 / 603 | the carrier or the callee rejected the call |
| Trial-only rejection to your mobile | the number is not verified (Twilio) or not sandboxed (Plivo) |
| 403 or 603 on a Twilio trial | termination is not enabled on trial accounts; upgrade the account |

| SIP status | Recorded outcome |
| --- | --- |
| 486, 600, 603 | `rejected` |
| 404, 408, 480, 487, 604 | `no_answer` |
| anything else, or no status | `incomplete`, with the error text |

---

## Calling India

- Plivo's account notes say voice traffic to the US and India needs no
  minimum-spend agreement; other countries do.
- From an account registered outside India, calls reach Indian numbers over
  international routes. Domestic routes are for India-registered businesses.
- India's rules require consent before commercial calls, and cold calling is
  prohibited. Calling your own verified number for a demo is fine; a real
  deployment needs DLT registration and a consent trail.
