from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from motor import calculate_predictions, get_state


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PAYLOAD_LATEST_PATH = OUTPUT_DIR / "payload_latest.json"

app = FastAPI(title="Tennis Motor Railway Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _paris_today() -> date:
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime

        return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:
        return date.today()


def normalize_day(day: str) -> str:
    value = (day or "today").strip().lower()
    today = _paris_today()

    if value == "today":
        return today.isoformat()

    if value == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    # Validation YYYY-MM-DD
    return date.fromisoformat(value).isoformat()


def _extract_matches_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in ("matches", "payload", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

    return []


async def _read_request_matches(request: Request) -> List[Dict[str, Any]]:
    try:
        payload = await request.json()
    except Exception:
        payload = []
    return _extract_matches_from_payload(payload)


def calculate_from_matches(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not matches:
        return {
            "matches": [],
            "summary": {
                "totalRows": 0,
                "validRows": 0,
                "errorRows": 0,
                "over80": 0,
                "vetoCount": 0,
                "jouables": 0,
            },
            "engine": {
                "name": "Tennis Motor V7",
                "status": "empty_payload",
            },
        }

    return calculate_predictions(matches)


def run_daily_fetch_sync(target_day: str) -> Dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    script = BASE_DIR / "fetch_day_lines_v6_9_strict_day_filter.py"
    if not script.exists():
        raise RuntimeError("fetch_day_lines_v6_9_strict_day_filter.py introuvable sur Railway.")

    cmd = [
        sys.executable,
        str(script),
        target_day,
        "--no-send-backend",
    ]

    timeout_seconds = int(os.environ.get("FETCH_TIMEOUT_SECONDS", "180"))

    completed = subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    if completed.returncode != 0:
        raise RuntimeError(
            "Extraction daily échouée.\n"
            f"Commande: {' '.join(cmd)}\n"
            f"STDOUT:\n{stdout[-4000:]}\n"
            f"STDERR:\n{stderr[-4000:]}"
        )

    payload_path = OUTPUT_DIR / f"payload_{target_day}.json"
    if not payload_path.exists():
        payload_path = PAYLOAD_LATEST_PATH

    if not payload_path.exists():
        raise RuntimeError("Payload daily introuvable après extraction.")

    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Payload daily illisible: {payload_path} | {exc}") from exc

    matches = _extract_matches_from_payload(payload)
    result = calculate_from_matches(matches)

    result.setdefault("daily", {})
    result["daily"].update(
        {
            "targetDay": target_day,
            "payloadCount": len(matches),
            "payloadPath": str(payload_path.name),
            "stdoutTail": stdout[-1200:],
            "stderrTail": stderr[-1200:],
        }
    )

    return result


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "Tennis Motor Railway Backend",
        "endpoints": ["/health", "/calculate", "/daily?day=today", "/predictions?day=today"],
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        state = get_state()
        history_rows = int(state.get("history_rows_loaded", 0))
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
        }

    return {
        "status": "ok",
        "engine": "loaded",
        "historyRowsLoaded": history_rows,
    }


@app.post("/calculate")
async def calculate(request: Request) -> Dict[str, Any]:
    matches = await _read_request_matches(request)
    return calculate_from_matches(matches)


@app.post("/predictions")
async def predictions_post(request: Request) -> Dict[str, Any]:
    matches = await _read_request_matches(request)
    return calculate_from_matches(matches)


@app.get("/daily")
async def daily(day: str = Query("today")) -> Dict[str, Any]:
    target_day = normalize_day(day)
    return await asyncio.to_thread(run_daily_fetch_sync, target_day)


@app.get("/predictions")
async def predictions_get(day: str = Query("today")) -> Dict[str, Any]:
    target_day = normalize_day(day)
    return await asyncio.to_thread(run_daily_fetch_sync, target_day)


@app.get("/state")
def state() -> Dict[str, Any]:
    s = get_state()
    return {
        "historyRowsLoaded": s.get("history_rows_loaded", 0),
        "rankReferenceSize": len(s.get("rank_reference_points", [])),
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
