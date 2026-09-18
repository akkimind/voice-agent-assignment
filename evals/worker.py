"""Process-pool worker. Project modules are imported lazily: config reads
CLINIC_DB_PATH at import, and each worker must point it at its own database
before that happens."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def init(db_dir: str) -> None:
    os.environ["CLINIC_DB_PATH"] = str(Path(db_dir) / f"worker-{os.getpid()}.db")
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import logging
    logging.disable(logging.CRITICAL)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")


def run_job(suite: str, key: str, phrase: str, run_no: int, patient_id: str) -> dict[str, Any]:
    """One conversation. A crash (both providers refusing under parallel load)
    is retried once and noted, so load does not read as an agent failure."""
    import asyncio

    import config
    from evals import conversation, suites
    case = suites.build(suite, key, phrase)
    ids = {p["id"] for p in config.load_seed_patients()}
    # A live call from before the fictional patients keeps its lines, not its patient.
    patient_id = patient_id if patient_id in ids else sorted(ids)[0]
    result = conversation.as_dict(asyncio.run(conversation.run(case, run_no, patient_id)))
    if result["status"] == "crash":
        import time
        first = result["errors"][:1]
        time.sleep(20)   # both providers refused under load; let their limits reset
        result = conversation.as_dict(asyncio.run(conversation.run(case, run_no, patient_id)))
        result["errors"].append(f"retried after crash: {first}")
    return result


def patient_ids() -> list[str]:
    import config
    return [p["id"] for p in config.load_seed_patients()]
