# Phase 1 — SIP trunk spike

Proves LiveKit can dial a real phone through Twilio. No agent involved.
A successful call rings, then plays silence, because no agent is in the room.

## Fill in two values

1. `outbound-trunk.json` → `address`
   Twilio Console → Elastic SIP Trunking → your trunk → Termination tab →
   Termination SIP URI. Use the domain only, no `sip:` prefix.

2. `participant.json` → `sip_trunk_id`
   Output of the `lk sip outbound create` command below.

## Run

Credentials must match the Twilio Voice credential list exactly.

```shell
export SIP_AUTH_USERNAME='...'
export SIP_AUTH_PASSWORD='...'

lk sip outbound create telephony/outbound-trunk.json \
  --auth-user "$SIP_AUTH_USERNAME" \
  --auth-pass "$SIP_AUTH_PASSWORD"

# paste the returned trunk ID into participant.json, then

lk sip participant create telephony/participant.json
```

## Verify

```shell
lk room participants get --room spike-room sip-test
```

Expect `kind` of `SIP`, plus non-empty `sip.callID` and `sip.twilio.callSid`.

## Failure decoder

| Symptom | Cause |
| --- | --- |
| `ServerError`, no SIP traffic | malformed request fields |
| 503 | wrong `address` in outbound-trunk.json |
| 403 | credential mismatch against the Twilio credential list |
| 404 / 486 / 603 | carrier rejected; check Twilio Voice logs |

A 403 or 603 on a trial account may mean Elastic SIP Trunking termination is
not enabled until the account is upgraded. That is an account limit, not config.

---

# Phase 7 — real calls with the agent

Once the trunk above works, the agent places calls itself.

## One-time setup

1. Create the trunk, then put its id in `.env`:

   ```shell
   SIP_OUTBOUND_TRUNK_ID='ST_...'
   ```

2. Start the worker in one terminal:

   ```shell
   python agent.py dev
   ```

## Make a call

```shell
python dispatch_outbound.py                 # first callable patient, real phone
python dispatch_outbound.py --patient p-002
python dispatch_outbound.py --phone +91...  # override the stored number
python dispatch_outbound.py --browser       # no phone; join the room yourself
```

The command only starts the agent in a new room. The agent dials from inside
the room, because it must be there before the phone rings.

## What happens on a call

| Step | Behaviour |
| --- | --- |
| Dialling | `wait_until_answered`, so the agent never speaks over the ringtone |
| Answered | The agent waits ~2.5s for "hello" first, then asks for the patient |
| Nobody answers | Recorded as `no_answer` or `rejected`, analysed and sent to Opik with no model involved |
| Goodbye | The agent hangs up ~2s after its closing words |
| Limits | 30s of ringing, 15 minutes per call |

Phone numbers are masked to their last four digits everywhere they are logged
or sent to Opik.

## Failure decoder, continued

| SIP status | Recorded outcome |
| --- | --- |
| 486, 600, 603 | `rejected` |
| 404, 408, 480, 487, 604 | `no_answer` |
| anything else, or no status | `incomplete`, with the error text |
