from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from motor import calculate_predictions, get_state


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

# Source officielle unique des matchs du jour :
# ATP daily-schedule scanné ligne par ligne.
DAILY_SCRIPT_NAME = "fetch_day_lines_v6_10c_daily_schedule_line_scanner.py"

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
        from datetime import datetime
        from zoneinfo import ZoneInfo

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


def _history_rows_loaded() -> int:
    try:
        state = get_state()
        return int(state.get("history_rows_loaded", 0))
    except Exception:
        return 0


def _empty_response(
    status: str,
    message: str = "",
    target_day: str = "",
    stdout_tail: str = "",
    stderr_tail: str = "",
    command: str = "",
) -> Dict[str, Any]:
    return {
        "matches": [],
        "summary": {
            "totalRows": 0,
            "validRows": 0,
            "errorRows": 0,
            "nonAnalyzedRows": 0,
            "over80": 0,
            "vetoCount": 0,
            "jouables": 0,
        },
        "engine": {
            "name": "Tennis Motor V7",
            "version": "Bayesian Shrinkage",
            "historyYears": [2022, 2023, 2024, 2025],
            "historyRowsLoaded": _history_rows_loaded(),
            "premiumFormula": "Bayesian shrinkage blend of SWE, ATP, Rank, Form5, Form10, SurfaceForm5, Dominance",
            "threshold": "> 0.80",
            "status": status,
        },
        "daily": {
            "targetDay": target_day,
            "payloadCount": 0,
            "stdoutTail": stdout_tail[-4000:] if stdout_tail else "",
            "stderrTail": stderr_tail[-4000:] if stderr_tail else "",
            "command": command,
            "dailyScript": DAILY_SCRIPT_NAME,
        },
        "error": message,
    }


def calculate_from_matches(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not matches:
        return _empty_response(
            status="empty_payload",
            message="Aucun match exploitable dans le payload daily.",
        )

    result = calculate_predictions(matches)

    if not isinstance(result, dict):
        return _empty_response(
            status="calculate_invalid_result",
            message="Le moteur n'a pas renvoyé un objet JSON valide.",
        )

    return result


def _read_payload_for_day(target_day: str) -> Tuple[List[Dict[str, Any]], str]:
    """
    Sécurité anti-cache :
    on lit uniquement le payload daté du jour demandé.
    On ne retombe JAMAIS sur payload_latest.json.
    """
    payload_path = OUTPUT_DIR / f"payload_{target_day}.json"

    if not payload_path.exists():
        return [], ""

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    return _extract_matches_from_payload(payload), payload_path.name


def run_daily_fetch_sync(target_day: str) -> Dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    script = BASE_DIR / DAILY_SCRIPT_NAME

    if not script.exists():
        return _empty_response(
            status="script_missing",
            message=f"{DAILY_SCRIPT_NAME} introuvable sur Railway.",
            target_day=target_day,
        )

    cmd = [
        sys.executable,
        str(script),
        target_day,
        "--no-send-backend",
    ]

    command_text = " ".join(cmd)
    timeout_seconds = int(os.environ.get("FETCH_TIMEOUT_SECONDS", "540"))

    try:
        completed = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return _empty_response(
            status="daily_fetch_timeout",
            message=f"Extraction daily trop longue : timeout après {timeout_seconds} secondes.",
            target_day=target_day,
            stdout_tail=exc.stdout or "",
            stderr_tail=exc.stderr or "",
            command=command_text,
        )
    except Exception as exc:
        return _empty_response(
            status="daily_fetch_exception",
            message=f"Erreur lancement extraction daily : {exc}",
            target_day=target_day,
            stderr_tail=traceback.format_exc(),
            command=command_text,
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    if completed.returncode != 0:
        return _empty_response(
            status="daily_fetch_failed",
            message=f"Extraction daily échouée. Code retour : {completed.returncode}",
            target_day=target_day,
            stdout_tail=stdout,
            stderr_tail=stderr,
            command=command_text,
        )

    try:
        matches, payload_name = _read_payload_for_day(target_day)
    except Exception as exc:
        return _empty_response(
            status="payload_read_failed",
            message=f"Payload daily illisible : {exc}",
            target_day=target_day,
            stdout_tail=stdout,
            stderr_tail=stderr + "\n" + traceback.format_exc(),
            command=command_text,
        )

    result = calculate_from_matches(matches)

    result.setdefault("daily", {})
    result["daily"].update(
        {
            "targetDay": target_day,
            "payloadCount": len(matches),
            "payloadPath": payload_name,
            "stdoutTail": stdout[-1200:],
            "stderrTail": stderr[-1200:],
            "command": command_text,
            "dailyScript": DAILY_SCRIPT_NAME,
            "cacheProtection": "payload_latest_disabled",
        }
    )

    return result


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "Tennis Motor Railway Backend",
        "endpoints": [
            "/health",
            "/calculate",
            "/daily?day=today",
            "/daily?day=tomorrow",
            "/predictions?day=today",
            "/state",
        ],
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    try:
        return {
            "status": "ok",
            "service": "Tennis Motor Railway Backend",
            "engine": "loaded",
            "historyRowsLoaded": _history_rows_loaded(),
            "dailyScript": DAILY_SCRIPT_NAME,
            "dailyScriptFound": (BASE_DIR / DAILY_SCRIPT_NAME).exists(),
            "cacheProtection": "payload_latest_disabled",
            "hostMode": "railway_0.0.0.0_port_env",
        }
    except Exception as exc:
        return {
            "status": "error",
            "service": "Tennis Motor Railway Backend",
            "engine": "not_loaded",
            "error": str(exc),
            "dailyScript": DAILY_SCRIPT_NAME,
            "dailyScriptFound": (BASE_DIR / DAILY_SCRIPT_NAME).exists(),
        }


@app.post("/calculate")
async def calculate(request: Request) -> Dict[str, Any]:
    try:
        matches = await _read_request_matches(request)
        return calculate_from_matches(matches)
    except Exception as exc:
        return _empty_response(
            status="calculate_failed",
            message=str(exc),
            stderr_tail=traceback.format_exc(),
        )


@app.post("/predictions")
async def predictions_post(request: Request) -> Dict[str, Any]:
    try:
        matches = await _read_request_matches(request)
        return calculate_from_matches(matches)
    except Exception as exc:
        return _empty_response(
            status="predictions_post_failed",
            message=str(exc),
            stderr_tail=traceback.format_exc(),
        )


@app.get("/daily")
async def daily(day: str = Query("today")) -> Dict[str, Any]:
    try:
        target_day = normalize_day(day)
    except Exception as exc:
        return _empty_response(
            status="bad_day_parameter",
            message=f"Paramètre day invalide : {day} | {exc}",
            target_day=str(day),
        )

    return await asyncio.to_thread(run_daily_fetch_sync, target_day)


@app.get("/predictions")
async def predictions_get(day: str = Query("today")) -> Dict[str, Any]:
    try:
        target_day = normalize_day(day)
    except Exception as exc:
        return _empty_response(
            status="bad_day_parameter",
            message=f"Paramètre day invalide : {day} | {exc}",
            target_day=str(day),
        )

    return await asyncio.to_thread(run_daily_fetch_sync, target_day)


@app.get("/state")
async def state() -> Dict[str, Any]:
    try:
        s = get_state()
        return {
            "status": "ok",
            "historyRowsLoaded": s.get("history_rows_loaded", 0),
            "rankReferenceSize": len(s.get("rank_reference_points", [])),
            "dailyScript": DAILY_SCRIPT_NAME,
            "dailyScriptFound": (BASE_DIR / DAILY_SCRIPT_NAME).exists(),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
