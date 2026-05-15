#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
premium_history.py

Module de sécurité pour enregistrer l'historique quotidien du Tennis Motor.

Correction importante :
- record_daily_analysis() accepte maintenant BOTH formats :
  1) un dict complet venant de app.py
  2) une list directe de matchs venant du calculateur/daily script

Donc cette erreur est corrigée :
AttributeError: 'list' object has no attribute 'get'

Ce fichier ne modifie pas le moteur.
Il sert seulement à enregistrer un historique propre sans faire planter /daily.
"""

from __future__ import annotations

import json
import os
import tempfile
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(BASE_DIR / "output")))

HISTORY_PATH = OUTPUT_DIR / "daily_analysis_history.json"
LATEST_PATH = OUTPUT_DIR / "daily_analysis_latest.json"


def _paris_now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("Europe/Paris"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _paris_today() -> date:
    return _paris_now().date()


def normalize_day(value: Any = None) -> str:
    today = _paris_today()

    if value is None or value == "":
        return today.isoformat()

    if isinstance(value, datetime):
        return value.date().isoformat()

    if isinstance(value, date):
        return value.isoformat()

    raw = str(value).strip()

    if not raw:
        return today.isoformat()

    low = raw.lower()

    if low == "today":
        return today.isoformat()

    if low == "tomorrow":
        from datetime import timedelta
        return (today + timedelta(days=1)).isoformat()

    if len(raw) >= 10:
        candidate = raw[:10]
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
            return candidate
        except Exception:
            pass

    return today.isoformat()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        txt = path.read_text(encoding="utf-8")
        if not txt.strip():
            return default
        return json.loads(txt)
    except Exception:
        return default


def _atomic_write_json(path: Path, payload: Any) -> None:
    _ensure_output_dir()

    data = json.dumps(payload, ensure_ascii=False, indent=2)

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except Exception:
            pass


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        x = float(value)
        if x > 1.0 and x <= 100.0:
            return x / 100.0
        return x

    raw = str(value).strip()
    if not raw:
        return None

    raw = raw.replace("%", "").replace(",", ".")

    try:
        x = float(raw)
    except Exception:
        return None

    if x > 1.0 and x <= 100.0:
        return x / 100.0

    return x


def _extract_probability(row: Dict[str, Any]) -> Optional[float]:
    candidate_keys = [
        "premium",
        "Premium",
        "premiumScore",
        "premium_score",
        "premiumProbability",
        "premium_probability",
        "probability",
        "Probability",
        "proba",
        "confidence",
        "score",
        "finalProbability",
        "final_probability",
        "engineProbability",
        "engine_probability",
    ]

    for key in candidate_keys:
        if key in row:
            p = _to_float(row.get(key))
            if p is not None:
                return p

    engine = row.get("engine")
    if isinstance(engine, dict):
        for key in candidate_keys:
            if key in engine:
                p = _to_float(engine.get(key))
                if p is not None:
                    return p

    prediction = row.get("prediction")
    if isinstance(prediction, dict):
        for key in candidate_keys:
            if key in prediction:
                p = _to_float(prediction.get(key))
                if p is not None:
                    return p

    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if value is None:
        return False

    if isinstance(value, (int, float)):
        return value != 0

    raw = str(value).strip().lower()
    return raw in {"true", "1", "yes", "oui", "y", "vrai", "on"}


def _row_has_veto(row: Dict[str, Any]) -> bool:
    for key in [
        "veto",
        "Veto",
        "vetoActive",
        "veto_active",
        "hasVeto",
        "has_veto",
        "blockedByVeto",
        "blocked_by_veto",
    ]:
        if key in row and _truthy(row.get(key)):
            return True

    text_fields = [
        row.get("decision"),
        row.get("Decision"),
        row.get("status"),
        row.get("finalDecision"),
        row.get("final_decision"),
        row.get("reason"),
        row.get("vetoReason"),
        row.get("veto_reason"),
    ]

    blob = " ".join(str(x) for x in text_fields if x is not None).lower()

    return "veto" in blob or "bloqué" in blob or "blocked" in blob


def _row_is_jouable(row: Dict[str, Any], threshold: float = 0.80) -> bool:
    for key in ["jouable", "isJouable", "is_jouable", "playable", "isPlayable"]:
        if key in row:
            return _truthy(row.get(key))

    text_fields = [
        row.get("decision"),
        row.get("Decision"),
        row.get("status"),
        row.get("finalDecision"),
        row.get("final_decision"),
    ]

    blob = " ".join(str(x) for x in text_fields if x is not None).lower()

    if "pas jouable" in blob or "not playable" in blob:
        return False

    if "jouable" in blob or "playable" in blob:
        return not _row_has_veto(row)

    p = _extract_probability(row)
    return bool(p is not None and p > threshold and not _row_has_veto(row))


def _find_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    for key in [
        "data",
        "matches",
        "predictions",
        "results",
        "rows",
        "items",
        "daily",
        "payload",
    ]:
        value = payload.get(key)

        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

        if isinstance(value, dict):
            nested = _find_rows(value)
            if nested:
                return nested

    return []


def _extract_target_day(payload: Any, target_date: Any = None) -> str:
    if target_date is not None:
        return normalize_day(target_date)

    if isinstance(payload, dict):
        for key in ["targetDay", "target_day", "targetDate", "target_date", "date"]:
            if payload.get(key):
                return normalize_day(payload.get(key))

        daily = payload.get("daily")
        if isinstance(daily, dict):
            for key in ["targetDay", "target_day", "targetDate", "target_date", "date"]:
                if daily.get(key):
                    return normalize_day(daily.get(key))

        summary = payload.get("summary")
        if isinstance(summary, dict):
            for key in ["targetDay", "target_day", "targetDate", "target_date", "date"]:
                if summary.get(key):
                    return normalize_day(summary.get(key))

    return normalize_day(None)


def _extract_target_label(payload: Any, target_day: str) -> str:
    if isinstance(payload, dict):
        for key in ["targetLabel", "target_label", "label"]:
            value = payload.get(key)
            if value:
                return str(value)

    today = normalize_day("today")
    tomorrow = normalize_day("tomorrow")

    if target_day == today:
        return "today"

    if target_day == tomorrow:
        return "tomorrow"

    return target_day


def _extract_daily_script(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        value = payload.get("dailyScript") or payload.get("daily_script") or payload.get("script")
        if value:
            return str(value)

    return None


def _build_summary(
    rows: List[Dict[str, Any]],
    existing_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}

    if isinstance(existing_summary, dict):
        summary.update(existing_summary)

    total = len(rows)
    over80 = 0
    veto_count = 0
    jouables = 0
    proche75_80 = 0

    for row in rows:
        p = _extract_probability(row)
        has_veto = _row_has_veto(row)

        if p is not None and p > 0.80:
            over80 += 1

        if has_veto:
            veto_count += 1

        if _row_is_jouable(row, threshold=0.80):
            jouables += 1

        if p is not None and 0.75 <= p < 0.80 and not has_veto:
            proche75_80 += 1

    summary.setdefault("totalRows", total)
    summary.setdefault("validRows", total)
    summary.setdefault("errorRows", 0)

    summary["over80"] = over80
    summary["vetoCount"] = veto_count
    summary["jouables"] = jouables
    summary["proche75_80"] = proche75_80

    if "engine" not in summary:
        summary["engine"] = {
            "name": "Tennis Motor",
            "threshold": "> 0.80",
            "historyYears": [2022, 2023, 2024, 2025],
        }

    return summary


def _as_record(
    analysis: Any,
    target_date: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    target_day = _extract_target_day(analysis, target_date)
    target_label = _extract_target_label(analysis, target_day)
    daily_script = _extract_daily_script(analysis)
    rows = _find_rows(analysis)

    existing_summary = None
    if isinstance(analysis, dict) and isinstance(analysis.get("summary"), dict):
        existing_summary = analysis.get("summary")

    summary = _build_summary(rows, existing_summary=existing_summary)

    record: Dict[str, Any] = {
        "status": "ok",
        "createdAt": _utc_timestamp(),
        "targetDay": target_day,
        "targetLabel": target_label,
        "dailyScript": daily_script,
        "summary": summary,
        "count": len(rows),
        "data": rows,
    }

    if isinstance(analysis, dict):
        for key in ["status", "source", "audit", "historyRecord"]:
            if key in analysis and key not in record:
                record[key] = analysis[key]

    if extra:
        record["extra"] = extra

    return record


def record_daily_analysis(
    analysis: Any,
    target_date: Any = None,
    *,
    max_history: int = 500,
    **kwargs: Any,
) -> Dict[str, Any]:
    try:
        if target_date is None:
            target_date = (
                kwargs.get("target_day")
                or kwargs.get("targetDay")
                or kwargs.get("day")
                or kwargs.get("date")
            )

        record = _as_record(analysis, target_date=target_date, extra=kwargs or None)

        history = _read_json(HISTORY_PATH, default=[])

        if not isinstance(history, list):
            history = []

        history.append(record)

        if isinstance(max_history, int) and max_history > 0 and len(history) > max_history:
            history = history[-max_history:]

        _atomic_write_json(HISTORY_PATH, history)
        _atomic_write_json(LATEST_PATH, record)

        return {
            "status": "ok",
            "targetDay": record.get("targetDay"),
            "targetLabel": record.get("targetLabel"),
            "count": record.get("count", 0),
            "path": str(HISTORY_PATH),
            "latestPath": str(LATEST_PATH),
            "summary": record.get("summary", {}),
        }

    except Exception as exc:
        return {
            "status": "error",
            "reason": "record_daily_analysis_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
        }


def get_daily_history(limit: int = 50) -> Dict[str, Any]:
    history = _read_json(HISTORY_PATH, default=[])

    if not isinstance(history, list):
        history = []

    if limit and limit > 0:
        items = history[-limit:]
    else:
        items = history

    return {
        "status": "ok",
        "path": str(HISTORY_PATH),
        "count": len(history),
        "items": items,
    }


def get_latest_daily_analysis() -> Dict[str, Any]:
    latest = _read_json(LATEST_PATH, default=None)

    if latest is None:
        return {
            "status": "empty",
            "path": str(LATEST_PATH),
            "message": "Aucun historique enregistré pour le moment.",
        }

    return {
        "status": "ok",
        "path": str(LATEST_PATH),
        "record": latest,
    }


def clear_daily_history() -> Dict[str, Any]:
    _atomic_write_json(HISTORY_PATH, [])

    if LATEST_PATH.exists():
        try:
            LATEST_PATH.unlink()
        except Exception:
            pass

    return {
        "status": "ok",
        "path": str(HISTORY_PATH),
        "message": "Historique vidé.",
    }


def record_daily_history(analysis: Any, target_date: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return record_daily_analysis(analysis, target_date=target_date, **kwargs)


def save_daily_analysis(analysis: Any, target_date: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return record_daily_analysis(analysis, target_date=target_date, **kwargs)


def save_daily_history(analysis: Any, target_date: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return record_daily_analysis(analysis, target_date=target_date, **kwargs)


def record_premium_history(analysis: Any, target_date: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return record_daily_analysis(analysis, target_date=target_date, **kwargs)


def get_history(limit: int = 50) -> Dict[str, Any]:
    return get_daily_history(limit=limit)


def load_history(limit: int = 50) -> Dict[str, Any]:
    return get_daily_history(limit=limit)


def read_history(limit: int = 50) -> Dict[str, Any]:
    return get_daily_history(limit=limit)


def get_premium_history(limit: int = 50) -> Dict[str, Any]:
    return get_daily_history(limit=limit)


if __name__ == "__main__":
    sample_list = [
        {
            "playerA": "Casper Ruud",
            "playerB": "Luciano Darderi",
            "surface": "Clay",
            "premium": 0.78,
            "veto": False,
            "decision": "❌ Pas jouable",
        },
        {
            "playerA": "Jannik Sinner",
            "playerB": "Daniil Medvedev",
            "surface": "Clay",
            "premium": 0.83,
            "veto": False,
            "decision": "✅ Jouable",
        },
    ]

    print(json.dumps(record_daily_analysis(sample_list, target_date="today"), ensure_ascii=False, indent=2))
