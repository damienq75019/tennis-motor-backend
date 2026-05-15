#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor Railway Backend - app.py

Endpoints principaux :
- /
- /health
- /daily?day=today|tomorrow|YYYY-MM-DD
- /predictions?day=today
- /calculate
- /history
- /history-refresh
- /history-reset?confirm=RESET
- /update-2026-history?force=true

Pipeline /daily :
1) règle les anciens pending premium si possible
2) met data/2026.csv à jour AVANT l'analyse du jour
   => Jeff + résultats premium déjà terminés
3) lance le fetch daily
4) calcule le moteur
5) enregistre les nouveaux picks PREMIUM jouables
6) renvoie les données Unity
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

import premium_history


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

FETCH_SCRIPT = os.getenv("DAILY_FETCH_SCRIPT", "fetch_day_lines_v6_10k_daily_schedule_no_forced_veto.py")
FETCH_TIMEOUT_SECONDS = int(os.getenv("FETCH_TIMEOUT_SECONDS", "540"))
AUTO_UPDATE_2026 = os.getenv("AUTO_UPDATE_2026", "1").strip().lower() not in {"0", "false", "no", "off"}
UPDATE_2026_SCRIPT = os.getenv("UPDATE_2026_SCRIPT", "update_2026_history.py")
UPDATE_MARKER = OUTPUT_DIR / "update_2026_last_run.json"

SERVICE_NAME = "Tennis Motor Railway Backend"

app = FastAPI(title=SERVICE_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _paris_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("Europe/Paris"))
    return datetime.now()


def normalize_day(day: Optional[str]) -> str:
    today = _paris_now().date()
    if not day or str(day).strip().lower() in {"today", "aujourd'hui", "aujourdhui"}:
        return today.isoformat()
    if str(day).strip().lower() in {"tomorrow", "demain"}:
        return (today + timedelta(days=1)).isoformat()
    # YYYY-MM-DD attendu
    s = str(day).strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except Exception:
        return today.isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _json_response(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status_code)


def _tail(s: str, n: int = 5000) -> str:
    if not s:
        return ""
    return s[-n:]


def motor_state() -> Dict[str, Any]:
    try:
        import motor  # type: ignore
        if hasattr(motor, "get_state"):
            st = motor.get_state()
            if isinstance(st, dict):
                return st
            return {"state": str(st)}
        return {"status": "ok", "message": "motor importé"}
    except Exception as e:
        return {"status": "error", "message": repr(e)}


def reset_motor_state() -> Dict[str, Any]:
    try:
        import motor  # type: ignore
        if hasattr(motor, "_STATE"):
            setattr(motor, "_STATE", None)
            return {"status": "ok", "message": "motor._STATE réinitialisé"}
        if hasattr(motor, "reset_state"):
            motor.reset_state()
            return {"status": "ok", "message": "motor.reset_state() exécuté"}
        return {"status": "ok", "message": "aucun cache moteur connu"}
    except Exception as e:
        return {"status": "error", "message": repr(e)}


def run_update_2026_history(force: bool = False, reason: str = "") -> Dict[str, Any]:
    if not AUTO_UPDATE_2026 and not force:
        return {"enabled": False, "ran": False, "ok": True, "status": "disabled"}

    today = _paris_now().date().isoformat()
    last = load_json(UPDATE_MARKER, {})
    if not force and isinstance(last, dict) and last.get("date") == today and last.get("ok") is True:
        return {
            "enabled": True,
            "ran": False,
            "ok": True,
            "status": "already_done_today",
            "date": today,
            "script": UPDATE_2026_SCRIPT,
            "last_run": last,
        }

    script_path = BASE_DIR / UPDATE_2026_SCRIPT
    if not script_path.exists():
        return {
            "enabled": True,
            "ran": False,
            "ok": False,
            "status": "script_missing",
            "script": str(script_path),
        }

    env = os.environ.copy()
    env.setdefault("OUTPUT_DIR", str(OUTPUT_DIR))
    env.setdefault("DATA_DIR", str(DATA_DIR))

    proc = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=max(60, min(FETCH_TIMEOUT_SECONDS, 900)),
    )
    ok = proc.returncode == 0
    marker = {
        "date": today,
        "ok": ok,
        "script": UPDATE_2026_SCRIPT,
        "reason": reason,
        "ranAtParis": _paris_now().isoformat(),
        "returncode": proc.returncode,
        "stdoutTail": _tail(proc.stdout, 4000),
        "stderrTail": _tail(proc.stderr, 4000),
        "motorReload": reset_motor_state(),
    }
    save_json(UPDATE_MARKER, marker)

    return {
        "enabled": True,
        "ran": True,
        "ok": ok,
        "status": "ok" if ok else "error",
        "date": today,
        "script": UPDATE_2026_SCRIPT,
        "returncode": proc.returncode,
        "stdoutTail": _tail(proc.stdout, 5000),
        "stderrTail": _tail(proc.stderr, 5000),
        "motorReload": marker["motorReload"],
    }


def run_daily_fetch(target_day: str) -> Dict[str, Any]:
    script_path = BASE_DIR / FETCH_SCRIPT
    if not script_path.exists():
        return {
            "status": "error",
            "script": FETCH_SCRIPT,
            "message": f"Script introuvable: {script_path}",
            "returncode": 127,
            "stdoutTail": "",
            "stderrTail": "",
        }

    cmd = [sys.executable, str(script_path), target_day, "--no-send-backend"]
    proc = subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        env={**os.environ, "OUTPUT_DIR": str(OUTPUT_DIR), "DATA_DIR": str(DATA_DIR)},
        capture_output=True,
        text=True,
        timeout=FETCH_TIMEOUT_SECONDS,
    )

    return {
        "status": "ok" if proc.returncode == 0 else "error",
        "script": FETCH_SCRIPT,
        "targetDay": target_day,
        "returncode": proc.returncode,
        "command": " ".join(cmd),
        "stdoutTail": _tail(proc.stdout, 6000),
        "stderrTail": _tail(proc.stderr, 6000),
    }


def read_payload_file(target_day: str) -> Dict[str, Any]:
    candidates = [
        OUTPUT_DIR / f"payload_{target_day}.json",
        OUTPUT_DIR / "payload_latest.json",
        OUTPUT_DIR / f"result_{target_day}.json",
        OUTPUT_DIR / "result_latest.json",
    ]
    for path in candidates:
        data = load_json(path, None)
        if isinstance(data, dict):
            data.setdefault("_loadedFrom", str(path))
            return data
    return {"matches": [], "_loadedFrom": "", "status": "empty"}


def read_unity_input_lines(target_day: str) -> List[str]:
    candidates = [
        OUTPUT_DIR / "unity_input.txt",
        OUTPUT_DIR / f"lines_{target_day}.txt",
        OUTPUT_DIR / "lines_latest.txt",
    ]
    for path in candidates:
        try:
            if path.exists():
                return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except Exception:
            pass
    return []


def _looks_like_prediction_payload(x: Any) -> bool:
    if isinstance(x, dict) and isinstance(x.get("matches"), list):
        return True
    if isinstance(x, list):
        return True
    return False


def _wrap_prediction_result(result: Any, fallback_payload: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(result, dict):
        if "matches" in result:
            return result
        if "predictions" in result and isinstance(result["predictions"], list):
            result["matches"] = result["predictions"]
            return result
        return result
    if isinstance(result, list):
        return {"matches": result}
    return fallback_payload if isinstance(fallback_payload, dict) else {"matches": []}


def calculate_with_motor(target_day: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compatibilité large avec tes différents motor.py :
    - calculate_predictions()
    - calculate_predictions(lines)
    - calculate_predictions(payload)
    - get_real_predictions()
    """
    lines = read_unity_input_lines(target_day)
    attempts: List[Dict[str, Any]] = []

    try:
        import motor  # type: ignore
    except Exception as e:
        out = payload.copy()
        out.setdefault("matches", payload.get("matches", []))
        out["engineError"] = f"motor import error: {repr(e)}"
        return out

    callables = []
    if hasattr(motor, "calculate_predictions"):
        callables.append(("calculate_predictions", getattr(motor, "calculate_predictions")))
    if hasattr(motor, "get_real_predictions"):
        callables.append(("get_real_predictions", getattr(motor, "get_real_predictions")))
    if hasattr(motor, "calculate"):
        callables.append(("calculate", getattr(motor, "calculate")))

    for name, fn in callables:
        # 1) zéro argument : c'était ton ancien mode le plus probable
        try:
            res = fn()
            attempts.append({"fn": name, "mode": "no_args", "ok": _looks_like_prediction_payload(res)})
            if _looks_like_prediction_payload(res):
                out = _wrap_prediction_result(res, payload)
                out.setdefault("targetDay", target_day)
                return out
        except TypeError:
            pass
        except Exception as e:
            attempts.append({"fn": name, "mode": "no_args", "error": repr(e)})

        # 2) lines
        if lines:
            try:
                res = fn(lines)
                attempts.append({"fn": name, "mode": "lines", "ok": _looks_like_prediction_payload(res)})
                if _looks_like_prediction_payload(res):
                    out = _wrap_prediction_result(res, payload)
                    out.setdefault("targetDay", target_day)
                    return out
            except TypeError:
                pass
            except Exception as e:
                attempts.append({"fn": name, "mode": "lines", "error": repr(e)})

        # 3) payload
        try:
            res = fn(payload)
            attempts.append({"fn": name, "mode": "payload", "ok": _looks_like_prediction_payload(res)})
            if _looks_like_prediction_payload(res):
                out = _wrap_prediction_result(res, payload)
                out.setdefault("targetDay", target_day)
                return out
        except TypeError:
            pass
        except Exception as e:
            attempts.append({"fn": name, "mode": "payload", "error": repr(e)})

        # 4) keyword payload
        try:
            res = fn(payload=payload)
            attempts.append({"fn": name, "mode": "kw_payload", "ok": _looks_like_prediction_payload(res)})
            if _looks_like_prediction_payload(res):
                out = _wrap_prediction_result(res, payload)
                out.setdefault("targetDay", target_day)
                return out
        except TypeError:
            pass
        except Exception as e:
            attempts.append({"fn": name, "mode": "kw_payload", "error": repr(e)})

    # fallback : si ton fetch a déjà créé result_YYYY-MM-DD.json
    result_file = load_json(OUTPUT_DIR / f"result_{target_day}.json", None)
    if isinstance(result_file, dict) and isinstance(result_file.get("matches"), list):
        result_file["motorAttempts"] = attempts
        return result_file

    out = payload.copy()
    out.setdefault("matches", payload.get("matches", []))
    out["motorAttempts"] = attempts
    out["engineWarning"] = "Aucun appel motor.py n'a renvoyé un payload clair ; retour du payload brut."
    return out


def enrich_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    matches = payload.get("matches")
    if not isinstance(matches, list):
        matches = []
        payload["matches"] = matches

    valid = [m for m in matches if isinstance(m, dict)]
    over80 = 0
    veto = 0
    proches = 0
    refused_no_veto = 0

    for m in valid:
        try:
            pct = float(str(m.get("premiumPct", 0)).replace(",", "."))
        except Exception:
            try:
                pct = float(m.get("premium", 0)) * 100
            except Exception:
                pct = 0
        veto_yes = str(m.get("veto", "non")).lower() in {"oui", "yes", "true", "1"}
        decision = str(m.get("decision", "")).lower()
        if pct >= 80 and not veto_yes and ("jouable" in decision or "✅" in decision or not decision):
            over80 += 1
        elif 75 <= pct < 80 and not veto_yes:
            proches += 1
        elif veto_yes:
            veto += 1
        elif pct < 75 and not veto_yes:
            refused_no_veto += 1

    payload["summary"] = {
        "totalRows": len(matches),
        "validRows": len(valid),
        "errorRows": max(0, len(matches) - len(valid)),
        "over80": over80,
        "vetoCount": veto,
        "jouables": over80,
        "proches": proches,
        "refusedNoVeto": refused_no_veto,
        "refusesSansVeto": refused_no_veto,
    } | (payload.get("summary") if isinstance(payload.get("summary"), dict) else {})
    return payload


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "endpoints": [
            "/health",
            "/calculate",
            "/daily?day=today",
            "/daily?day=tomorrow",
            "/predictions?day=today",
            "/audit?day=today",
            "/debug-audit?day=today",
            "/history",
            "/history-refresh",
            "/history-reset?confirm=RESET",
            "/update-2026-history?force=true",
        ],
    }


@app.get("/health")
def health():
    st = motor_state()
    history_rows = 0
    try:
        history_rows = len(premium_history.load_history())
    except Exception:
        history_rows = 0

    # essaie d'extraire le nombre de lignes moteur si ton get_state le donne
    loaded = None
    if isinstance(st, dict):
        for k in ("historyRowsLoaded", "rowsLoaded", "history_rows_loaded"):
            if k in st:
                loaded = st[k]
                break

    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "engine": "loaded" if st.get("status") != "error" else "error",
        "historyRowsLoaded": loaded if loaded is not None else st.get("historyRowsLoaded", ""),
        "premiumHistoryRows": history_rows,
        "oddsSource": "Flashscore",
        "motorState": st,
    }


@app.get("/daily")
def daily(day: str = Query("today")):
    target_day = normalize_day(day)

    try:
        # 1. régler anciens pending AVANT l'Elo
        settle_before = premium_history.settle_pending()

        # 2. si un ancien pending vient d'être réglé, on force l'update Elo même si déjà fait aujourd'hui.
        force_update = bool(settle_before.get("settled", 0))
        update_before = run_update_2026_history(force=force_update, reason="before_daily_after_settle")

        # 3. fetch du programme du jour
        fetch_info = run_daily_fetch(target_day)

        # 4. lecture payload + calcul moteur
        raw_payload = read_payload_file(target_day)
        calculated = calculate_with_motor(target_day, raw_payload)
        calculated = enrich_summary(calculated)
        calculated.setdefault("targetDay", target_day)

        # 5. enregistrement premium du jour
        history_record = premium_history.record_daily_analysis(calculated, target_day=target_day)

        # 6. deuxième settle léger : utile si un match du jour est déjà terminé plus tard dans la journée
        settle_after = premium_history.settle_pending()

        # 7. si settle_after a ajouté un résultat, on force l'Elo
        update_after = None
        if settle_after.get("settled", 0):
            update_after = run_update_2026_history(force=True, reason="after_daily_settled_new_result")

        calculated.setdefault("daily", {})
        if not isinstance(calculated["daily"], dict):
            calculated["daily"] = {}
        calculated["daily"].update({
            "targetDay": target_day,
            "fetch": fetch_info,
            "payloadPath": f"payload_{target_day}.json",
        })

        calculated["historyRecord"] = {
            **history_record,
            "autoSettleBefore": settle_before,
            "autoSettleAfter": settle_after,
        }
        calculated["update2026"] = update_after or update_before

        return calculated

    except subprocess.TimeoutExpired as e:
        return _json_response({
            "status": "error",
            "message": "Timeout pendant la récupération daily.",
            "targetDay": target_day,
            "timeoutSeconds": FETCH_TIMEOUT_SECONDS,
            "error": repr(e),
        }, 504)
    except Exception as e:
        return _json_response({
            "status": "error",
            "message": "Erreur /daily",
            "targetDay": target_day,
            "error": repr(e),
            "trace": traceback.format_exc()[-5000:],
        }, 500)


@app.get("/predictions")
def predictions(day: str = Query("today")):
    return daily(day=day)


@app.get("/calculate")
def calculate(day: str = Query("today")):
    return daily(day=day)


@app.get("/audit")
def audit(day: str = Query("today")):
    target_day = normalize_day(day)
    candidates = [
        OUTPUT_DIR / f"audit_{target_day}.txt",
        OUTPUT_DIR / "audit_latest.txt",
        OUTPUT_DIR / f"result_{target_day}.txt",
        OUTPUT_DIR / "result_latest.txt",
    ]
    payload = {"status": "ok", "targetDay": target_day, "files": []}
    for p in candidates:
        if p.exists():
            try:
                payload["files"].append({"path": str(p), "text": p.read_text(encoding="utf-8", errors="replace")[-10000:]})
            except Exception as e:
                payload["files"].append({"path": str(p), "error": repr(e)})
    return payload


@app.get("/debug-audit")
def debug_audit(day: str = Query("today")):
    return audit(day=day)


@app.get("/history")
def history(settle: bool = Query(False)):
    return premium_history.get_history_payload(settle=settle)


@app.get("/history-refresh")
def history_refresh():
    # refresh = règle pending + met à jour Elo si des lignes sont réglées
    info = premium_history.history_refresh()
    settle_info = info.get("settle") if isinstance(info, dict) else {}
    if isinstance(settle_info, dict) and settle_info.get("settled", 0):
        info["update2026"] = run_update_2026_history(force=True, reason="history_refresh_settled")
    return info


@app.get("/history-reset")
def history_reset(confirm: str = Query("")):
    return premium_history.reset_history(confirm=confirm)


@app.get("/update-2026-history")
def update_2026_history_endpoint(force: bool = Query(False)):
    return run_update_2026_history(force=force, reason="manual_endpoint")


@app.get("/raw-history")
def raw_history():
    return premium_history.load_history()


@app.get("/files")
def files():
    wanted = [
        OUTPUT_DIR / "premium_history.json",
        OUTPUT_DIR / "premium_history_summary.json",
        DATA_DIR / "2026.csv",
        DATA_DIR / "2026_raw_jeff_sackmann.csv",
        UPDATE_MARKER,
    ]
    out = []
    for p in wanted:
        out.append({"path": str(p), "exists": p.exists(), "size": p.stat().st_size if p.exists() else 0})
    return {"status": "ok", "files": out}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
