#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor Backend - V6.9H OFFICIAL PAIR GUARD - Railway Ready

Base stable :
- garde le moteur officiel actuel ;
- garde fetch_day_lines_v6_9_strict_day_filter.py pour les matchs analysables ;
- ajoute seulement les matchs présents sur ATP daily-schedule mais absents du payload V6.9 ;
- ces matchs sont affichés comme "Non analysé" si une donnée obligatoire manque ;
- aucun pronostic n'est inventé.

Fichiers nécessaires dans le même dossier backend :
- motor.py
- shared_core_v2pro.py
- fetch_day_lines_v6_9_strict_day_filter.py
- fetch_day_lines_v6_9c_daily_schedule_audit.py
- fetch_day_lines_v6_7_results_context_fixed_safe_clamped.py
- data/2022.csv, data/2023.csv, data/2024.csv, data/2025.csv
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

import motor


BACKEND_DIR = Path(__file__).resolve().parent
OUT_DIR = BACKEND_DIR / "output"

DAILY_SCRIPT = "fetch_day_lines_v6_9_strict_day_filter.py"
MISSING_AUDIT_SCRIPT = "fetch_day_lines_v6_9c_daily_schedule_audit.py"

APP_VERSION = "v6_9i_promote_missing_points_railway"


class MatchInput(BaseModel):
    playerA: str
    playerB: str
    surface: str
    playerAPoints: int
    playerBPoints: int
    player_b_is_qualifier: bool = False
    player_b_tournament_wins: int = 0


class CalculateRequest(BaseModel):
    matches: List[MatchInput] = Field(default_factory=list)


app = FastAPI(title="Tennis Motor Backend", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def parse_target_day(raw: str) -> date:
    raw = (raw or "today").strip().lower()
    today = date.today()

    if raw == "today":
        return today

    if raw == "tomorrow":
        return today + timedelta(days=1)

    return datetime.strptime(raw, "%Y-%m-%d").date()


def get_history_rows_loaded() -> int:
    try:
        if hasattr(motor, "get_state"):
            state = motor.get_state()
            if isinstance(state, dict):
                return int(state.get("history_rows_loaded", 0))
    except Exception:
        pass

    return 0


def normalize_text(value: Any) -> str:
    text = str(value or "").lower()
    cleaned = []
    previous_space = False

    for ch in text:
        if ch.isalnum():
            cleaned.append(ch)
            previous_space = False
        else:
            if not previous_space:
                cleaned.append(" ")
                previous_space = True

    return " ".join("".join(cleaned).split())


def pair_key(player_a: Any, player_b: Any) -> Tuple[str, str]:
    a = normalize_text(player_a)
    b = normalize_text(player_b)
    return tuple(sorted([a, b]))  # type: ignore[return-value]


def is_real_missing_singles(row: Dict[str, Any]) -> bool:
    """
    L'audit daily-schedule peut capter un faux positif de double :
    Theo Arribage vs Albano Olivetti.
    On garde seulement les manquants qui ont une vraie preuve de match simple :
    H2H, Defeats, R32/R64/R16, Not Before, Followed By.
    """
    player_a = str(row.get("playerA", "") or "").strip()
    player_b = str(row.get("playerB", "") or "").strip()

    if not player_a or not player_b:
        return False

    if pair_key(player_a, player_b)[0] == pair_key(player_a, player_b)[1]:
        return False

    evidence = str(row.get("evidence", "") or "").lower()

    strong_signals = [
        "h2h",
        "defeats",
        "not before",
        "followed by",
        "r64",
        "r32",
        "r16",
        "quarter",
        "semi",
        "final",
    ]

    return any(signal in evidence for signal in strong_signals)


def make_non_analyzed_match(row: Dict[str, Any]) -> Dict[str, Any]:
    player_a = str(row.get("playerA", "") or "").strip()
    player_b = str(row.get("playerB", "") or "").strip()
    evidence = str(row.get("evidence", "") or "").strip()
    source_url = str(row.get("source_url", "") or row.get("sourceUrl", "") or "").strip()

    return {
        "playerA": player_a,
        "playerB": player_b,
        "surface": "Clay",
        "playerAPoints": 0,
        "playerBPoints": 0,
        "player_b_is_qualifier": False,
        "player_b_tournament_wins": 0,
        "sweA": 0,
        "sweB": 0,
        "pSwe": 0,
        "pAtp": 0,
        "pRank": 0,
        "pForm5": 0,
        "pForm10": 0,
        "pSurfaceForm5": 0,
        "pDominance": 0,
        "premium": 0,
        "premiumPct": 0,
        "veto": "",
        "decision": "Non analysé",
        "error": "Non analysé : données ATP incomplètes ou joueur introuvable dans la table des points ATP.",
        "nonAnalyzed": True,
        "source": "ATP daily-schedule",
        "sourceUrl": source_url,
        "evidence": evidence,
    }


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def make_payload_match_from_missing(row: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Transforme un match official daily-schedule manquant en entrée moteur
    uniquement si les deux points ATP sont présents dans l'audit.

    Sécurité moteur : si le contexte Q/wins est inconnu sur Clay, on force
    player_b_tournament_wins=2 pour empêcher une validation verte artificielle.
    Le match devient analysable mais reste protégé par le veto terre battue.
    """
    player_a = str(row.get("playerA", "") or "").strip()
    player_b = str(row.get("playerB", "") or "").strip()
    points_a = safe_int(row.get("playerAPoints", 0))
    points_b = safe_int(row.get("playerBPoints", 0))

    if not player_a or not player_b:
        return None
    if points_a <= 0 or points_b <= 0:
        return None

    return {
        "playerA": player_a,
        "playerB": player_b,
        "surface": "Clay",
        "playerAPoints": points_a,
        "playerBPoints": points_b,
        "player_b_is_qualifier": False,
        "player_b_tournament_wins": 2,
        "source": "ATP daily-schedule promoted missing with safe clay veto",
    }


def promote_missing_rows_into_payload(
    payload: List[Dict[str, Any]],
    missing_rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Avant le calcul moteur :
    - ajoute les matchs manquants qui ont des points ATP réels ;
    - laisse les autres en Non analysé après calcul.
    """
    existing_keys = set()
    for row in payload:
        if not isinstance(row, dict):
            continue
        existing_keys.add(pair_key(row.get("playerA", ""), row.get("playerB", "")))

    promoted: List[Dict[str, Any]] = []
    still_missing: List[Dict[str, Any]] = []

    for row in missing_rows:
        if not isinstance(row, dict):
            continue

        candidate = make_payload_match_from_missing(row)
        if candidate is None:
            still_missing.append(row)
            continue

        key = pair_key(candidate.get("playerA", ""), candidate.get("playerB", ""))
        if key in existing_keys:
            continue

        payload.append(candidate)
        promoted.append(candidate)
        existing_keys.add(key)

    return payload, promoted, still_missing


def render_backend_result(result: Dict[str, Any]) -> str:
    lines: List[str] = []

    summary = result.get("summary")
    matches = result.get("matches")
    engine = result.get("engine")

    if isinstance(summary, dict):
        lines.append("Résumé")
        lines.append(f"- Lignes totales : {summary.get('totalRows', 0)}")
        lines.append(f"- Lignes valides : {summary.get('validRows', 0)}")
        lines.append(f"- Lignes en erreur : {summary.get('errorRows', 0)}")
        lines.append(f"- Non analysés : {summary.get('nonAnalyzedRows', 0)}")
        lines.append(f"- Premium > 80% : {summary.get('over80', 0)}")
        lines.append(f"- Veto : {summary.get('vetoCount', 0)}")
        lines.append(f"- Jouables : {summary.get('jouables', 0)}")
        lines.append("")

    if isinstance(matches, list):
        lines.append("Résultats")
        lines.append("")

        for row in matches:
            if not isinstance(row, dict):
                continue

            player_a = row.get("playerA", "")
            player_b = row.get("playerB", "")
            surface = row.get("surface", "")

            if row.get("nonAnalyzed") or row.get("error"):
                lines.append(f"{player_a} vs {player_b} ({surface})")
                lines.append("Décision : Non analysé")
                lines.append(f"Raison : {row.get('error', 'Données ATP incomplètes.')}")
                if row.get("evidence"):
                    lines.append(f"Preuve ATP : {row.get('evidence')}")
                lines.append("")
                continue

            lines.append(f"{player_a} vs {player_b} ({surface})")
            lines.append(f"Points ATP : {row.get('playerAPoints', '')} vs {row.get('playerBPoints', '')}")
            lines.append(
                f"Qualifier B : {row.get('player_b_is_qualifier', '')} | "
                f"Wins tournoi B : {row.get('player_b_tournament_wins', '')}"
            )
            lines.append(f"SWE : {row.get('sweA', '')} vs {row.get('sweB', '')}")
            lines.append(f"pSwe : {row.get('pSwe', '')} | pAtp : {row.get('pAtp', '')}")
            lines.append(f"pRank : {row.get('pRank', '')} | pForm5 : {row.get('pForm5', '')} | pForm10 : {row.get('pForm10', '')}")
            lines.append(f"pSurfaceForm5 : {row.get('pSurfaceForm5', '')} | pDominance : {row.get('pDominance', '')}")
            lines.append(f"Premium : {row.get('premiumPct', '')}%")
            lines.append(f"Veto : {row.get('veto', '')}")

            decision = str(row.get("decision", "")).replace("✅ ", "").replace("❌ ", "")
            lines.append(f"Décision : {decision}")
            lines.append("")

    if isinstance(engine, dict):
        lines.append("Moteur")
        lines.append(f"- Nom : {engine.get('name', '')}")
        lines.append(f"- Version : {engine.get('version', '')}")
        lines.append(f"- Lignes historiques chargées : {engine.get('historyRowsLoaded', 0)}")
        lines.append(f"- Formule Premium : {engine.get('premiumFormula', '')}")
        lines.append(f"- Seuil : {engine.get('threshold', '')}")

    return "\n".join(lines)


@app.get("/")
def root() -> Dict[str, Any]:
    return health()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "message": "backend prêt",
        "version": APP_VERSION,
        "historyRowsLoaded": get_history_rows_loaded(),
        "dailyScript": DAILY_SCRIPT,
        "dailyScriptFound": (BACKEND_DIR / DAILY_SCRIPT).exists(),
        "missingAuditScript": MISSING_AUDIT_SCRIPT,
        "missingAuditScriptFound": (BACKEND_DIR / MISSING_AUDIT_SCRIPT).exists(),
        "backendDir": str(BACKEND_DIR),
        "dataDirFound": (BACKEND_DIR / "data").exists(),
    }


@app.post("/calculate")
def calculate(request: CalculateRequest) -> Dict[str, Any]:
    matches = [match.model_dump() for match in request.matches]

    if not hasattr(motor, "calculate_predictions"):
        return {"error": "Le fichier motor.py ne contient pas calculate_predictions(matches)."}

    result = motor.calculate_predictions(matches)

    if not isinstance(result, dict):
        return {"error": "Le moteur n'a pas renvoyé un objet dict valide."}

    return result


def run_daily_fetch(day: str, target_day: date) -> Dict[str, Any]:
    script_path = BACKEND_DIR / DAILY_SCRIPT

    if not script_path.exists():
        return {"error": f"Script daily introuvable : {script_path}"}

    OUT_DIR.mkdir(exist_ok=True)

    cmd = [
        sys.executable,
        str(script_path),
        day,
        "--backend-url",
        "http://127.0.0.1:9",
    ]

    completed = subprocess.run(
        cmd,
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=260,
    )

    if completed.returncode != 0:
        return {
            "error": "Le script daily a échoué.",
            "returncode": completed.returncode,
            "stdout": completed.stdout[-5000:],
            "stderr": completed.stderr[-5000:],
        }

    payload_path = OUT_DIR / f"payload_{target_day.isoformat()}.json"
    audit_path = OUT_DIR / f"audit_{target_day.isoformat()}.txt"

    if not payload_path.exists():
        return {
            "error": f"Payload introuvable après fetch : {payload_path}",
            "stdout": completed.stdout[-5000:],
            "stderr": completed.stderr[-5000:],
        }

    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"Payload JSON illisible : {exc}"}

    if not isinstance(payload, list):
        return {"error": "Payload JSON invalide : liste attendue."}

    try:
        (OUT_DIR / "payload_latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    return {
        "payload": payload,
        "payloadPath": str(payload_path),
        "auditPath": str(audit_path),
        "stdout": completed.stdout[-3000:],
    }


def run_missing_audit(day: str, target_day: date) -> Dict[str, Any]:
    script_path = BACKEND_DIR / MISSING_AUDIT_SCRIPT

    if not script_path.exists():
        return {
            "enabled": False,
            "error": f"Script audit manquant : {script_path}",
            "missing": [],
            "ignored": [],
        }

    cmd = [
        sys.executable,
        str(script_path),
        day,
    ]

    try:
        completed = subprocess.run(
            cmd,
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {
            "enabled": True,
            "error": "Audit matchs invisibles timeout.",
            "missing": [],
            "ignored": [],
        }

    if completed.returncode != 0:
        return {
            "enabled": True,
            "error": "Audit matchs invisibles échoué.",
            "stdout": completed.stdout[-3000:],
            "stderr": completed.stderr[-3000:],
            "missing": [],
            "ignored": [],
        }

    json_path = OUT_DIR / f"audit_daily_schedule_missing_{target_day.isoformat()}.json"
    latest_json_path = OUT_DIR / "audit_daily_schedule_missing_latest.json"

    if json_path.exists():
        read_path = json_path
    elif latest_json_path.exists():
        read_path = latest_json_path
    else:
        return {
            "enabled": True,
            "error": "Audit JSON introuvable.",
            "missing": [],
            "ignored": [],
        }

    try:
        data = json.loads(read_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "enabled": True,
            "error": f"Audit JSON illisible : {exc}",
            "missing": [],
            "ignored": [],
        }

    raw_missing = data.get("missingFromPayload", [])
    if not isinstance(raw_missing, list):
        raw_missing = []

    kept: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []

    for row in raw_missing:
        if not isinstance(row, dict):
            continue

        if is_real_missing_singles(row):
            kept.append(row)
        else:
            ignored.append(row)

    raw_official_pairs = data.get("dailySchedulePairs", [])
    official_pairs: List[Dict[str, Any]] = []

    if isinstance(raw_official_pairs, list):
        for row in raw_official_pairs:
            if not isinstance(row, dict):
                continue

            # Sécurité V6.9H :
            # on garde seulement les vrais blocs ATP.
            # Les lignes reconstruites "vs" peuvent parfois mélanger deux matchs.
            if str(row.get("source", "")).strip() != "daily_schedule_block":
                continue

            pa = str(row.get("playerA", "") or "").strip()
            pb = str(row.get("playerB", "") or "").strip()

            if not pa or not pb:
                continue

            if pair_key(pa, pb)[0] == pair_key(pa, pb)[1]:
                continue

            official_pairs.append(row)

    return {
        "enabled": True,
        "error": "",
        "auditPath": str(read_path),
        "missing": kept,
        "ignored": ignored,
        "rawMissingCount": len(raw_missing),
        "keptMissingCount": len(kept),
        "ignoredMissingCount": len(ignored),
        "officialDailyPairs": official_pairs,
        "officialDailyPairsCount": len(official_pairs),
    }


def filter_payload_against_official_pairs(
    payload: List[Dict[str, Any]],
    missing_audit: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Sécurité V6.9H :
    - le scraper peut parfois créer une fausse paire en mélangeant des joueurs
      de deux blocs voisins ;
    - l'audit ATP daily-schedule donne les vrais blocs officiels ;
    - on supprime du payload les paires absentes des vrais blocs ATP.
    """
    official_rows = missing_audit.get("officialDailyPairs", [])

    if not isinstance(official_rows, list) or len(official_rows) == 0:
        return payload, []

    official_keys = set()

    for row in official_rows:
        if not isinstance(row, dict):
            continue

        official_keys.add(pair_key(row.get("playerA", ""), row.get("playerB", "")))

    if not official_keys:
        return payload, []

    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []

    for row in payload:
        if not isinstance(row, dict):
            continue

        key = pair_key(row.get("playerA", ""), row.get("playerB", ""))

        if key in official_keys:
            kept.append(row)
        else:
            dropped.append(row)

    return kept, dropped


def append_non_analyzed_matches(result: Dict[str, Any], missing_rows: List[Dict[str, Any]]) -> int:
    if not missing_rows:
        return 0

    matches = result.get("matches")
    if not isinstance(matches, list):
        matches = []
        result["matches"] = matches

    existing_keys = set()

    for row in matches:
        if not isinstance(row, dict):
            continue
        existing_keys.add(pair_key(row.get("playerA", ""), row.get("playerB", "")))

    appended = 0

    for row in missing_rows:
        new_match = make_non_analyzed_match(row)
        key = pair_key(new_match.get("playerA", ""), new_match.get("playerB", ""))

        if key in existing_keys:
            continue

        matches.append(new_match)
        existing_keys.add(key)
        appended += 1

    summary = result.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        result["summary"] = summary

    old_total = int(summary.get("totalRows", len(matches) - appended) or 0)
    old_error = int(summary.get("errorRows", 0) or 0)
    old_non_analyzed = int(summary.get("nonAnalyzedRows", 0) or 0)

    summary["totalRows"] = old_total + appended
    summary["errorRows"] = old_error + appended
    summary["nonAnalyzedRows"] = old_non_analyzed + appended

    if "validRows" not in summary:
        summary["validRows"] = max(0, len(matches) - appended)

    return appended


@app.get("/daily")
def daily(day: str = Query("today", description="today | tomorrow | YYYY-MM-DD")) -> Dict[str, Any]:
    try:
        target_day = parse_target_day(day)
    except Exception:
        return {"error": "Paramètre day invalide. Utilise today, tomorrow ou YYYY-MM-DD."}

    fetched = run_daily_fetch(day, target_day)

    if "error" in fetched:
        return fetched

    payload = fetched.get("payload", [])

    if not isinstance(payload, list):
        payload = []

    # IMPORTANT V6.9H :
    # on lance l'audit AVANT le moteur pour obtenir les vrais blocs ATP officiels.
    missing_audit = run_missing_audit(day, target_day)

    payload_before_guard_count = len(payload)
    payload, dropped_wrong_pairs = filter_payload_against_official_pairs(payload, missing_audit)

    if dropped_wrong_pairs:
        try:
            filtered_payload_path = OUT_DIR / f"payload_{target_day.isoformat()}_official_guard.json"
            filtered_payload_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (OUT_DIR / "payload_latest_official_guard.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    missing_rows = missing_audit.get("missing", [])
    promoted_missing_rows: List[Dict[str, Any]] = []
    remaining_missing_rows: List[Dict[str, Any]] = []

    if isinstance(missing_rows, list):
        payload, promoted_missing_rows, remaining_missing_rows = promote_missing_rows_into_payload(
            payload,
            missing_rows,
        )
    else:
        remaining_missing_rows = []

    payload_after_promote_count = len(payload)

    if not payload:
        result: Dict[str, Any] = {
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
                "historyRowsLoaded": get_history_rows_loaded(),
                "premiumFormula": "Bayesian shrinkage blend of SWE, ATP, Rank, Form5, Form10, SurfaceForm5, Dominance",
                "threshold": "> 0.80",
            },
        }
    else:
        if not hasattr(motor, "calculate_predictions"):
            return {"error": "Le fichier motor.py ne contient pas calculate_predictions(matches)."}

        result = motor.calculate_predictions(payload)

        if not isinstance(result, dict):
            return {"error": "Le moteur n'a pas renvoyé un objet dict valide."}

    result["meta"] = {
        "targetDay": target_day.isoformat(),
        "targetLabel": day,
        "mode": APP_VERSION,
        "payloadPath": fetched.get("payloadPath", ""),
        "auditPath": fetched.get("auditPath", ""),
        "officialPairGuard": {
            "enabled": True,
            "payloadBeforeGuard": payload_before_guard_count,
            "payloadAfterGuard": payload_after_promote_count,
            "payloadAfterPromote": payload_after_promote_count,
            "promotedMissingRows": len(promoted_missing_rows),
            "droppedWrongPairs": len(dropped_wrong_pairs),
            "officialDailyPairsCount": missing_audit.get("officialDailyPairsCount", 0),
            "droppedPairs": [
                {
                    "playerA": row.get("playerA", ""),
                    "playerB": row.get("playerB", ""),
                    "reason": "Paire absente des vrais blocs ATP daily-schedule",
                }
                for row in dropped_wrong_pairs
                if isinstance(row, dict)
            ],
        },
    }

    appended = 0

    if remaining_missing_rows:
        appended = append_non_analyzed_matches(result, remaining_missing_rows)

    if isinstance(result.get("meta"), dict):
        result["meta"]["missingAudit"] = {
            "enabled": missing_audit.get("enabled", False),
            "error": missing_audit.get("error", ""),
            "auditPath": missing_audit.get("auditPath", ""),
            "rawMissingCount": missing_audit.get("rawMissingCount", 0),
            "keptMissingCount": missing_audit.get("keptMissingCount", 0),
            "ignoredMissingCount": missing_audit.get("ignoredMissingCount", 0),
            "appendedNonAnalyzed": appended,
            "promotedMissingAnalyzed": len(promoted_missing_rows),
            "officialDailyPairsCount": missing_audit.get("officialDailyPairsCount", 0),
        }

    OUT_DIR.mkdir(exist_ok=True)

    result_json_path = OUT_DIR / f"result_{target_day.isoformat()}.json"
    result_txt_path = OUT_DIR / f"result_{target_day.isoformat()}.txt"
    result_latest_path = OUT_DIR / "result_latest.txt"

    result_json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    result_text = render_backend_result(result)
    result_txt_path.write_text(result_text, encoding="utf-8")
    result_latest_path.write_text(result_text, encoding="utf-8")

    return result


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
