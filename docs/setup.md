# Setup and running

Everything about installing the project and running calls. The short version is in the [README](../README.md#quick-start).

## Setup

### Before you start

- **Python 3.11 or newer** and **git**. Check with `python3 --version`.
- Accounts, all with free tiers: **LiveKit Cloud**, **Groq**, **Deepgram**, and
  optionally **Opik**. For real phone calls, also a **paid** account with a SIP
  provider such as Twilio. The installer shows where each key is.

### Install

```shell
git clone https://github.com/akkimind/voice-agent-assignment.git
cd voice-agent-assignment
./install.sh
```

`install.sh` does three things:

1. Creates a virtual environment in `.venv`, so nothing is installed system-wide.
2. Installs the Python packages in `requirements.txt` into it:

   | Package | What it is for |
   | --- | --- |
   | `livekit-agents` | The voice agent framework: rooms, turns, tools, sessions |
   | `livekit-plugins-deepgram` | Speech to text and text to speech |
   | `livekit-plugins-openai` | Talks to Groq, which speaks the OpenAI API |
   | `livekit-plugins-silero` | Voice activity detection: when the caller is speaking |
   | `opik` | Sends each call to Opik and manages the evaluation rule |
   | `python-dotenv` | Reads the keys from `.env` |

   Their own dependencies (the LiveKit API client, the OpenAI client and so on)
   come with them.
3. Runs `install.py`, which asks for everything the project needs, one service
   at a time. For each one it prints the link and the console path to the key,
   asks for the value (secrets are typed hidden; Enter keeps a value already
   set), and checks that the key works with one small request to that service.
   Then it writes `.env`, readable only by you and never committed; creates the
   clinic database (`clinic.db`: tables, the five fictional patients, a few
   taken slots); creates the Opik evaluation rule; and, if you want phone calls,
   creates the SIP trunk in LiveKit for you (see [Phone calls](../telephony/README.md)).

Run it again at any time to change a value. `./.venv/bin/python install.py
--check` tests the keys already in `.env` without asking anything.

### Install by hand

The same steps without the installer:

```shell
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env    # then fill it in, see the table below
./.venv/bin/python db.py --reset          # clinic database with the fictional patients
./.venv/bin/python opik_rules.py apply    # the Opik evaluation rule (needs the Opik keys)
./.venv/bin/python install.py --check     # confirm the keys work
```

### Where each key comes from

Console menus move over time; if one is not where described, use the console's
search box.

| Variable | Service | Where to get it |
| --- | --- | --- |
| `LIVEKIT_URL` | [LiveKit Cloud](https://cloud.livekit.io) | Create a project. **Settings → General → Project URL**. It is shown without the scheme; write it as `wss://<project>.livekit.cloud` |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | [LiveKit Cloud](https://cloud.livekit.io) | **Settings → API keys → Create key**. The secret is shown once |
| `GROQ_API_KEY` | [Groq](https://console.groq.com) | **API Keys** ([console.groq.com/keys](https://console.groq.com/keys)) **→ Create API Key**. Shown once |
| `DEEPGRAM_API_KEY` | [Deepgram](https://console.deepgram.com) | **Projects** menu (top left) → your project → **Settings → API Keys → Create a New API Key**. Shown once |
| `OPIK_API_KEY` | [Opik](https://www.comet.com/opik) (optional) | Your avatar (top right) **→ API Key** |
| `OPIK_WORKSPACE` | [Opik](https://www.comet.com/opik) (optional) | The name in the address bar after `/opik/`, e.g. `comet.com/opik/<workspace>/projects` |
| `SIP_TRUNK_ADDRESS`, `SIP_CALLER_ID` | Your SIP provider (phone calls only) | The trunk's termination address and the number calls come from; see [Phone calls](../telephony/README.md) |
| `SIP_AUTH_USERNAME`, `SIP_AUTH_PASSWORD` | Your SIP provider (phone calls only) | The credential list attached to that trunk |
| `SIP_OUTBOUND_TRUNK_ID` | Written for you (phone calls only) | `install.py` creates the LiveKit trunk from the four values above and writes its id |
| `DEMO_DIAL_TO` | You (phone calls only) | The real phone a demo call rings, e.g. `+919812345678`; see [Running](#running) |

Without the Opik keys everything still works; calls simply are not sent to Opik.
Groq's free tier allows about 200,000 tokens a day per model: plenty for calls,
about 12 eval conversations a day.

## Running

```shell
./.venv/bin/python agent.py console --record   # talk to it in this terminal, recorded
./.venv/bin/python agent.py dev                # start the worker, then in a second terminal:
./.venv/bin/python dispatch_outbound.py --browser            # a call you join in the browser
./.venv/bin/python dispatch_outbound.py                      # a real phone call
./.venv/bin/python dispatch_outbound.py --patient p-002 --phone +919812345678
```

`agent.py dev` starts the worker: it connects to LiveKit and waits for calls.
`dispatch_outbound.py` starts one call. It picks a patient from the database
(the first without an appointment, or the one given with `--patient`), and asks
LiveKit to put the agent in a new room with that patient's id. It does not dial
itself; the agent does, from inside the room, so it is there before the phone
rings. Patients who already have an appointment are never called.

In real use the agent calls the number in the patient's record, and nobody
types a number. The five patients in `patients.json` are fictional, with
`+1 555 555 01xx` numbers that cannot ring anyone, so a demo needs a real phone
from somewhere else: `DEMO_DIAL_TO` in `.env` (the installer asks for it), or
`--phone` for a one-off. The order is `--phone`, then `DEMO_DIAL_TO`, then the
record. Numbers are in international format, `+` and the country code.

With `--browser` no phone is involved: it prints a link to join the call from
your browser as the patient. `--simulate-status 486` (busy) or `487` (no answer)
places no call at all and exercises what happens when nobody picks up.

### Every command

Run from the project folder. Only the first row is needed once; the rest are
used as needed. `py` below is short for `./.venv/bin/python`.

| When | Command | What it does |
| --- | --- | --- |
| Once, to set up | `./install.sh` | Creates `.venv`, installs the packages, asks for every key, writes `.env`, creates the database and the Opik rule |
| To change a key | `py install.py` | The questions again; Enter keeps each current value |
| To test the keys | `py install.py --check` | One request to each service, nothing changed |
| For phone calls | `py install.py --trunk` | Creates or updates the LiveKit SIP trunk from the `SIP_*` values in `.env` |
| Every time you make calls | `py agent.py dev` | Starts the agent worker; leave it running |
| To try it alone | `py agent.py console --record` | Talk to the agent in this terminal, no browser or phone |
| To place a call | `py dispatch_outbound.py --browser` | A call you join in the browser, as the patient |
| | `py dispatch_outbound.py` | A real phone call (needs the trunk) |
| | `py dispatch_outbound.py --patient p-002 --phone +91…` | A chosen patient, a chosen phone |
| | `py dispatch_outbound.py --simulate-status 487` | A call nobody answers, without a phone |
| To start over | `py db.py --reset` | Clean calendar: the five patients, no bookings or callbacks |
| After changing the rule | `py opik_rules.py apply` | Creates or updates the Opik evaluation rule; `show` prints it |
| To test the code | `py -m unittest` | The unit tests, no network |
| To test behaviour | `py -m evals` | Simulated calls against the real model (Groq free tier) |
| To see the scores | `py -m evals.scorecard` | Every metric against its target |
| After a bad live call | `py -m evals.from_log logs/<call>.jsonl --id <name> --expect booked` | Turns the call into a permanent test |

Each call writes:

| File | Contents |
| --- | --- |
| `logs/<room>_<time>.jsonl` | Every input and output with two timestamps, written by our own code |
| `session_reports/<room>_<time>.json` | Transcript and tool results |
| `session_reports/<room>_<time>_analysis.json` | The post-call analysis |
