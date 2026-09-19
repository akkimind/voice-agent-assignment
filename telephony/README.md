# Phone calls

The agent dials through a SIP trunk. LiveKit supports several providers and the
agent code does not care which one: only the trunk address and credentials
change. Plivo is the default here because its free trial needs no card and it
allows calls to India; Twilio is documented after it.

---

## Part 1 — what you do in Plivo

1. **Sign up** at [console.plivo.com](https://console.plivo.com). The trial
   credit arrives without a credit card.

2. **Verify the phone you want to call.** Phone Numbers → Sandbox Numbers → add
   your mobile and confirm the code. A trial account can only call numbers
   verified this way, which is exactly what a demo needs.

3. **Create a credential.** Zentrunk → Trunk Authentication → Credentials List →
   Add New.
   - Username: 5–20 letters and digits.
   - Password: 5–20 characters, must include one of `~!@#$%^&*()_+`.
   - Write both down; they go in `.env`, never in the repo.

4. **Create the outbound trunk.** Zentrunk → Outbound Trunks → Create New.
   - Attach the credential list from step 3.
   - Turn on TLS ("secure") if offered.

5. **Copy the Termination SIP Domain.** It looks like
   `<trunk_id>.zt.plivo.com`. No phone number purchase is needed for outbound
   calling.

6. **Decide the caller ID.** Plivo requires the number you call *from* to be one
   you own or have verified. If the console refuses your own mobile as a
   verified caller ID (verification is mainly US), buy the cheapest Plivo number
   and use that. This is the one step that may cost a little.

7. **Authorise the LiveKit CLI** once, so I can create the trunk:

   ```shell
   lk cloud auth
   ```

Then send me the **termination domain** and the **caller ID number**. Keep the
password to yourself; put it in `.env` as described below.

## Part 2 — what I do

1. Put the domain and caller ID into `outbound-trunk.json`.
2. Create the LiveKit trunk and record its id in `.env` as
   `SIP_OUTBOUND_TRUNK_ID`.
3. Place a silent test call: your phone rings, then silence, because no agent is
   in the room. Silence is a pass.
4. Run a real call with the agent and check the transcript, the analysis and the
   Opik trace.
5. Test the failure paths: you decline one call, and let another ring out.

## Part 3 — your `.env`

```shell
SIP_AUTH_USERNAME='...'      # step 3
SIP_AUTH_PASSWORD='...'      # step 3
SIP_OUTBOUND_TRUNK_ID='ST_...'   # I fill this in after creating the trunk
```

`.env` is gitignored and chmod 600. Nothing from it is printed in full: phone
numbers are masked to their last four digits everywhere they are logged or sent
to Opik.

---

## Creating the trunk

`outbound-trunk.json` holds the provider side:

```json
{
  "trunk": {
    "name": "adit-outbound",
    "address": "<trunk_id>.zt.plivo.com",
    "numbers": ["<caller id in E.164>"]
  }
}
```

```shell
lk sip outbound create telephony/outbound-trunk.json \
  --auth-user "$SIP_AUTH_USERNAME" \
  --auth-pass "$SIP_AUTH_PASSWORD"
```

Record the returned trunk id in `.env`.

### Silent test, without the agent

Put the trunk id into `participant.json`, then:

```shell
lk sip participant create telephony/participant.json
lk room participants get --room spike-room sip-test
```

Expect `kind` of `SIP` with a populated `sip.callID`. Your phone rings and the
line is silent, because no agent has joined. That is the pass condition.

---

## Making real calls

```shell
python agent.py dev                          # worker, in one terminal
python dispatch_outbound.py                  # first callable patient
python dispatch_outbound.py --patient p-002
python dispatch_outbound.py --phone +91...   # override the stored number
python dispatch_outbound.py --browser        # no phone; join the room yourself
```

`dispatch_outbound.py` only starts the agent in a new room. The agent dials from
inside the room, because it must be there before the phone rings.

### What happens on a call

| Step | Behaviour |
| --- | --- |
| Dialling | `wait_until_answered`, so the agent never speaks over the ringtone |
| Answered | The agent waits ~2.5s for "hello" first, then asks for the patient |
| Nobody answers | Recorded as `no_answer` or `rejected`, analysed and sent to Opik with no model involved |
| Goodbye | The agent hangs up ~2s after its closing words |
| Limits | 30s of ringing, 15 minutes per call |

---

## Failure decoder

| Symptom | Cause |
| --- | --- |
| `ServerError`, no SIP traffic | malformed request fields |
| 401 / 403 | credentials do not match the provider's credential list |
| 404 | wrong termination domain in `outbound-trunk.json` |
| 503 | trunk address unreachable |
| 486 / 603 | the carrier or the callee rejected the call |
| Trial-only rejection to your mobile | the number is not sandboxed (Plivo) or not verified (Twilio) |

| SIP status | Recorded outcome |
| --- | --- |
| 486, 600, 603 | `rejected` |
| 404, 408, 480, 487, 604 | `no_answer` |
| anything else, or no status | `incomplete`, with the error text |

---

## Calling India

- Plivo's own account notes say voice traffic to the US and India needs no
  minimum-spend agreement; other countries do.
- From an account registered outside India, calls reach Indian numbers over
  international routes. Domestic routes are for India-registered businesses.
- India's rules require consent before commercial calls, and cold calling is
  prohibited. Calling your own verified number for a demo is fine; a real
  deployment needs DLT registration and a consent trail.

---

## Twilio instead

The same shape, different console, at [console.twilio.com](https://console.twilio.com).
Trial accounts cannot use Elastic SIP Trunking; the account must be upgraded.

1. **Communication → Voice → Manage → Credential lists → Create new credential
   list**, with a username and password.
2. **Communication → Voice → Elastic SIP Trunking → Manage → Trunks → Create new
   SIP Trunk.**
3. **Termination** tab: set a Termination SIP URI and, under **Authentication →
   Credential Lists**, attach the list. Copy the domain, e.g.
   `your-trunk.pstn.twilio.com`, without the `sip:` prefix.
4. **Numbers** tab: attach the number you will call from.
5. Skip **Origination** entirely; that is for inbound calls.

Then `install.py` creates the LiveKit trunk from those values, or run the same
`lk sip outbound create` command with that domain.

A 403 or 603 on a Twilio trial usually means termination is not enabled on the
account, which is an account limitation rather than a configuration mistake.
