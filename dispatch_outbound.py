"""Start one outbound call from the command line.

    python dispatch_outbound.py                    # first callable patient, real phone
    python dispatch_outbound.py --patient p-002
    python dispatch_outbound.py --phone +91...     # override the stored number
    python dispatch_outbound.py --browser          # no phone; join the room yourself

This only asks LiveKit to run the agent in a new room. The agent does the
dialling itself, because it must be in the room before the phone rings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

import config
import db
import scheduling

AGENT_NAME = "adit-outbound-agent"


def _room_name(patient_id: str) -> str:
    return f"call-{patient_id}-{secrets.token_hex(3)}"


async def dispatch(patient_id: str | None, phone: str | None, browser: bool,
                   simulate_status: int | None = None) -> int:
    from livekit import api

    with db.session() as conn:
        db.init_db(conn)
        if patient_id:
            patient = db.get_patient(conn, patient_id)
            booked = db.upcoming_appointment(conn, patient_id, scheduling.to_utc_iso(db.utc_now()))
            if booked:
                print(f"{patient['name']} already has an appointment ({booked['reference']}); not calling.",
                      file=sys.stderr)
                return 2
        else:
            waiting = db.list_callable_patients(conn)
            if not waiting:
                print("Nobody is waiting to be called: every patient already has an appointment.",
                      file=sys.stderr)
                return 2
            patient = waiting[0]

    metadata = {"patient_id": patient["id"]}
    if not browser:
        metadata["transport"] = "sip"
        # The seed records carry fictional numbers; a real one for demos comes
        # from .env so it never lands in the repository.
        metadata["phone"] = phone or os.environ.get("DEMO_DIAL_TO") or patient["phone"]
        if not metadata["phone"]:
            print(f"{patient['name']} has no phone number on file", file=sys.stderr)
            return 2
        if simulate_status:
            # No call is placed: the refusal is injected so the recording,
            # analysis and Opik trace for a refused call can be exercised.
            metadata["simulate_status"] = simulate_status
        elif not os.environ.get("SIP_OUTBOUND_TRUNK_ID"):
            print("SIP_OUTBOUND_TRUNK_ID is not set; see telephony/README.md", file=sys.stderr)
            return 2

    room = _room_name(patient["id"])
    lk = api.LiveKitAPI()
    try:
        await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            agent_name=AGENT_NAME, room=room, metadata=json.dumps(metadata)))
    finally:
        await lk.aclose()

    if browser:
        # A join link scoped to this one room, valid for an hour.
        from datetime import timedelta
        from urllib.parse import urlencode
        token = (api.AccessToken().with_identity("patient").with_name("Patient")
                 .with_grants(api.VideoGrants(room_join=True, room=room))
                 .with_ttl(timedelta(hours=1)).to_jwt())
        link = "https://meet.livekit.io/custom?" + urlencode(
            {"liveKitUrl": os.environ["LIVEKIT_URL"], "token": token})
        print("join in your browser (allow the microphone):")
        print(link)

    where = ("the browser" if browser else
             f"a simulated SIP {simulate_status} refusal" if simulate_status else
             f"phone ending {str(metadata['phone'])[-4:]}")
    print(f"calling {patient['name']} ({patient['id']}) on {where}")
    print(f"room: {room}")
    print(f"the agent writes logs/{room}_*.jsonl and session_reports/{room}_*.json")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).parent / ".env")
    parser = argparse.ArgumentParser(description="Start one outbound call.")
    parser.add_argument("--patient", help=f"patient id, e.g. {config.load_seed_patients()[0]['id']}")
    parser.add_argument("--phone", help="ring this number instead of the stored one")
    parser.add_argument("--browser", action="store_true", help="no phone call; join the room yourself")
    parser.add_argument("--simulate-status", type=int, metavar="CODE",
                        help="place no call; record a refusal with this SIP status, e.g. 486 or 487")
    args = parser.parse_args(argv)
    return asyncio.run(dispatch(args.patient, args.phone, args.browser, args.simulate_status))


if __name__ == "__main__":
    sys.exit(main())
