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

AGENT_NAME = "adit-outbound-agent"


def _room_name(patient_id: str) -> str:
    return f"call-{patient_id}-{secrets.token_hex(3)}"


async def dispatch(patient_id: str | None, phone: str | None, browser: bool) -> int:
    from livekit import api

    with db.session() as conn:
        db.init_db(conn)
        patient = db.get_patient(conn, patient_id) if patient_id else db.list_callable_patients(conn)[0]

    metadata = {"patient_id": patient["id"]}
    if not browser:
        metadata["transport"] = "sip"
        metadata["phone"] = phone or patient["phone"]
        if not metadata["phone"]:
            print(f"{patient['name']} has no phone number on file", file=sys.stderr)
            return 2
        if not os.environ.get("SIP_OUTBOUND_TRUNK_ID"):
            print("SIP_OUTBOUND_TRUNK_ID is not set; see telephony/README.md", file=sys.stderr)
            return 2

    room = _room_name(patient["id"])
    lk = api.LiveKitAPI()
    try:
        await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            agent_name=AGENT_NAME, room=room, metadata=json.dumps(metadata)))
    finally:
        await lk.aclose()

    where = "the browser" if browser else f"phone ending {str(metadata['phone'])[-4:]}"
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
    args = parser.parse_args(argv)
    return asyncio.run(dispatch(args.patient, args.phone, args.browser))


if __name__ == "__main__":
    sys.exit(main())
