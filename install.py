"""Interactive setup: asks for every key and setting, checks each works, writes .env.

    ./install.sh                     # creates .venv, installs packages, then runs this
    ./.venv/bin/python install.py    # just the questions
    ./.venv/bin/python install.py --check   # test the keys already in .env
    ./.venv/bin/python install.py --trunk   # create the LiveKit SIP trunk from .env

Press Enter to keep a value already in .env. Secrets are typed hidden. Each key
is sent only to its own service, once, to check it works. .env is written
readable by you alone and is never committed.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
TRUNK_NAME = "adit-outbound"


@dataclass
class Field:
    name: str
    label: str
    secret: bool = False
    optional: bool = False
    pattern: str = ""          # a format check, not a guarantee
    example: str = ""


@dataclass
class Service:
    title: str
    why: str
    where: list[str]
    fields: list[Field]
    check: Callable[[dict[str, str]], str | None] | None = None   # an error, or None when it works
    optional: bool = False


# --- checks: one small request to each service ---------------------------------

def _get(url: str, headers: dict[str, str]) -> str | None:
    request = urllib.request.Request(url, headers={"User-Agent": "adit-installer", **headers})
    try:
        with urllib.request.urlopen(request, timeout=15):
            return None
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.read().decode(errors='ignore')[:160]}"
    except OSError as exc:
        return f"could not reach it: {exc}"


def check_livekit(v: dict[str, str]) -> str | None:
    from livekit import api

    async def probe() -> None:
        client = api.LiveKitAPI(url=v["LIVEKIT_URL"], api_key=v["LIVEKIT_API_KEY"],
                                api_secret=v["LIVEKIT_API_SECRET"])
        try:
            await client.room.list_rooms(api.ListRoomsRequest())
        finally:
            await client.aclose()
    try:
        asyncio.run(probe())
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:160]}"


def check_groq(v: dict[str, str]) -> str | None:
    return _get("https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {v['GROQ_API_KEY']}"})


def check_deepgram(v: dict[str, str]) -> str | None:
    return _get("https://api.deepgram.com/v1/projects", {"Authorization": f"Token {v['DEEPGRAM_API_KEY']}"})


def check_opik(v: dict[str, str]) -> str | None:
    if not v.get("OPIK_API_KEY"):
        return None
    try:
        import opik
        opik.Opik(api_key=v["OPIK_API_KEY"], workspace=v.get("OPIK_WORKSPACE") or None).auth_check()
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:160]}"


SERVICES = [
    Service(
        "LiveKit Cloud", "Runs the call: rooms, audio, the agent worker, phone dialling.",
        ["Sign up or log in at https://cloud.livekit.io and create a project.",
         "Project URL: Settings → Project → URL (it starts with wss://).",
         "Key and secret: Settings → API Keys → Create key. Copy both; the secret is shown once."],
        [Field("LIVEKIT_URL", "Project URL", pattern=r"wss://\S+", example="wss://your-project.livekit.cloud"),
         Field("LIVEKIT_API_KEY", "API key", pattern=r"\S{6,}"),
         Field("LIVEKIT_API_SECRET", "API secret", secret=True, pattern=r"\S{16,}")],
        check_livekit),
    Service(
        "Groq", "The agent's model (gpt-oss-120b), post-call analysis and the identity check.",
        ["Sign up or log in at https://console.groq.com",
         "API Keys (https://console.groq.com/keys) → Create API Key. Copy it; it is shown once.",
         "The free tier works; it allows about 200,000 tokens a day per model."],
        [Field("GROQ_API_KEY", "API key", secret=True, pattern=r"gsk_\S+")],
        check_groq),
    Service(
        "Deepgram", "Speech to text (Nova-3) and text to speech (Aura-2).",
        ["Sign up or log in at https://console.deepgram.com",
         "Pick the project in the Projects menu (top left) → Settings → API Keys → Create a New API Key.",
         "Give it a name, keep the default role, Create Key, then copy the secret; it is shown once."],
        [Field("DEEPGRAM_API_KEY", "API key", secret=True, pattern=r"[0-9a-fA-F]{32,}")],
        check_deepgram),
    Service(
        "Opik (optional)", "Receives each finished call and runs the online evaluation. Without it, calls still work.",
        ["Sign up or log in at https://www.comet.com/opik",
         "API key: your avatar (top right) → API Key → copy.",
         "Workspace: the name in the address bar after /opik/, e.g. comet.com/opik/<workspace>/projects."],
        [Field("OPIK_API_KEY", "API key", secret=True, optional=True),
         Field("OPIK_WORKSPACE", "Workspace name", optional=True)],
        check_opik, optional=True),
]

PROVIDERS = {
    "twilio": ("Twilio", [
        "Needs a paid (upgraded) account: trial accounts cannot use Elastic SIP Trunking.",
        "Console: https://console.twilio.com",
        "1. Credentials: Communication → Voice → Manage → Credential lists → Create new credential list.",
        "   Choose a username and password; you will type them below.",
        "2. Trunk: Communication → Voice → Elastic SIP Trunking → Manage → Trunks → Create new SIP Trunk.",
        "3. In the trunk, Termination tab → Termination SIP URI: pick a name, e.g. my-clinic.pstn.twilio.com.",
        "   Same tab → Authentication → Credential Lists → select the list from step 1.",
        "4. In the trunk, Numbers tab → Add a number you own: calls will show it as the caller ID.",
        "5. Skip Origination: that is for incoming calls.",
    ], r"[\w.-]+\.pstn\.twilio\.com"),
    "plivo": ("Plivo", [
        "Console: https://console.plivo.com",
        "1. Credentials: Zentrunk → Trunk Authentication → Credentials List → Add New.",
        "2. Trunk: Zentrunk → Outbound Trunks → Create New, and attach that credentials list.",
        "3. Copy the trunk's Termination SIP Domain, e.g. 12345678.zt.plivo.com.",
        "4. The caller ID must be a Plivo number you rent, or on a trial, a verified number.",
    ], r"[\w.-]+\.zt\.plivo\.com"),
    "other": ("another SIP provider", [
        "Any provider LiveKit supports: https://docs.livekit.io/telephony/",
        "You need its termination SIP address, a username and password for it, and a caller ID number.",
    ], r"[\w.-]+\.[a-z]{2,}"),
}


# --- .env ---------------------------------------------------------------------

def read_env() -> dict[str, str]:
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(ENV).items() if v} if ENV.exists() else {}


def write_env(values: dict[str, str]) -> None:
    """Rewrites .env in the order of .env.example, keeping any other keys at the end."""
    template = (ROOT / ".env.example").read_text().splitlines()
    out, done = [], set()
    for line in template:
        m = re.match(r"^([A-Z_]+)=", line)
        if m:
            key = m.group(1)
            done.add(key)
            out.append(f"{key}={_quote(values.get(key, ''))}")
        else:
            out.append(line)
    extra = [k for k in values if k not in done]
    if extra:
        out += ["", "# --- Other settings ---"] + [f"{k}={_quote(values[k])}" for k in extra]
    ENV.write_text("\n".join(out) + "\n")
    ENV.chmod(0o600)


def _quote(value: str) -> str:
    """Quoted only when it has to be: spaces or a # would otherwise be misread."""
    if value and re.search(r"[\s#'\"]", value):
        return "'" + value.replace("'", "\\'") + "'"
    return value


def masked(value: str) -> str:
    return "not set" if not value else value[:4] + "…" + value[-2:] if len(value) > 8 else "set"


# --- asking -------------------------------------------------------------------

def ask(f: Field, current: str) -> str:
    shown = masked(current) if f.secret else (current or "not set")
    hint = f" (e.g. {f.example})" if f.example and not current else ""
    prompt = f"  {f.label}{hint} [{shown}{', optional' if f.optional else ''}]: "
    while True:
        value = (getpass.getpass(prompt) if f.secret else input(prompt)).strip()
        if not value:
            if current or f.optional:
                return current
            print("    This one is needed.")
            continue
        if f.pattern and not re.fullmatch(f.pattern, value):
            print(f"    That does not look right{' (expected ' + f.example + ')' if f.example else ''}.")
            if input("    Use it anyway? [y/N]: ").strip().lower() != "y":
                continue
        return value


def yes(question: str, default: bool = False) -> bool:
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def section(title: str) -> None:
    print(f"\n{'─' * 70}\n{title}\n{'─' * 70}")


def configure_services(values: dict[str, str]) -> None:
    for s in SERVICES:
        section(s.title)
        print(s.why)
        for line in s.where:
            print(f"  • {line}")
        if s.optional and not any(values.get(f.name) for f in s.fields) and not yes("Set it up now?", True):
            continue
        while True:
            for f in s.fields:
                values[f.name] = ask(f, values.get(f.name, ""))
            if s.check is None or not all(values.get(f.name) for f in s.fields if not f.optional):
                break
            print("  Checking…", end=" ", flush=True)
            problem = s.check(values)
            if problem is None:
                print("works.")
                break
            print(f"failed: {problem}")
            if not yes("  Enter the values again?", True):
                print("  Kept as entered; fix them later by running install.py again.")
                break


# --- phone calls ----------------------------------------------------------------

def configure_telephony(values: dict[str, str]) -> None:
    section("Real phone calls (optional)")
    print("Browser calls need none of this. Real calls go out through a SIP trunk from a phone")
    print("provider; LiveKit dials through it. This step creates that trunk in LiveKit for you.")
    if not yes("Set up phone calls now?"):
        return
    choice = ""
    while choice not in PROVIDERS:
        choice = input("  Provider: twilio, plivo or other? ").strip().lower()
    name, steps, domain_pattern = PROVIDERS[choice]
    print(f"\n  First, in {name}:")
    for line in steps:
        print(f"    {line}")
    input("\n  Press Enter once that is done. ")

    values["SIP_TRUNK_ADDRESS"] = ask(
        Field("", "Termination SIP address", pattern=domain_pattern,
              example={"twilio": "my-clinic.pstn.twilio.com", "plivo": "12345678.zt.plivo.com"}.get(
                  choice, "sip.example.com")), values.get("SIP_TRUNK_ADDRESS", ""))
    values["SIP_CALLER_ID"] = ask(Field("", "Caller ID, the number calls come from", pattern=r"\+\d{8,15}",
                                        example="+14155550100"), values.get("SIP_CALLER_ID", ""))
    values["SIP_AUTH_USERNAME"] = ask(Field("", "SIP username (from the credential list)"),
                                      values.get("SIP_AUTH_USERNAME", ""))
    values["SIP_AUTH_PASSWORD"] = ask(Field("", "SIP password", secret=True), values.get("SIP_AUTH_PASSWORD", ""))
    print("\n  The phone a demo call rings. The patients in patients.json have fictional numbers,")
    print("  so for a real call this number is used instead. On a trial account it must be verified.")
    values["DEMO_DIAL_TO"] = ask(Field("", "Phone to call", optional=True, pattern=r"\+\d{8,15}",
                                       example="+919812345678"), values.get("DEMO_DIAL_TO", ""))
    make_trunk(values)


TRUNK_FIELDS = ("SIP_TRUNK_ADDRESS", "SIP_CALLER_ID", "SIP_AUTH_USERNAME", "SIP_AUTH_PASSWORD")


def make_trunk(values: dict[str, str]) -> bool:
    """Creates the LiveKit outbound trunk from the four SIP values, or updates
    ours if it exists, and records its id. Rerunning never duplicates it."""
    missing = [k for k in TRUNK_FIELDS if not values.get(k)]
    if missing:
        print(f"  Cannot create the trunk yet; not set: {', '.join(missing)}")
        return False
    print("  Creating the outbound trunk in LiveKit…", end=" ", flush=True)
    try:
        values["SIP_OUTBOUND_TRUNK_ID"] = asyncio.run(_create_trunk(values))
        print(f"done: {values['SIP_OUTBOUND_TRUNK_ID']}")
        return True
    except Exception as exc:
        print(f"failed: {type(exc).__name__}: {str(exc)[:200]}")
        return False


async def _create_trunk(values: dict[str, str]) -> str:
    from livekit import api
    client = api.LiveKitAPI(url=values["LIVEKIT_URL"], api_key=values["LIVEKIT_API_KEY"],
                            api_secret=values["LIVEKIT_API_SECRET"])
    try:
        info = api.SIPOutboundTrunkInfo(name=TRUNK_NAME, address=values["SIP_TRUNK_ADDRESS"],
                                        numbers=[values["SIP_CALLER_ID"]],
                                        auth_username=values["SIP_AUTH_USERNAME"],
                                        auth_password=values["SIP_AUTH_PASSWORD"])
        existing = await client.sip.list_sip_outbound_trunk(api.ListSIPOutboundTrunkRequest())
        ours = next((t for t in existing.items if t.name == TRUNK_NAME), None)
        if ours:
            await client.sip.update_sip_outbound_trunk(ours.sip_trunk_id, info)
            return ours.sip_trunk_id
        created = await client.sip.create_sip_outbound_trunk(api.CreateSIPOutboundTrunkRequest(trunk=info))
        return created.sip_trunk_id
    finally:
        await client.aclose()


# --- after the keys -----------------------------------------------------------

def run(*args: str) -> bool:
    return subprocess.run([sys.executable, *args], cwd=ROOT).returncode == 0


def finish(values: dict[str, str]) -> None:
    section("Database and Opik rule")
    if not (ROOT / "clinic.db").exists():
        print("Creating the clinic database: tables, the five fictional patients, a few taken slots.")
        run("db.py", "--reset")
    elif yes("Reset the clinic database to a clean calendar?"):
        run("db.py", "--reset")
    if values.get("OPIK_API_KEY") and yes("Create or update the online evaluation rule in Opik?", True):
        env = {**os.environ, **values}
        subprocess.run([sys.executable, "opik_rules.py", "apply"], cwd=ROOT, env=env)

    section("Done")
    for s in SERVICES:
        state = "set" if all(values.get(f.name) for f in s.fields if not f.optional) else "not set"
        print(f"  {s.title:<22} {state}")
    phone = "set up" if values.get("SIP_OUTBOUND_TRUNK_ID") else "not set up (browser calls only)"
    print(f"  {'Phone calls':<22} {phone}")
    print("\nNext:")
    print("  ./.venv/bin/python -m unittest                  # tests, no network")
    print("  ./.venv/bin/python agent.py console              # talk to it in this terminal")
    print("  ./.venv/bin/python agent.py dev                  # worker, then in another terminal:")
    print("  ./.venv/bin/python dispatch_outbound.py --browser   # a call you join in the browser")
    if values.get("SIP_OUTBOUND_TRUNK_ID"):
        print("  ./.venv/bin/python dispatch_outbound.py             # a real phone call")


def check_only(values: dict[str, str]) -> int:
    failed = 0
    for s in SERVICES:
        if s.check is None or not all(values.get(f.name) for f in s.fields if not f.optional):
            print(f"  {s.title:<22} not set")
            continue
        problem = s.check(values)
        failed += problem is not None
        print(f"  {s.title:<22} {'works' if problem is None else 'FAILED: ' + problem}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="install.py", description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="only test the keys already in .env")
    parser.add_argument("--trunk", action="store_true",
                        help="create or update the LiveKit SIP trunk from the SIP_* values in .env")
    args = parser.parse_args()
    values = read_env()
    if args.check:
        return check_only(values)
    if args.trunk:
        ok = make_trunk(values)
        write_env(values)
        return 0 if ok else 1
    print("Setup for the outbound voice agent. Press Enter to keep a value already set.")
    try:
        configure_services(values)
        write_env(values)
        configure_telephony(values)
        write_env(values)
    except (KeyboardInterrupt, EOFError):
        write_env(values)
        print("\nStopped. What was entered so far is saved in .env; run install.py again to continue.")
        return 1
    print(f"\nSaved {ENV.name} (readable only by you).")
    finish(values)
    return 0


if __name__ == "__main__":
    sys.exit(main())
