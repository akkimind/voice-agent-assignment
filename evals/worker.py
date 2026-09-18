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


def cases() -> dict[str, Any]:
    from evals import personas
    return {c.id: c for c in personas.PERSONAS}


def patient_ids() -> list[str]:
    import config
    return [p["id"] for p in config.load_seed_patients()]


def run_job(case_id: str, run_no: int, patient_id: str) -> dict[str, Any]:
    """One conversation. A crash (both providers refusing under parallel load)
    is retried once and noted, so load does not read as an agent failure."""
    import asyncio
    from evals import conversation
    case = cases()[case_id]
    result = conversation.as_dict(asyncio.run(conversation.run(case, run_no, patient_id)))
    if result["status"] == "crash":
        first = result["errors"][:1]
        result = conversation.as_dict(asyncio.run(conversation.run(case, run_no, patient_id)))
        result["errors"].append(f"retried after crash: {first}")
    return result
