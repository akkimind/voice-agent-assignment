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
