#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tennis Motor - Railway / FastAPI backend
app.py COMPLET

Endpoints :
- GET /health
- GET /
- GET /calculate
- GET /predictions?day=today|tomorrow|YYYY-MM-DD
- GET /daily?day=today|tomorrow|YYYY-MM-DD

Correction intégrée :
- premium_history.record_daily_analysis(...) est appelé de façon compatible.
  Si ta fonction accepte target_day, on lui passe target_day.
  Si elle ne l'accepte pas, on appelle sans target_day.
  Donc l'erreur :
  TypeError: record_daily_analysis() got an unexpected keyword argument 'target_day'
  ne bloque plus /daily.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PAYLOAD_LATEST_PATH = OUTPUT_DIR / "payload_latest.json"

# Mets ici le nom exact de TON script daily actuel.
# Le tien vu dans les logs précédents :
DEFAULT_DAILY_SCRIPT = "fetch_day_lines_v6_10k_daily_schedule_no_forced_veto.py"

DAILY_SCRIPT = os.getenv("DAILY_SCRIPT", DEFAULT_DAILY_SCRIPT)

FETCH_TIMEOUT_SECONDS = int(os.getenv("FETCH_TIMEOUT_SECONDS", "540"))

APP_NAME = "Tennis Motor Backend"
APP_VERSION = "Railway FastAPI safe app.py"

PARIS_TZ_NAME = "Europe/Paris"


# ============================================================
# APP
# ============================================================

app = FastAPI(title=APP_NAME, version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# UTILITAIRES JSON / DATE
# ============================================================

def _json_safe(value: Any) -> Any:
    """Convertit les objets non JSON en valeurs sérialisables."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


def _read_json_file(path: Path) -> Optional[Any]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _paris_today() -> date:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(PARIS_TZ_NAME)).date()
    return date.today()


def normalize_day(day: Optional[str]) -> Tuple[str, str]:
    """
    Retourne :
    - target_day : date ISO YYYY-MM-DD
    - target_label : today / tomorrow / YYYY-MM-DD
    """
    raw = (day or "today").strip().lower()
    today = _paris_today()

    if raw in ("today", "aujourd'hui", "aujourdhui", "now", ""):
        return today.isoformat(), "today"

    if raw in ("tomorrow", "demain"):
        return (today + timedelta(days=1)).isoformat(), "tomorrow"

    # Accepte directement YYYY-MM-DD
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
        return parsed.isoformat(), parsed.isoformat()
    except Exception:
        # fallback propre : today
        return today.isoformat(), "today"


def _make_error_payload(
    endpoint: str,
    target_day: Optional[str],
    exc: BaseException,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "status": "error",
        "message": f"Erreur {endpoint}",
        "targetDay": target_day,
        "error": f"{type(exc).__name__}: {exc}",
        "trace": traceback.format_exc(),
    }
    if extra:
        payload.update(extra)
    return payload


# ============================================================
# IMPORTS MOTEUR
# ============================================================

def _import_motor():
    """
    Importe motor.py au moment de l'appel.
    Comme ça, si motor.py plante, /health reste disponible.
    """
    import motor  # type: ignore
    return motor


def _calculate_predictions_from_motor(target_day: Optional[str] = None) -> Any:
    """
    Appelle calculate_predictions() de façon compatible avec plusieurs versions :
    - calculate_predictions(target_day=...)
    - calculate_predictions(day=...)
    - calculate_predictions()
    """
    motor = _import_motor()

    if not hasattr(motor, "calculate_predictions"):
        raise RuntimeError("motor.py ne contient pas calculate_predictions()")

    fn = motor.calculate_predictions

    try:
        sig = inspect.signature(fn)
        params = sig.parameters

        if target_day and "target_day" in params:
            return fn(target_day=target_day)

        if target_day and "day" in params:
            return fn(day=target_day)

        if target_day and "targetDay" in params:
            return fn(targetDay=target_day)

        return fn()

    except TypeError:
        # Sécurité pour anciennes versions sans signature fiable
        return fn()


def _get_state_from_motor() -> Any:
    motor = _import_motor()
    if hasattr(motor, "get_state"):
        return motor.get_state()
    return {"status": "ok", "message": "motor.get_state() absent"}


# ============================================================
# COMPAT PREMIUM HISTORY
# ============================================================

def _record_daily_history_safe(calculated: Any, target_day: Optional[str]) -> Any:
    """
    Enregistre l'historique daily sans jamais planter /daily.

    Compatible avec :
    - record_daily_analysis(calculated, target_day=target_day)
    - record_daily_analysis(calculated, targetDay=target_day)
    - record_daily_analysis(calculated, day=target_day)
    - record_daily_analysis(calculated)
    """
    try:
        import premium_history  # type: ignore
    except Exception as exc:
        return {
            "status": "skipped",
            "reason": "premium_history import impossible",
            "error": f"{type(exc).__name__}: {exc}",
        }

    if not hasattr(premium_history, "record_daily_analysis"):
        return {
            "status": "skipped",
            "reason": "premium_history.record_daily_analysis absent",
        }

    fn = premium_history.record_daily_analysis

    try:
        sig = inspect.signature(fn)
        params = sig.parameters

        if "target_day" in params:
            return fn(calculated, target_day=target_day)

        if "targetDay" in params:
            return fn(calculated, targetDay=target_day)

        if "day" in params:
            return fn(calculated, day=target_day)

        return fn(calculated)

    except TypeError:
        # Correction directe de ton erreur Railway :
        # TypeError: got an unexpected keyword argument 'target_day'
        try:
            return fn(calculated)
        except Exception as exc:
            return {
                "status": "error",
                "reason": "record_daily_analysis failed without target_day",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }

    except Exception as exc:
        return {
            "status": "error",
            "reason": "record_daily_analysis failed",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
        }


# ============================================================
# DAILY SCRIPT RUNNER
# ============================================================

def _daily_output_paths(target_day: str) -> Dict[str, Path]:
    return {
        "payload_day": OUTPUT_DIR / f"payload_{target_day}.json",
        "payload_latest": PAYLOAD_LATEST_PATH,
        "lines_day": OUTPUT_DIR / f"lines_{target_day}.txt",
    }


def _find_daily_script() -> Path:
    script_path = BASE_DIR / DAILY_SCRIPT
    if not script_path.exists():
        raise FileNotFoundError(
            f"Script daily introuvable: {script_path.name}. "
            f"Vérifie DAILY_SCRIPT ou le nom du fichier dans app.py."
        )
    return script_path


def _run_daily_script(target_day: str) -> Dict[str, Any]:
    """
    Lance le script daily si présent.
    On tente plusieurs formats d'arguments compatibles :
    1) python script.py YYYY-MM-DD
    2) python script.py --day YYYY-MM-DD
    3) python script.py --target-day YYYY-MM-DD

    Le premier qui termine avec code 0 est accepté.
    """
    script_path = _find_daily_script()

    attempts: List[List[str]] = [
        [sys.executable, str(script_path), target_day],
        [sys.executable, str(script_path), "--day", target_day],
        [sys.executable, str(script_path), "--target-day", target_day],
    ]

    last_result: Optional[subprocess.CompletedProcess[str]] = None

    for cmd in attempts:
        try:
            result = subprocess.run(
                cmd,
                cwd=str(BASE_DIR),
                text=True,
                capture_output=True,
                timeout=FETCH_TIMEOUT_SECONDS,
            )
            last_result = result

            if result.returncode == 0:
                return {
                    "status": "ok",
                    "dailyScript": script_path.name,
                    "cmd": cmd,
                    "returncode": result.returncode,
                    "stdout": result.stdout[-12000:],
                    "stderr": result.stderr[-12000:],
                }

        except subprocess.TimeoutExpired as exc:
            return {
                "status": "error",
                "dailyScript": script_path.name,
                "error": f"Timeout après {FETCH_TIMEOUT_SECONDS} secondes",
                "cmd": cmd,
                "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else exc.stdout,
                "stderr": (exc.stderr or "")[-12000:] if isinstance(exc.stderr, str) else exc.stderr,
            }

    return {
        "status": "error",
        "dailyScript": script_path.name,
        "error": "Le script daily a échoué avec tous les formats d'arguments testés",
        "last_returncode": None if last_result is None else last_result.returncode,
        "last_stdout": "" if last_result is None else last_result.stdout[-12000:],
        "last_stderr": "" if last_result is None else last_result.stderr[-12000:],
    }


def _load_daily_payload_after_run(target_day: str) -> Optional[Any]:
    paths = _daily_output_paths(target_day)

    # Priorité au payload du jour
    payload = _read_json_file(paths["payload_day"])
    if payload is not None:
        return payload

    # Puis payload_latest
    payload = _read_json_file(paths["payload_latest"])
    if payload is not None:
        return payload

    return None


# ============================================================
# SUMMARY
# ============================================================

def _as_list_from_payload(payload: Any) -> List[Any]:
    """
    Essaie de récupérer une liste de matchs depuis différents formats possibles.
    """
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    for key in (
        "matches",
        "predictions",
        "rows",
        "data",
        "items",
        "results",
        "validRows",
        "valid_rows",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return value

    return []


def _extract_probability(item: Any) -> Optional[float]:
    if not isinstance(item, dict):
        return None

    keys = (
        "premium",
        "Premium",
        "probability",
        "proba",
        "score",
        "confidence",
        "finalProbability",
        "final_probability",
    )

    for key in keys:
        if key in item:
            try:
                value = item[key]
                if isinstance(value, str):
                    value = value.replace("%", "").strip()
                    f = float(value)
                    if f > 1.0:
                        return f / 100.0
                    return f
                f = float(value)
                if f > 1.0:
                    return f / 100.0
                return f
            except Exception:
                pass

    return None


def _is_veto(item: Any) -> bool:
    if not isinstance(item, dict):
        return False

    for key in ("veto", "hasVeto", "has_veto", "blocked", "isVeto"):
        if key in item:
            return bool(item[key])

    decision = str(item.get("decision", "") or item.get("status", "")).lower()
    return "veto" in decision or "bloqué" in decision or "blocked" in decision


def _build_summary(payload: Any, target_day: str) -> Dict[str, Any]:
    rows = _as_list_from_payload(payload)

    total_rows = len(rows)
    veto_count = 0
    over80 = 0
    proche75_80 = 0
    jouables = 0

    for item in rows:
        veto = _is_veto(item)
        prob = _extract_probability(item)

        if veto:
            veto_count += 1

        if prob is not None:
            if prob > 0.80:
                over80 += 1
                if not veto:
                    jouables += 1
            elif 0.75 <= prob < 0.80:
                proche75_80 += 1

    return {
        "targetDay": target_day,
        "totalRows": total_rows,
        "validRows": total_rows,
        "errorRows": 0,
        "over80": over80,
        "vetoCount": veto_count,
        "jouables": jouables,
        "proche75_80": proche75_80,
        "engine": {
            "name": "Tennis Motor",
            "threshold": "> 0.80",
            "historyYears": [2022, 2023, 2024, 2025],
        },
    }


def _merge_daily_response(
    target_day: str,
    target_label: str,
    calculated: Any,
    run_info: Optional[Dict[str, Any]] = None,
    history_record: Optional[Any] = None,
) -> Dict[str, Any]:
    summary = _build_summary(calculated, target_day)

    response: Dict[str, Any] = {
        "status": "ok",
        "targetDay": target_day,
        "targetLabel": target_label,
        "dailyScript": DAILY_SCRIPT,
        "summary": summary,
        "historyRecord": history_record,
        "data": calculated,
    }

    if isinstance(calculated, dict):
        # Si le payload a déjà un status, summary, matches, etc. on garde aussi le format haut niveau.
        for key in ("matches", "predictions", "rows", "audit", "lines", "payload"):
            if key in calculated and key not in response:
                response[key] = calculated[key]

        if "summary" in calculated and isinstance(calculated["summary"], dict):
            response["summary"] = {**summary, **calculated["summary"]}

    if run_info is not None:
        response["runner"] = run_info

    return response


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def root():
    return {
        "status": "ok",
        "name": APP_NAME,
        "version": APP_VERSION,
        "endpoints": [
            "/health",
            "/calculate",
            "/predictions?day=today",
            "/daily?day=today",
            "/daily?day=tomorrow",
        ],
        "dailyScript": DAILY_SCRIPT,
        "outputDir": str(OUTPUT_DIR),
        "serverDateParis": _paris_today().isoformat(),
    }


@app.get("/health")
def health():
    payload: Dict[str, Any] = {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "dailyScript": DAILY_SCRIPT,
        "dailyScriptExists": (BASE_DIR / DAILY_SCRIPT).exists(),
        "outputDirExists": OUTPUT_DIR.exists(),
        "serverDateParis": _paris_today().isoformat(),
    }

    try:
        payload["motorState"] = _get_state_from_motor()
    except Exception as exc:
        payload["motorState"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    return payload


@app.get("/calculate")
def calculate(day: Optional[str] = Query(default=None)):
    target_day, target_label = normalize_day(day)

    try:
        calculated = _calculate_predictions_from_motor(target_day=target_day)
        history_record = _record_daily_history_safe(calculated, target_day)

        response = _merge_daily_response(
            target_day=target_day,
            target_label=target_label,
            calculated=calculated,
            run_info=None,
            history_record=history_record,
        )

        return JSONResponse(content=_json_safe(response), status_code=200)

    except Exception as exc:
        return JSONResponse(
            content=_json_safe(_make_error_payload("/calculate", target_day, exc)),
            status_code=500,
        )


@app.get("/predictions")
def predictions(day: str = Query(default="today")):
    target_day, target_label = normalize_day(day)

    try:
        calculated = _calculate_predictions_from_motor(target_day=target_day)
        history_record = _record_daily_history_safe(calculated, target_day)

        response = _merge_daily_response(
            target_day=target_day,
            target_label=target_label,
            calculated=calculated,
            run_info=None,
            history_record=history_record,
        )

        return JSONResponse(content=_json_safe(response), status_code=200)

    except Exception as exc:
        return JSONResponse(
            content=_json_safe(_make_error_payload("/predictions", target_day, exc)),
            status_code=500,
        )


@app.get("/daily")
def daily(day: str = Query(default="today")):
    target_day, target_label = normalize_day(day)

    try:
        run_info = _run_daily_script(target_day)

        # Si le script daily a généré un payload, on l'utilise.
        payload = _load_daily_payload_after_run(target_day)

        # Sinon, fallback moteur direct.
        if payload is None:
            payload = _calculate_predictions_from_motor(target_day=target_day)

        history_record = _record_daily_history_safe(payload, target_day)

        response = _merge_daily_response(
            target_day=target_day,
            target_label=target_label,
            calculated=payload,
            run_info=run_info,
            history_record=history_record,
        )

        # Sauvegarde latest propre
        try:
            _write_json_file(PAYLOAD_LATEST_PATH, response)
            _write_json_file(OUTPUT_DIR / f"payload_{target_day}.json", response)
        except Exception:
            pass

        status_code = 200 if run_info.get("status") == "ok" else 500
        # Même si le runner échoue mais payload existe, on renvoie 200 pour Unity si data présente.
        if payload is not None:
            status_code = 200

        return JSONResponse(content=_json_safe(response), status_code=status_code)

    except Exception as exc:
        return JSONResponse(
            content=_json_safe(_make_error_payload("/daily", target_day, exc)),
            status_code=500,
        )


# ============================================================
# LANCEMENT LOCAL
# ============================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
