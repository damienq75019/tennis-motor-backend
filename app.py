from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from motor import HISTORY_YEARS, calculate_match_prediction, get_state
from api_tennis_daily_builder import ApiTennisDailyBuilder
from api_tennis_results2026_sync import ApiTennisResults2026Syncer
from postgres_premium_store import PostgresPremiumStore, score_match_with_form_value
from api_tennis_premium_sync import PremiumHistorySyncer, tracked_category
from flashscore_odds import FlashscoreOddsProvider
from step59_step56_audit import SERVICE_VERSION as STEP59_AUDIT_VERSION, get_step56_auditor
from v3_learning_engine import (
    V3_VERSION,
    V3LearningMemoryStore,
    build_v3_learning_report,
    build_v3_rules_from_history,
    evaluate_shadow_matches,
)


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
AUDIT_DIR = OUTPUT_DIR / "audits"
PAYLOAD_DIR = OUTPUT_DIR / "payloads"

# Règle utilisateur verrouillée : Jannik Sinner reste exclu de l'analyse.
EXCLUDED_ANALYSIS_PLAYERS = ["Jannik Sinner"]

app = FastAPI(title="Tennis Motor Backend Clean", version="step62-refuse-value-persistent-history")
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
    if value in {"yesterday", "hier"}:
        return (today - timedelta(days=1)).isoformat()

    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Paramètre day invalide. Utilise today, tomorrow, yesterday, hier ou une date YYYY-MM-DD.",
        ) from exc


def _norm_name(value: str) -> str:
    value = value or ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(?:wc|q|ll|pr|alt|seed)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _name_tokens(value: str) -> List[str]:
    return [x for x in _norm_name(value).split() if len(x) >= 2]


def _same_player(a: str, b: str) -> bool:
    na = _norm_name(a)
    nb = _norm_name(b)

    if not na or not nb:
        return False
    if na == nb:
        return True

    ta = _name_tokens(a)
    tb = _name_tokens(b)
    if not ta or not tb:
        return False
    if set(ta) == set(tb):
        return True

    last_a = ta[-1]
    last_b = tb[-1]

    if len(ta) == 1 and len(ta[0]) >= 4 and ta[0] == last_b:
        return True
    if len(tb) == 1 and len(tb[0]) >= 4 and tb[0] == last_a:
        return True

    if len(ta) >= 2 and len(tb) >= 2:
        if " ".join(ta[-2:]) == " ".join(tb[-2:]):
            return True

    if last_a == last_b:
        first_a = ta[0][0]
        first_b = tb[0][0]
        return first_a == first_b or ta[0] in tb or tb[0] in ta

    return False


def _excluded_analysis_names() -> List[str]:
    raw = os.environ.get("EXCLUDED_ANALYSIS_PLAYERS", "").strip()
    names = [x.strip() for x in raw.split(",") if x.strip()] if raw else list(EXCLUDED_ANALYSIS_PLAYERS)

    out: List[str] = []
    for name in names:
        if name and name not in out:
            out.append(name)
    return out


def _match_has_excluded_player(match: Dict[str, Any], excluded_names: List[str]) -> bool:
    if not isinstance(match, dict):
        return False

    fields = [
        match.get("playerA"),
        match.get("playerB"),
        match.get("player_a"),
        match.get("player_b"),
        match.get("sourcePlayerA"),
        match.get("sourcePlayerB"),
        match.get("sourceOriginalPair"),
    ]
    normalized_fields = [_norm_name(str(value or "")) for value in fields]

    for excluded in excluded_names:
        excluded_norm = _norm_name(excluded)
        if not excluded_norm:
            continue
        for field_norm in normalized_fields:
            if not field_norm:
                continue
            if field_norm == excluded_norm:
                return True
            if re.search(rf"\b{re.escape(excluded_norm)}\b", field_norm):
                return True
            if _same_player(excluded, field_norm):
                return True
    return False


def _filter_excluded_analysis_matches(matches: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    excluded_names = _excluded_analysis_names()
    if not excluded_names:
        return matches, []

    kept: List[Dict[str, Any]] = []
    removed: List[Dict[str, str]] = []

    for match in matches:
        if _match_has_excluded_player(match, excluded_names):
            player_a = str(match.get("playerA") or match.get("player_a") or match.get("sourcePlayerA") or "")
            player_b = str(match.get("playerB") or match.get("player_b") or match.get("sourcePlayerB") or "")
            removed.append({"playerA": player_a, "playerB": player_b})
        else:
            kept.append(match)

    return kept, removed


def _get_first_existing(match: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for key in keys:
        if key in match and match.get(key) is not None:
            return match.get(key)
    return default


def _to_bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value or "").strip().lower()
    return s in {"1", "true", "yes", "oui", "y", "o"}


def _to_int_value(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def _reverse_match_for_engine(match: Dict[str, Any]) -> Dict[str, Any]:
    rev = dict(match)

    player_a = str(_get_first_existing(match, ["playerA", "player_a"], "") or "")
    player_b = str(_get_first_existing(match, ["playerB", "player_b"], "") or "")
    points_a = _get_first_existing(match, ["playerAPoints", "player_a_points"], 0)
    points_b = _get_first_existing(match, ["playerBPoints", "player_b_points"], 0)

    rev["playerA"] = player_b
    rev["playerB"] = player_a
    rev["player_a"] = player_b
    rev["player_b"] = player_a
    rev["playerAPoints"] = points_b
    rev["playerBPoints"] = points_a
    rev["player_a_points"] = points_b
    rev["player_b_points"] = points_a

    old_a_is_qualifier = _get_first_existing(
        match,
        ["player_a_is_qualifier", "playerAIsQualifier", "player_a_qualifier", "playerAQualifier"],
        False,
    )
    old_a_tournament_wins = _get_first_existing(
        match,
        ["player_a_tournament_wins", "playerATournamentWins", "player_a_wins", "playerAWins"],
        0,
    )
    old_b_is_qualifier = _get_first_existing(
        match,
        ["player_b_is_qualifier", "playerBIsQualifier", "player_b_qualifier", "playerBQualifier"],
        False,
    )
    old_b_tournament_wins = _get_first_existing(
        match,
        ["player_b_tournament_wins", "playerBTournamentWins", "player_b_wins", "playerBWins"],
        0,
    )

    rev["player_a_is_qualifier"] = old_b_is_qualifier
    rev["playerAIsQualifier"] = old_b_is_qualifier
    rev["player_a_tournament_wins"] = old_b_tournament_wins
    rev["playerATournamentWins"] = old_b_tournament_wins
    rev["player_b_is_qualifier"] = old_a_is_qualifier
    rev["playerBIsQualifier"] = old_a_is_qualifier
    rev["player_b_tournament_wins"] = old_a_tournament_wins
    rev["playerBTournamentWins"] = old_a_tournament_wins

    return rev


def _premium_score(match: Dict[str, Any]) -> float:
    value = match.get("premiumPct", match.get("premium", 0.0))
    try:
        score = float(value)
    except Exception:
        return 0.0
    if 0.0 <= score <= 1.0:
        score *= 100.0
    return score


def _is_veto(match: Dict[str, Any]) -> bool:
    return str(match.get("veto", "")).strip().lower() in {"oui", "yes", "true", "1"}


def _rebuild_summary_from_matches(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(matches)
    error_rows = 0
    over80 = 0
    proches = 0
    veto_count = 0
    jouables = 0
    refuses_sans_veto = 0
    non_analyzed = 0

    for match in matches:
        if match.get("nonAnalyzable") or match.get("analysisStatus") == "not_analyzed":
            non_analyzed += 1
            continue

        if match.get("error"):
            error_rows += 1

        premium_pct = _premium_score(match)
        veto = _is_veto(match)

        if veto:
            veto_count += 1
        if premium_pct > 80.0 and not veto:
            over80 += 1
            jouables += 1
        elif 75.0 <= premium_pct < 80.0 and not veto:
            proches += 1
        elif not veto:
            refuses_sans_veto += 1

    return {
        "totalRows": total,
        "validRows": total - error_rows - non_analyzed,
        "errorRows": error_rows,
        "nonAnalyzed": non_analyzed,
        "nonAnalyzable": non_analyzed,
        "over80": over80,
        "vetoCount": veto_count,
        "jouables": jouables,
        "proches": proches,
        "refusedNoVeto": refuses_sans_veto,
        "refusesSansVeto": refuses_sans_veto,
    }


def _copy_daily_context_to_prediction(source_match: Dict[str, Any], prediction: Dict[str, Any], orientation: str) -> Dict[str, Any]:
    if not isinstance(source_match, dict) or not isinstance(prediction, dict):
        return prediction

    source_a_qualifier = _to_bool_value(_get_first_existing(
        source_match,
        ["player_a_is_qualifier", "playerAIsQualifier", "player_a_qualifier", "playerAQualifier"],
        False,
    ))
    source_b_qualifier = _to_bool_value(_get_first_existing(
        source_match,
        ["player_b_is_qualifier", "playerBIsQualifier", "player_b_qualifier", "playerBQualifier"],
        False,
    ))
    source_a_wins = _to_int_value(_get_first_existing(
        source_match,
        ["player_a_tournament_wins", "playerATournamentWins", "player_a_wins", "playerAWins"],
        0,
    ))
    source_b_wins = _to_int_value(_get_first_existing(
        source_match,
        ["player_b_tournament_wins", "playerBTournamentWins", "player_b_wins", "playerBWins"],
        0,
    ))

    if orientation == "reversed":
        display_a_qualifier = source_b_qualifier
        display_b_qualifier = source_a_qualifier
        display_a_wins = source_b_wins
        display_b_wins = source_a_wins
    else:
        display_a_qualifier = source_a_qualifier
        display_b_qualifier = source_b_qualifier
        display_a_wins = source_a_wins
        display_b_wins = source_b_wins

    prediction["player_a_is_qualifier"] = display_a_qualifier
    prediction["player_b_is_qualifier"] = display_b_qualifier
    prediction["player_a_tournament_wins"] = display_a_wins
    prediction["player_b_tournament_wins"] = display_b_wins
    prediction["playerAIsQualifier"] = display_a_qualifier
    prediction["playerBIsQualifier"] = display_b_qualifier
    prediction["playerATournamentWins"] = display_a_wins
    prediction["playerBTournamentWins"] = display_b_wins
    prediction["playerAWins"] = display_a_wins
    prediction["playerBWins"] = display_b_wins
    prediction["contextSource"] = "api_tennis_clean_manual_or_future_provider"
    prediction["contextOrientation"] = orientation
    prediction["contextPlayerATournamentWins"] = display_a_wins
    prediction["contextPlayerBTournamentWins"] = display_b_wins

    # Marqueurs de prudence pour la future intégration provider.
    if "player_b_qualifier_confidence" in source_match:
        prediction["player_b_qualifier_confidence"] = source_match.get("player_b_qualifier_confidence")
    if "player_b_qualifier_source" in source_match:
        prediction["player_b_qualifier_source"] = source_match.get("player_b_qualifier_source")

    # Step 2.9 : détection qualifié en audit only.
    # Ces champs suivent l'orientation affichée, mais ne modifient pas le veto moteur.
    def _copy_qualifier_detection(display_prefix: str, source_prefix: str) -> None:
        for suffix in [
            "is_qualifier_detected",
            "qualifier_detection_confidence",
            "qualifier_detection_source",
            "qualifier_detection_reason",
        ]:
            src_key = f"{source_prefix}_{suffix}"
            dst_key = f"{display_prefix}_{suffix}"
            if src_key in source_match:
                prediction[dst_key] = source_match.get(src_key)

    if orientation == "reversed":
        _copy_qualifier_detection("player_a", "player_b")
        _copy_qualifier_detection("player_b", "player_a")
    else:
        _copy_qualifier_detection("player_a", "player_a")
        _copy_qualifier_detection("player_b", "player_b")

    if "qualifierDetectorPolicy" in source_match:
        prediction["qualifierDetectorPolicy"] = source_match.get("qualifierDetectorPolicy")

    # Métadonnées legacy conservées pour compatibilité Unity/PostgreSQL.
    for key in [
        "sportradarSportEventId",
        "sportradarSeasonId",
        "sportradarCompetitionId",
        "tournament",
        "seasonName",
        "round",
        "startTime",
        "status",
        "matchStatus",
        "winnerId",
        "score",
        "source",
        "tournamentWinsPolicy",
    ]:
        if key in source_match:
            prediction[key] = source_match.get(key)

    source_id_a = str(source_match.get("sportradarPlayerAId") or "")
    source_id_b = str(source_match.get("sportradarPlayerBId") or "")
    prediction["sportradarSourcePlayerAId"] = source_id_a
    prediction["sportradarSourcePlayerBId"] = source_id_b
    if orientation == "reversed":
        prediction["sportradarPlayerAId"] = source_id_b
        prediction["sportradarPlayerBId"] = source_id_a
    else:
        prediction["sportradarPlayerAId"] = source_id_a
        prediction["sportradarPlayerBId"] = source_id_b

    # STEP61 : preserve odds display with the same orientation as the displayed pick.
    # Odds are never used by STEP56 prediction; they are used only by Refuse Value.
    def _copy_odd_pair(a_key: str, b_key: str) -> None:
        a_val = source_match.get(a_key)
        b_val = source_match.get(b_key)
        if orientation == "reversed":
            if b_val is not None:
                prediction[a_key] = b_val
            if a_val is not None:
                prediction[b_key] = a_val
        else:
            if a_val is not None:
                prediction[a_key] = a_val
            if b_val is not None:
                prediction[b_key] = b_val

    for a_key, b_key in [
        ("oddA", "oddB"),
        ("playerAOdd", "playerBOdd"),
        ("player_a_odd", "player_b_odd"),
        ("coteA", "coteB"),
    ]:
        _copy_odd_pair(a_key, b_key)

    for key in ["oddsSource", "oddsStatus", "oddsSourceMatch", "oddsAudit"]:
        if key in source_match:
            prediction[key] = source_match.get(key)

    return prediction


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


def _empty_response(status: str, message: str = "", target_day: str = "") -> Dict[str, Any]:
    try:
        state = get_state()
        history_rows_loaded = int(state.get("history_rows_loaded", 0))
    except Exception:
        history_rows_loaded = 0

    return {
        "matches": [],
        "summary": {
            "totalRows": 0,
            "validRows": 0,
            "errorRows": 0,
            "over80": 0,
            "vetoCount": 0,
            "jouables": 0,
            "proches": 0,
            "refusedNoVeto": 0,
            "refusesSansVeto": 0,
        },
        "engine": {
            "name": "Tennis Motor V7",
            "version": "Bayesian Shrinkage + Real History Signals",
            "historyYears": list(HISTORY_YEARS),
            "historyRowsLoaded": history_rows_loaded,
            "premiumFormula": "Bayesian shrinkage blend of SWE, ATP, Rank, Form5, Form10, SurfaceForm5, Dominance; API-Tennis names resolved to historical keys",
            "threshold": "> 0.80",
            "status": status,
        },
        "daily": {
            "targetDay": target_day,
            "payloadCount": 0,
            "excludedPlayers": _excluded_analysis_names(),
        },
        "error": message,
    }


def _positive_points(value: Any) -> bool:
    try:
        if value is None:
            return False
        return float(str(value).replace(",", ".").strip()) > 0
    except Exception:
        return False


def _is_placeholder_player_name(value: Any) -> bool:
    name = str(value or "").strip()
    if not name:
        return True

    normalized = re.sub(r"\s+", " ", name).strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)

    placeholder_compact = {
        "tbd",
        "tb",
        "bye",
        "wsf",
        "wsf1",
        "wsf2",
        "wqf",
        "wqf1",
        "wqf2",
        "winner",
        "loser",
        "qualifier",
        "qualified",
        "semifinalwinner",
        "semifinalwinner1",
        "semifinalwinner2",
        "semifinalist",
        "to be decided".replace(" ", ""),
    }
    if compact in placeholder_compact:
        return True

    patterns = [
        r"^(qf|sf|pf|wsf|wqf)\s*\d*$",
        r"^(winner|loser|qualifier|bye)\s*\d*$",
        r"^(winner|loser)\s+(of\s+)?(semi[- ]?final|quarter[- ]?final|final|sf|qf|pf)\s*\d*$",
        r"^(semi[- ]?final|quarter[- ]?final|final|sf|qf|pf)\s+(winner|loser)\s*\d*$",
        r"^to\s+be\s+(decided|determined)$",
        r"^tbd\s*\d*$",
    ]
    return any(re.fullmatch(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns)


def _is_placeholder_match(match: Dict[str, Any]) -> bool:
    player_a = _get_first_existing(match, ["playerA", "player_a"], "")
    player_b = _get_first_existing(match, ["playerB", "player_b"], "")
    return _is_placeholder_player_name(player_a) or _is_placeholder_player_name(player_b)


def _filter_placeholder_matches(matches: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    kept: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []

    for match in matches or []:
        if _is_placeholder_match(match):
            player_a = str(_get_first_existing(match, ["playerA", "player_a"], "") or "")
            player_b = str(_get_first_existing(match, ["playerB", "player_b"], "") or "")
            removed.append({
                "playerA": player_a,
                "playerB": player_b,
                "sourceOriginalPair": f"{player_a} vs {player_b}",
                "reason": "placeholder_player_not_displayed",
                "message": "Joueurs pas encore connus par API-Tennis.",
                "sportradarSportEventId": match.get("sportradarSportEventId") or match.get("sportEventId") or match.get("id") or "",
                "round": match.get("round", ""),
                "startTime": match.get("startTime", ""),
                "tournament": match.get("tournament", ""),
            })
            continue
        kept.append(match)

    return kept, removed


def _not_analyzed_row(source_match: Dict[str, Any], reason_code: str, reason_text: str) -> Dict[str, Any]:
    player_a = str(_get_first_existing(source_match, ["playerA", "player_a"], "") or "")
    player_b = str(_get_first_existing(source_match, ["playerB", "player_b"], "") or "")
    points_a = _get_first_existing(source_match, ["playerAPoints", "player_a_points"], 0)
    points_b = _get_first_existing(source_match, ["playerBPoints", "player_b_points"], 0)

    row = dict(source_match)
    row.update({
        "playerA": player_a,
        "playerB": player_b,
        "playerAPoints": points_a if points_a is not None else 0,
        "playerBPoints": points_b if points_b is not None else 0,
        "premium": 0.0,
        "premiumPct": 0.0,
        "veto": "non",
        "decision": "❌ Non analysé",
        "nonAnalyzable": True,
        "analysisStatus": "not_analyzed",
        "analysisBlockedReason": reason_code,
        "reason": reason_text,
        "error": reason_text,
        "manualRequired": False,
        "manualReviewRequired": bool(source_match.get("manualReviewRequired", False)),
        "engineOrientation": "not_analyzed",
        "engineComparedOriginalPct": 0.0,
        "engineComparedReversedPct": 0.0,
        "sourcePlayerA": player_a,
        "sourcePlayerB": player_b,
        "sourceOriginalPair": f"{player_a} vs {player_b}",
    })
    reasons = list(row.get("manualReviewReasons") or [])
    if reason_code not in reasons:
        reasons.append(reason_code)
    row["manualReviewReasons"] = reasons
    return row


def _split_analyzable_matches(matches: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Règle verrouillée : aucun calcul moteur si points ATP absents ou à 0."""
    analyzable: List[Dict[str, Any]] = []
    not_analyzed: List[Dict[str, Any]] = []

    for match in matches or []:
        points_a = _get_first_existing(match, ["playerAPoints", "player_a_points"], None)
        points_b = _get_first_existing(match, ["playerBPoints", "player_b_points"], None)

        if not _positive_points(points_a) or not _positive_points(points_b):
            not_analyzed.append(_not_analyzed_row(match, "points_atp_missing", "points ATP manquants"))
            continue

        if _is_placeholder_match(match):
            row = _not_analyzed_row(match, "placeholder_player", "joueur non connu / placeholder")
            row["manualRequired"] = True
            row["manualReviewRequired"] = True
            not_analyzed.append(row)
            continue

        analyzable.append(match)

    return analyzable, not_analyzed


def _rebuild_not_analyzed_reasons(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows or []:
        reason = str(row.get("analysisBlockedReason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts




def _step56_category_from_confidence(confidence: float) -> str:
    if confidence >= 0.88:
        return "STEP56_ELITE"
    if confidence > 0.80:
        return "STEP56_PREMIUM"
    if confidence >= 0.75:
        return "STEP56_PROCHE"
    return "STEP56_REFUSE"



def _safe_float_value(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        s = str(value).strip().replace(",", ".")
        if not s:
            return default
        return float(s)
    except Exception:
        return default


def _predicted_odd(match: Dict[str, Any]) -> float:
    """Return the displayed odd for the current predicted winner.

    In Tennis Motor payloads, the final displayed pick is always playerA after
    the orientation pass. Therefore oddA/playerAOdd/coteA is the odd for the
    predicted winner.
    """
    for key in ("oddA", "playerAOdd", "player_a_odd", "coteA", "oddPredicted"):
        value = _safe_float_value(match.get(key), 0.0)
        if value > 1.0:
            return value
    return 0.0


def _category_without_veto(match: Dict[str, Any]) -> str:
    if not isinstance(match, dict):
        return ""
    if match.get("nonAnalyzable") or match.get("analysisStatus") == "not_analyzed" or match.get("error"):
        return ""
    pct = _premium_score(match)
    if pct > 80.0 and "pas jouable" not in str(match.get("decision", "")).lower():
        return "PREMIUM"
    if 75.0 <= pct < 80.0:
        return "PROCHE"
    return "REFUSE"


def _apply_refuse_value_engine(match: Dict[str, Any]) -> Dict[str, Any]:
    """STEP61: value analysis only for REFUSE after veto is audit-only.

    This does not change the prediction probability or the predicted winner.
    It only adds a financial classification for refused matches.
    """
    if not isinstance(match, dict):
        return match

    category = _category_without_veto(match)
    pct = _premium_score(match)
    odd = _predicted_odd(match)
    implied_pct = (100.0 / odd) if odd > 1.0 else 0.0
    ev_pct = ((pct / 100.0) * odd - 1.0) * 100.0 if odd > 1.0 else 0.0

    match["refuseValueEngineVersion"] = "step61-refuse-value-veto-audit-only"
    match["refuseValueApplies"] = category == "REFUSE"
    match["refuseValueCategoryBase"] = category
    match["refuseValueOdd"] = round(odd, 3) if odd > 0 else 0.0
    match["refuseValueImpliedPct"] = round(implied_pct, 2)
    match["refuseValueEvPct"] = round(ev_pct, 2)
    match["refuseValueCote180"] = bool(category == "REFUSE" and odd > 1.0 and odd <= 1.80)
    match["refuseValueLarge"] = bool(category == "REFUSE" and odd > 1.0 and odd <= 1.80 and 60.0 <= pct <= 72.0)
    match["refuseValueStrict"] = bool(category == "REFUSE" and odd > 1.0 and odd <= 1.80 and 68.0 <= pct <= 72.0)

    if category != "REFUSE":
        status = "NOT_REFUSE"
        label = "—"
        reason = "Règle Refusé Value non appliquée : le match n'est pas en catégorie REFUSE."
    elif odd <= 1.0:
        status = "NO_ODDS"
        label = "❌ REFUSÉ — cote manquante"
        reason = "Refusé sans cote exploitable : pas de classement value."
    elif odd > 1.80:
        status = "DANGER_ODD_GT_180"
        label = "❌ REFUSÉ DANGER"
        reason = "Refusé avec cote > 1.80 : zone négative dans l'historique actuel."
    elif 68.0 <= pct <= 72.0:
        status = "VALUE_STRICT"
        label = "✅ REFUSÉ VALUE STRICT"
        reason = "Refusé 68-72% avec cote <= 1.80 : meilleure zone actuelle de l'audit."
    elif 60.0 <= pct <= 72.0:
        status = "VALUE_LARGE"
        label = "✅ REFUSÉ VALUE LARGE"
        reason = "Refusé 60-72% avec cote <= 1.80 : zone value large actuelle."
    else:
        status = "COTE_180_ONLY"
        label = "⚖ REFUSÉ COTE ≤ 1.80"
        reason = "Refusé avec cote <= 1.80 mais hors zone 60-72%. À surveiller, pas strict."

    match["refuseValueStatus"] = status
    match["refuseValueDecision"] = status
    match["refuseValueLabel"] = label
    match["refuseValueReason"] = reason
    return match

def _apply_step56_official_fields(chosen: Dict[str, Any], audit: Dict[str, Any], step49_best: Dict[str, Any], original_score: float, reversed_score: float) -> Dict[str, Any]:
    """STEP61: make STEP56 the official probability/decision while preserving STEP49 traces."""
    if not isinstance(chosen, dict) or not isinstance(audit, dict) or audit.get("status") != "ok":
        return chosen

    confidence = float(audit.get("confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))

    # STEP61: clay veto is preserved as audit metadata but no longer blocks
    # official category/decision. This matches the historical Refusés period
    # used to build the Refuse Value rules.
    veto_raw_active = _is_veto(chosen)
    chosen["vetoAudit"] = "oui" if veto_raw_active else "non"
    chosen["vetoAuditActive"] = bool(veto_raw_active)
    chosen["vetoAuditOriginalValue"] = chosen.get("veto", "non")
    chosen["vetoAuditPolicy"] = "audit_only_no_engine_block_step61"
    chosen["veto"] = "non"
    chosen["clayVetoBlockingDisabled"] = True

    chosen["step49Backup"] = {
        "playerA": step49_best.get("playerA"),
        "playerB": step49_best.get("playerB"),
        "premium": step49_best.get("premium"),
        "premiumPct": step49_best.get("premiumPct"),
        "veto": step49_best.get("veto"),
        "decision": step49_best.get("decision"),
        "originalPct": round(original_score, 3),
        "reversedPct": round(reversed_score, 3),
    }
    official_audit = dict(audit)
    official_audit["mode"] = "official_daily_engine"
    official_audit["decisionMutation"] = True
    official_audit["historyWriteEnabled"] = True
    chosen["step56Official"] = official_audit
    chosen["step56OfficialEngine"] = True
    chosen["step56OfficialCategory"] = _step56_category_from_confidence(confidence)
    chosen["step56ProbabilityAOriginalPct"] = audit.get("probabilityAPct")
    chosen["step56PredictedWinner"] = audit.get("predictedWinner")
    chosen["step56PredictedWinnerSideOriginal"] = audit.get("predictedWinnerSide")
    chosen["step56Confidence"] = round(confidence, 6)
    chosen["step56ConfidencePct"] = round(confidence * 100.0, 2)
    chosen["premium"] = round(confidence, 3)
    chosen["premiumPct"] = round(confidence * 100.0, 1)
    chosen["decision"] = "✅ Jouable" if confidence > 0.80 else "❌ Pas jouable"
    chosen["officialEngine"] = "STEP56 Global Direct Prediction"
    chosen["officialEngineVersion"] = "step61-refuse-value-veto-audit-only"
    chosen["oddsUsedByOfficialEngine"] = False
    chosen["playerNamesUsedAsFeatures"] = False
    chosen = _apply_refuse_value_engine(chosen)
    return chosen


def _build_refuse_value_summary(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = {"totalRefuse": 0, "cote180": 0, "large": 0, "strict": 0, "danger": 0, "noOdds": 0, "other": 0}
    for match in matches or []:
        if not isinstance(match, dict):
            continue
        if _category_without_veto(match) != "REFUSE":
            continue
        out["totalRefuse"] += 1
        status = str(match.get("refuseValueStatus") or "")
        if match.get("refuseValueCote180"):
            out["cote180"] += 1
        if match.get("refuseValueLarge"):
            out["large"] += 1
        if match.get("refuseValueStrict"):
            out["strict"] += 1
        if status.startswith("DANGER"):
            out["danger"] += 1
        elif status == "NO_ODDS":
            out["noOdds"] += 1
        elif status not in {"VALUE_STRICT", "VALUE_LARGE", "COTE_180_ONLY"}:
            out["other"] += 1
    return out


def _refresh_refuse_value_fields(response: Dict[str, Any]) -> None:
    matches = response.get("matches") if isinstance(response, dict) else []
    if not isinstance(matches, list):
        return
    for match in matches:
        if isinstance(match, dict):
            _apply_refuse_value_engine(match)
    summary = _rebuild_summary_from_matches(matches)
    rv = _build_refuse_value_summary(matches)
    summary.update({
        "refuseValueCote180": rv.get("cote180", 0),
        "refuseValueLarge": rv.get("large", 0),
        "refuseValueStrict": rv.get("strict", 0),
        "refuseDanger": rv.get("danger", 0),
    })
    response["summary"] = summary
    response.setdefault("daily", {})
    if isinstance(response["daily"].get("refuseValueEngine"), dict):
        response["daily"]["refuseValueEngine"]["counts"] = rv

def calculate_from_matches(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    original_match_count = len(matches or [])
    matches, excluded_removed = _filter_excluded_analysis_matches(matches or [])
    matches, placeholder_removed = _filter_placeholder_matches(matches)
    matches, not_analyzed_matches = _split_analyzable_matches(matches)

    if not matches and not not_analyzed_matches:
        if placeholder_removed and not excluded_removed:
            status = "empty_payload_after_placeholder_filter"
            message = "Aucun match exploitable : API-Tennis ne donne pas encore les vrais joueurs."
        elif excluded_removed:
            status = "empty_payload_after_exclusion"
            message = "Aucun match exploitable après exclusion joueur."
        else:
            status = "empty_payload"
            message = "Aucun match exploitable dans le payload."

        response = _empty_response(status=status, message=message)
        response["daily"]["excludedMatches"] = len(excluded_removed)
        response["daily"]["excludedSample"] = excluded_removed[:10]
        response["daily"]["placeholderFilteredMatches"] = len(placeholder_removed)
        response["daily"]["placeholderFilteredSample"] = placeholder_removed[:10]
        response["daily"]["placeholderFilterPolicy"] = "api_tennis_atp_singles_only"
        response["daily"]["originalPayloadMatches"] = original_match_count
        response["daily"]["notAnalyzedMatches"] = 0
        return response

    state = get_state()
    auditor = get_step56_auditor()
    final_matches: List[Dict[str, Any]] = []
    step56_reversed_chosen = 0
    step56_fallback_to_step49 = 0
    step56_counts = {"ok": 0, "error": 0, "elite": 0, "premium": 0, "proche": 0, "refuse": 0, "veto": 0, "disagreeWithStep49Pick": 0}

    for match in matches:
        original_prediction = calculate_match_prediction(match, state)
        reversed_input = _reverse_match_for_engine(match)
        reversed_prediction = calculate_match_prediction(reversed_input, state)

        original_score = _premium_score(original_prediction)
        reversed_score = _premium_score(reversed_prediction)

        if reversed_score > original_score:
            step49_best = dict(reversed_prediction)
            step49_orientation = "reversed"
        else:
            step49_best = dict(original_prediction)
            step49_orientation = "original"
        step49_best = _copy_daily_context_to_prediction(match, step49_best, step49_orientation)

        audit = auditor.audit_match(match)
        if isinstance(audit, dict) and audit.get("status") == "ok":
            step56_counts["ok"] += 1
            predicted_side = str(audit.get("predictedWinnerSide") or "A").upper()
            if predicted_side == "B":
                chosen_input = reversed_input
                orientation = "reversed"
                step56_reversed_chosen += 1
            else:
                chosen_input = match
                orientation = "original"

            chosen = dict(calculate_match_prediction(chosen_input, state))
            chosen = _copy_daily_context_to_prediction(match, chosen, orientation)
            chosen = _apply_step56_official_fields(chosen, audit, step49_best, original_score, reversed_score)

            cat = str(chosen.get("step56OfficialCategory") or "")
            if cat == "STEP56_ELITE":
                step56_counts["elite"] += 1
            elif cat == "STEP56_PREMIUM":
                step56_counts["premium"] += 1
            elif cat == "STEP56_PROCHE":
                step56_counts["proche"] += 1
            else:
                step56_counts["refuse"] += 1
            if _is_veto(chosen):
                step56_counts["veto"] += 1
            if str(step49_best.get("playerA") or "") != str(chosen.get("playerA") or ""):
                step56_counts["disagreeWithStep49Pick"] += 1
        else:
            # Sécurité : si STEP56 n'arrive pas à calculer une ligne, on garde l'ancien moteur
            # mais on marque l'erreur pour audit. Cela évite de casser /daily.
            step56_counts["error"] += 1
            step56_fallback_to_step49 += 1
            chosen = dict(step49_best)
            chosen["step56Official"] = audit
            chosen["step56OfficialEngine"] = False
            chosen["officialEngine"] = "STEP49 fallback because STEP56 failed"
            chosen["officialEngineVersion"] = "step61-refuse-value-veto-audit-only-fallback"

        source_player_a = str(match.get("playerA") or match.get("player_a") or "")
        source_player_b = str(match.get("playerB") or match.get("player_b") or "")
        chosen["engineOrientation"] = "step56_" + str(chosen.get("contextOrientation") or "original")
        chosen["engineComparedOriginalPct"] = round(original_score, 3)
        chosen["engineComparedReversedPct"] = round(reversed_score, 3)
        chosen["sourcePlayerA"] = source_player_a
        chosen["sourcePlayerB"] = source_player_b
        chosen["sourceOriginalPair"] = f"{source_player_a} vs {source_player_b}"
        final_matches.append(chosen)

    final_matches, excluded_removed_after_engine = _filter_excluded_analysis_matches(final_matches)
    excluded_removed.extend(excluded_removed_after_engine)
    final_matches.sort(key=lambda row: row.get("premium", -1), reverse=True)
    all_matches = final_matches + not_analyzed_matches
    summary = _rebuild_summary_from_matches(all_matches)
    refuse_value_summary = _build_refuse_value_summary(all_matches)
    summary.update({
        "refuseValueCote180": refuse_value_summary.get("cote180", 0),
        "refuseValueLarge": refuse_value_summary.get("large", 0),
        "refuseValueStrict": refuse_value_summary.get("strict", 0),
        "refuseDanger": refuse_value_summary.get("danger", 0),
    })

    return {
        "matches": all_matches,
        "summary": summary,
        "engine": {
            "name": "Tennis Motor STEP56",
            "version": "Global Direct Prediction Official No Odds",
            "serviceVersion": "step61-refuse-value-veto-audit-only",
            "historyYears": list(HISTORY_YEARS),
            "historyRowsLoaded": state["history_rows_loaded"],
            "premiumFormula": "STEP56 Global Direct Prediction: 88 tennis features, no odds, no player names as model features; names used only for historical lookup.",
            "threshold": "> 0.80",
            "orientationMode": "step56_direct_probability_pick_side",
            "coreFallback": "STEP49 only if STEP56 audit computation fails on a row",
        },
        "daily": {
            "doubleSideStatus": "ok",
            "doubleSideMode": "step56_official_pick_side_with_step49_trace",
            "doubleSideMatches": len(final_matches),
            "doubleSideReversedChosen": step56_reversed_chosen,
            "step56Official": {
                "status": "ok" if step56_counts["error"] == 0 else "partial",
                "serviceVersion": "step61-refuse-value-veto-audit-only",
                "model": "STEP56 Global Direct Prediction",
                "officialDecisionsMutatedByStep56": True,
                "historyWriteEnabled": True,
                "noOddsUsed": True,
                "noPlayerNamesAsFeatures": True,
                "featureCount": len(get_step56_auditor().feature_order) if getattr(get_step56_auditor(), "feature_order", None) else 88,
                "counts": step56_counts,
                "fallbackToStep49Rows": step56_fallback_to_step49,
            },
            "refuseValueEngine": {
                "status": "enabled",
                "serviceVersion": "step61-refuse-value-veto-audit-only",
                "scope": "REFUSE only",
                "usesOddsForPrediction": False,
                "usesOddsForValueOnly": True,
                "rules": {
                    "cote180": "category=REFUSE and odd<=1.80",
                    "large": "category=REFUSE and 60<=premiumPct<=72 and odd<=1.80",
                    "strict": "category=REFUSE and 68<=premiumPct<=72 and odd<=1.80",
                    "danger": "category=REFUSE and (odd>1.80 or no odds or out of value zones)",
                },
                "counts": refuse_value_summary,
            },
            "vetoAuditOnly": {
                "status": "enabled",
                "blockingDisabled": True,
                "policy": "round-wins clay veto kept as vetoAudit metadata but does not block category/history",
            },
            "contextPropagation": "step61_step56_official_refuse_value_veto_audit_only",
            "excludedPlayers": _excluded_analysis_names(),
            "excludedMatches": len(excluded_removed),
            "excludedSample": excluded_removed[:10],
            "placeholderFilteredMatches": len(placeholder_removed),
            "placeholderFilteredSample": placeholder_removed[:10],
            "placeholderFilterPolicy": "hide_sportradar_tbd_wsf_winner_placeholders",
            "notAnalyzedMatches": len(not_analyzed_matches),
            "notAnalyzedReasons": _rebuild_not_analyzed_reasons(not_analyzed_matches),
            "originalPayloadMatches": original_match_count,
        },
    }



def _apply_form_value_engine(response: Dict[str, Any]) -> None:
    """STEP34: active Form/Value layer connected to multi-year history.

    Adds formValue fields to every current match without deleting the original motor traces.
    Unity can display the active value decision and the backend can persist it in raw_json.
    """
    response.setdefault("daily", {})
    matches = response.get("matches") if isinstance(response, dict) else []
    if not isinstance(matches, list) or not matches:
        response["daily"]["formValueEngine"] = {
            "status": "skipped",
            "reason": "no_matches",
            "version": "step49-clay-veto-restored",
        }
        return

    store = PostgresPremiumStore()
    if not store.enabled:
        response["daily"]["formValueEngine"] = {
            "status": "skipped",
            "reason": "database_not_configured",
            "version": "step49-clay-veto-restored",
        }
        return

    try:
        report = store.form_value_report(category="ALL", limit=50000)
    except Exception as exc:
        response["daily"]["formValueEngine"] = {
            "status": "error",
            "version": "step49-clay-veto-restored",
            "error": f"{type(exc).__name__}: {exc}",
        }
        return

    counts = {"scored": 0, "promote": 0, "downgrade": 0, "keep": 0, "wait": 0, "ignored": 0}
    sample: List[Dict[str, Any]] = []

    for match in matches:
        if not isinstance(match, dict):
            counts["ignored"] += 1
            continue
        category = tracked_category(match)
        if not category:
            counts["ignored"] += 1
            continue
        value = score_match_with_form_value(match, category, report)
        match.update(value)
        match["formValueEngineVersion"] = "step49-clay-veto-restored"
        old_decision = str(match.get("decision") or "")
        action = str(value.get("formValueAction") or "")
        if action == "PROMOTE":
            counts["promote"] += 1
            match["decisionWithForm"] = "✅ Jouable Form Value"
            match["formValueFinalDecision"] = "PROMOTE"
        elif action == "DOWNGRADE":
            counts["downgrade"] += 1
            match["decisionWithForm"] = "❌ Danger Form Value"
            match["formValueFinalDecision"] = "DOWNGRADE"
        elif action == "WAIT":
            counts["wait"] += 1
            match["decisionWithForm"] = old_decision
            match["formValueFinalDecision"] = "WAIT"
        else:
            counts["keep"] += 1
            match["decisionWithForm"] = old_decision
            match["formValueFinalDecision"] = "KEEP"
        counts["scored"] += 1
        if len(sample) < 12:
            sample.append({
                "match": f"{match.get('playerA')} vs {match.get('playerB')}",
                "category": category,
                "action": action,
                "score": value.get("formValueScore"),
                "roiPct": value.get("formValueRoiPct"),
                "edgePct": value.get("formValueEdgePct"),
                "label": value.get("formValueLabel"),
            })

    response["daily"]["formValueEngine"] = {
        "status": "ok",
        "version": "step49-clay-veto-restored",
        "mode": "active_history_value_layer",
        "counts": counts,
        "categoryRanking": report.get("ranking", []),
        "sample": sample,
        "policy": "Couche Form/Historique active validée utilisateur : pForm5, pForm10, pSurfaceForm5, pDominance, pSWE, pATP, premiumPct et cote sont reliés aux décisions formValue.",
    }


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "Tennis Motor Backend Clean",
        "version": "step62-refuse-value-persistent-history",
        "coreEngineVersion": "step56-global-direct-prediction-official-with-persistent-refuse-value",
        "message": "Backend STEP62 : STEP56 officiel + Refuse Value Engine persistant PostgreSQL, veto terre battue en audit only, aucun appel Sportradar.",
        "endpoints": ["/health", "/calculate", "/predictions", "/state", "/history", "/daily", "/api-tennis/status", "/odds/status", "/sync/results2026/status", "/sync/results2026/run", "/sync/results2026/postgres/status", "/sync/results2026/postgres/export", "/sync/premium/status", "/sync/premium/list", "/sync/premium/reset", "/sync/premium/run", "/sync/premium/settle", "/sync/premium/settle-pending", "/sync/history/form-value", "/sync/history/list", "/sync/history/reset", "/sync/history/repair-dellien-royer", "/sync/history/repair-shelton-merida", "/sync/history/repair-wawrinka-fils-dejong", "/sync/history/repair-van-assche-kypson-gaubas", "/sync/history/settle", "/sync/history/settle-pending", "/sync/daily-maintenance/run", "/sync/refuse-value/history", "/sync/refuse-value/backfill", "/audit/step56/status", "/audit/step56/daily", "/audit/step56/calculate"],
        "excludedAnalysisPlayers": _excluded_analysis_names(),
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    state = get_state()
    return {
        "status": "ok",
        "service": "Tennis Motor Backend Clean",
        "version": "step62-refuse-value-persistent-history",
        "coreEngineVersion": "step56-global-direct-prediction-official-with-persistent-refuse-value",
        "step56Official": "enabled_official_engine_refuse_value_veto_audit_only",
        "step56AuditEndpoint": "/audit/step56/daily",
        "officialDecisionEngine": "step56-global-direct-prediction",
        "officialDecisionsMutatedByStep56": True,
        "historyYears": list(HISTORY_YEARS),
        "historyRowsLoaded": state.get("history_rows_loaded", 0),
        "excludedAnalysisPlayers": _excluded_analysis_names(),
        "dailyProvider": "api_tennis",
        "oddsProvider": "flashscore",
        "oddsUsage": "display_only_not_used_by_engine",
        "results2026Sync": "enabled",
        "results2026Storage": "postgres" if os.environ.get("DATABASE_URL", "").strip() else "csv",
        "premiumHistoryStorage": "postgres" if os.environ.get("DATABASE_URL", "").strip() else "unavailable",
        "databaseUrlConfigured": bool(os.environ.get("DATABASE_URL", "").strip()),
        "apiTennisKeyConfigured": bool(os.environ.get("API_TENNIS_KEY", "").strip()),
        "apiTennisEventTypeKey": "265",
        "sportradarDisabled": True,
        "qualifierDetector": "api_tennis_round_wins_active_qualifier_audit_only",
        "qualifierDetectorActivation": "tournament_wins_veto_audit_only_no_engine_block",
        "clayVetoPolicy": "audit_only_on_clay_round_wins_no_engine_block_step61",
        "placeholderFilter": "api_tennis_atp_singles_only",
        "motorSignals": "step56_88_features_global_direct_prediction_no_odds_no_player_names_as_features",
        "step56Policy": "official_daily_engine_no_odds_no_player_names_refuse_value_history_write_veto_audit_only",
        "step62PersistentRefuseValue": True,
        "refuseValueEngine": "enabled_for_refuse_only_persistent_history",
        "refuseValueHistory": "postgres_columns_plus_history_endpoint",
        "refuseValueRules": "cote<=1.80; large=60-72+cote<=1.80; strict=68-72+cote<=1.80",
        "vetoBlocking": "disabled_audit_only",
    }


@app.post("/calculate")
async def calculate(request: Request) -> Dict[str, Any]:
    matches = await _read_request_matches(request)
    result = calculate_from_matches(matches)
    result.setdefault("daily", {})
    result["daily"]["source"] = "manual_payload"
    return result


@app.post("/predictions")
async def predictions_post(request: Request) -> Dict[str, Any]:
    return await calculate(request)


@app.get("/daily")
def daily(day: str = Query("today"), auto_history: bool = Query(True), provider: str = Query("api_tennis")) -> Dict[str, Any]:
    target_day = normalize_day(day)
    selected_provider = (provider or os.environ.get("DAILY_PROVIDER", "api_tennis") or "api_tennis").strip().lower()

    # STEP47 : API-Tennis est le fournisseur unique.
    # Même si un ancien client envoie provider=sportradar, on force API-Tennis.
    builder = ApiTennisDailyBuilder(audit_dir=AUDIT_DIR)
    built = builder.build_matches_for_day(target_day)
    provider_name = "api_tennis"
    provider_key_configured = bool(os.environ.get("API_TENNIS_KEY", "").strip())
    error_status = "api_tennis_error"
    error_message = "Erreur API-Tennis inconnue."

    if built.get("status") != "ok":
        response = _empty_response(
            status=error_status,
            message=str(built.get("error") or error_message),
            target_day=target_day,
        )
        response["daily"].update({
            "provider": provider_name,
            "providerRequested": selected_provider,
            "sportradarForcedOff": True,
            "step": "48",
            "targetDay": target_day,
            "audit": built.get("audit", {}),
            "apiKeyConfigured": provider_key_configured,
        })
        return response

    source_matches = built.get("matches", [])
    response = calculate_from_matches(source_matches)
    # STEP47 : /daily doit exposer un statut top-level explicite.
    # Certains flux internes (cron_daily_maintenance) vérifient ce champ avant
    # d'écrire l'historique. Les anciennes réponses avaient un payload valide
    # mais pas de champ "status", ce qui provoquait historySync=skipped.
    response["status"] = "ok"

    # Step 2.10 : cotes Flashscore uniquement pour affichage Unity.
    # Le moteur ne lit jamais ces champs et ne les utilise pas dans la décision.
    try:
        odds_provider = FlashscoreOddsProvider()
        odds_result = odds_provider.enrich_daily_response(response, target_day=target_day)
        # Odds arrive after prediction. Recompute only the Refuse Value layer;
        # do not touch STEP56 probability or official pick.
        _refresh_refuse_value_fields(response)
        response.setdefault("daily", {})
        response["daily"]["odds"] = odds_result.get("audit", {})
    except Exception as exc:
        response.setdefault("daily", {})
        response["daily"]["odds"] = {
            "status": "error",
            "provider": "flashscore",
            "error": f"{type(exc).__name__}: {exc}",
            "policy": "odds_display_only_engine_ignored",
        }

    response.setdefault("daily", {})
    response["daily"].update({
        "provider": provider_name,
        "providerRequested": selected_provider,
        "sportradarForcedOff": True,
        "step": "48",
        "targetDay": target_day,
        "payloadCount": len(source_matches),
        "audit": built.get("audit", {}),
        "manualReviewPolicy": "points ATP absents ou à 0 = non analysé; API-Tennis fournit les matchs ATP simples via event_type_key=265; noms API-Tennis enrichis par get_standings ATP quand possible; aucune donnée n'est inventée.",
        "oddsPolicy": "Flashscore odds are display-only; STEP56 official engine ignores odds completely.",
        "apiTennisProviderActive": provider_name == "api_tennis",
        "apiTennisKeyConfigured": bool(os.environ.get("API_TENNIS_KEY", "").strip()),
    })

    # STEP61 : aucune cote dans le calcul officiel.
    # La couche Form Value historique est désactivée pour éviter toute confusion :
    # les cotes Flashscore restent affichées uniquement, mais ne modifient ni premiumPct,
    # ni decision, ni historySync.
    response.setdefault("daily", {})
    response["daily"]["formValueEngine"] = {
        "status": "disabled",
        "version": "step62-refuse-value-persistent-history",
        "mode": "disabled_no_odds_in_official_calculations",
        "policy": "STEP56 officiel : aucune cote utilisée dans premiumPct, decision ou historique.",
    }

    # STEP25 : sauvegarde automatique historique moteur catégorisé.
    # Les jours futurs ne sont jamais enregistrés pour éviter de polluer l'historique.
    if auto_history:
        try:
            target_date = date.fromisoformat(target_day)
            if target_date <= _paris_today():
                syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
                sync_result = syncer.sync_daily_result(response, target_day, dry_run=False)
                response["daily"]["premiumHistorySync"] = sync_result
            else:
                response["daily"]["premiumHistorySync"] = {
                    "status": "skipped",
                    "reason": "future_day_not_saved",
                    "targetDay": target_day,
                    "policy": "Aucun historique Premium/Proche n'est enregistré pour demain ou une date future.",
                }
        except Exception as exc:
            response["daily"]["premiumHistorySync"] = {
                "status": "error",
                "targetDay": target_day,
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        response["daily"]["premiumHistorySync"] = {
            "status": "skipped",
            "reason": "auto_history_disabled",
            "targetDay": target_day,
        }

    return response


@app.get("/audit/step56/status")
def step56_audit_status() -> Dict[str, Any]:
    """STEP59: dark audit status. Does not change official motor decisions."""
    auditor = get_step56_auditor()
    return auditor.status()


@app.post("/audit/step56/calculate")
async def step56_audit_calculate(request: Request) -> Dict[str, Any]:
    """STEP59: audit a manual payload with STEP56 features/probability without mutating official decisions."""
    matches = await _read_request_matches(request)
    response = calculate_from_matches(matches)
    response.setdefault("daily", {})
    response["daily"]["source"] = "manual_payload_step56_audit_only"
    response["daily"]["auditOnly"] = True
    response["daily"]["historyWrite"] = False
    auditor = get_step56_auditor()
    auditor.enrich_response(response)
    response["status"] = "ok"
    return response


@app.get("/audit/step56/daily")
def step56_audit_daily(day: str = Query("today"), provider: str = Query("api_tennis")) -> Dict[str, Any]:
    """STEP59: run normal daily in auto_history=false, then add STEP56 audit fields.

    Official STEP49 premiumPct/decision/veto are not changed and no history write is triggered here.
    """
    response = daily(day=day, auto_history=False, provider=provider)
    response.setdefault("daily", {})
    response["daily"]["auditOnly"] = True
    response["daily"]["historyWrite"] = False
    response["daily"]["officialEngineUnchanged"] = True
    response["daily"]["step59Policy"] = "STEP56 official engine is active on /daily; this audit endpoint does not write history; no odds used by STEP56."
    auditor = get_step56_auditor()
    auditor.enrich_response(response)
    response["status"] = "ok"
    return response


@app.get("/predictions")
def predictions_get(day: str = Query("today")) -> Dict[str, Any]:
    return daily(day)


@app.get("/odds/status")
def odds_status() -> Dict[str, Any]:
    provider = FlashscoreOddsProvider()
    audit = provider.fetch_odds_audit()
    return {
        "status": "ok" if audit.get("status") in {"ok", "partial"} else audit.get("status", "unknown"),
        "provider": "flashscore",
        "usage": "display_only_not_used_by_engine",
        "urls": audit.get("urls", []),
        "records": audit.get("records", 0),
        "errors": audit.get("errors", []),
        "warnings": audit.get("warnings", []),
        "serviceVersion": "step49-clay-veto-restored",
    }


@app.get("/sportradar/status")
def sportradar_status() -> Dict[str, Any]:
    return {
        "status": "disabled",
        "provider": "api_tennis",
        "sportradarDisabled": True,
        "apiTennisOnly": True,
        "message": "STEP47 : Sportradar est désactivé. Utilise /api-tennis/status et /daily avec API-Tennis.",
        "serviceVersion": "step49-clay-veto-restored",
    }


@app.get("/api-tennis/status")
def api_tennis_status() -> Dict[str, Any]:
    builder = ApiTennisDailyBuilder(audit_dir=AUDIT_DIR)
    return {
        "status": "ok" if builder.enabled else "missing_key",
        "provider": "api_tennis",
        "apiKeyConfigured": builder.enabled,
        "eventTypeKey": "265",
        "eventTypeType": "Atp Singles",
        "baseUrl": builder.config.base_url,
        "timezone": builder.config.timezone,
        "usage": "STEP36 daily provider: get_standings ATP + get_fixtures ATP Singles.",
        "note": "La clé API n'est jamais renvoyée par le backend.",
    }

@app.get("/state")
def state() -> Dict[str, Any]:
    return get_state()


@app.get("/history")
def history() -> Dict[str, Any]:
    """Historique Premium séparé.

    Step 2.5 : PostgreSQL est prioritaire. Le fallback premium_history.json reste seulement
    pour compatibilité si DATABASE_URL est absent.
    """
    store = PostgresPremiumStore()
    if store.enabled:
        try:
            summary = store.summary(category="PREMIUM")
            return {
                "status": "ok",
                "storage": "postgres",
                "summary": summary.get("summary", summary),
                "chart": summary.get("chart", {}),
                "rows": summary.get("rows", []),
            }
        except Exception as exc:
            return {
                "status": "error",
                "storage": "postgres",
                "error": f"{type(exc).__name__}: {exc}",
                "summary": {},
                "chart": {},
                "rows": [],
            }

    try:
        import premium_history

        summary = premium_history.build_summary(write_cleaned=True)
        rows = premium_history.load_history()
        return {
            "status": "ok",
            "storage": "json_fallback",
            "historyPath": str(premium_history.HISTORY_PATH),
            "summaryPath": str(premium_history.SUMMARY_PATH),
            "summary": summary.get("summary", summary),
            "chart": summary.get("chart", {}),
            "rows": rows,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "summary": {},
            "chart": {},
            "rows": [],
        }


@app.get("/sync/results2026/status")
def sync_results2026_status() -> Dict[str, Any]:
    syncer = ApiTennisResults2026Syncer(base_dir=BASE_DIR)
    status = syncer.status()
    status["serviceVersion"] = "step49-clay-veto-restored"
    return status


@app.get("/sync/results2026/postgres/status")
def sync_results2026_postgres_status() -> Dict[str, Any]:
    syncer = ApiTennisResults2026Syncer(base_dir=BASE_DIR)
    status = syncer.postgres_status()
    status["serviceVersion"] = "step49-clay-veto-restored"
    return status


@app.get("/sync/results2026/postgres/export")
def sync_results2026_postgres_export() -> Dict[str, Any]:
    syncer = ApiTennisResults2026Syncer(base_dir=BASE_DIR)
    result = syncer.export_postgres_to_csv()
    result["serviceVersion"] = "step49-clay-veto-restored"

    try:
        if result.get("status") == "ok":
            import motor as motor_module
            motor_module._STATE = None
            result["motorStateReset"] = True
        else:
            result["motorStateReset"] = False
    except Exception as exc:
        result["motorStateReset"] = False
        result.setdefault("warnings", []).append(f"motor state reset failed: {type(exc).__name__}: {exc}")

    return result


@app.get("/sync/results2026/run")
def sync_results2026_run(day: str = Query("today"), dry_run: bool = Query(False)) -> Dict[str, Any]:
    target_day = normalize_day(day)
    syncer = ApiTennisResults2026Syncer(base_dir=BASE_DIR)
    result = syncer.sync_day(target_day, dry_run=dry_run)
    result["serviceVersion"] = "step49-clay-veto-restored"

    # Si data/2026.csv a été modifié, on force la reconstruction de l'état Elo/Form au prochain calcul.
    try:
        if not dry_run and int((result.get("counts") or {}).get("rows_added") or 0) > 0:
            import motor as motor_module
            motor_module._STATE = None
            result["motorStateReset"] = True
        else:
            result["motorStateReset"] = False
    except Exception as exc:
        result["motorStateReset"] = False
        result.setdefault("warnings", []).append(f"motor state reset failed: {type(exc).__name__}: {exc}")

    return result


@app.get("/sync/premium/status")
def sync_premium_status() -> Dict[str, Any]:
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    status = syncer.status()
    status["serviceVersion"] = "step49-clay-veto-restored"
    return status





def _normalize_history_category_for_api(category: str) -> str:
    cat = str(category or "premium").strip().upper().replace("É", "E").replace("È", "E").replace("Ê", "E")
    aliases = {
        "PREMIUM": "PREMIUM",
        "PROCHE": "PROCHE",
        "PROCHES": "PROCHE",
        "VETO": "VETO",
        "REFUSE": "REFUSE",
        "REFUS": "REFUSE",
        "REFUSES": "REFUSE",
    }
    return aliases.get(cat, "PREMIUM")


def _history_list_payload(
    store: PostgresPremiumStore,
    category: str,
    limit: int,
    auto_settle: bool,
    settle_days_back: int,
) -> Dict[str, Any]:
    cat = _normalize_history_category_for_api(category)
    settle_result = None
    cleanup_result = None
    if auto_settle:
        syncer = PremiumHistorySyncer(store=store)
        settle_result = syncer.settle_pending_recent(days_back=settle_days_back, dry_run=False, provider="api_tennis")
        cleanup_result = store.cleanup_duplicate_events(category=cat)
    else:
        cleanup_result = store.cleanup_duplicate_events(category=cat)

    rows = store.fetch_rows(limit=limit, category=cat)
    counts = store.counts(category=cat)
    summary = store.summary(category=cat)
    try:
        form_value = store.form_value_report(category=cat, limit=50000)
    except Exception as exc:
        form_value = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    items: List[Dict[str, Any]] = []

    for row in rows:
        source_a = str(row.get("sourcePlayerA") or "")
        source_b = str(row.get("sourcePlayerB") or "")
        predicted = str(row.get("predictedWinner") or "")
        opponent = str(row.get("opponent") or "")
        items.append({
            "id": row.get("id"),
            "date": row.get("date"),
            "match": f"{source_a} vs {source_b}".strip(),
            "sourcePlayerA": source_a,
            "sourcePlayerB": source_b,
            "predictedWinner": predicted,
            "opponent": opponent,
            "premiumPct": row.get("premiumPct"),
            "status": row.get("status"),
            "category": row.get("status"),
            "result": row.get("result"),
            "realWinner": row.get("realWinner"),
            "score": row.get("score"),
            "oddPredicted": row.get("oddPredicted"),
            "oddOpponent": row.get("oddOpponent"),
            "oddsSource": row.get("oddsSource"),
            "tournament": row.get("tournament"),
            "round": row.get("round"),
            "surface": row.get("surface"),
            "startTime": row.get("startTime"),
            "settledAt": row.get("settledAt"),
            "winnerId": row.get("winnerId"),
            "sportradarSportEventId": row.get("sportradarSportEventId"),
            "sportradarPlayerAId": row.get("sportradarPlayerAId"),
            "sportradarPlayerBId": row.get("sportradarPlayerBId"),
        })

    return {
        "status": "ok",
        "databaseConfigured": True,
        "databaseStatus": "ok",
        "table": store.TABLE,
        "category": cat,
        "limit": limit,
        "count": len(rows),
        "counts": counts,
        "summary": summary.get("summary", {}),
        "chart": summary.get("chart", {}),
        "formValue": form_value,
        "items": items,
        "rows": rows,
        "settle": settle_result,
        "cleanup": cleanup_result,
        "policy": f"Liste historique moteur STEP33 filtrée : {cat}. Historique durable multi-années; auto-settle sur toutes les dates pending si settle_days_back=0; void/remboursé exclu du ROI; doublons legacy compactés.",
        "serviceVersion": "step49-clay-veto-restored",
    }



def _refuse_value_item_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    source_a = str(row.get("sourcePlayerA") or "")
    source_b = str(row.get("sourcePlayerB") or "")
    item = {
        "id": row.get("id"),
        "date": row.get("date"),
        "match": f"{source_a} vs {source_b}".strip(),
        "sourcePlayerA": source_a,
        "sourcePlayerB": source_b,
        "playerA": row.get("predictedWinner"),
        "playerB": row.get("opponent"),
        "predictedWinner": row.get("predictedWinner"),
        "opponent": row.get("opponent"),
        "premiumPct": row.get("premiumPct"),
        "status": row.get("status"),
        "category": row.get("status"),
        "result": row.get("result"),
        "realWinner": row.get("realWinner"),
        "score": row.get("score"),
        "oddPredicted": row.get("oddPredicted"),
        "oddOpponent": row.get("oddOpponent"),
        "oddsSource": row.get("oddsSource"),
        "tournament": row.get("tournament"),
        "round": row.get("round"),
        "surface": row.get("surface"),
        "startTime": row.get("startTime"),
        "settledAt": row.get("settledAt"),
        "winnerId": row.get("winnerId"),
        "sportradarSportEventId": row.get("sportradarSportEventId"),
        "refuseValueEngineVersion": row.get("refuseValueEngineVersion"),
        "refuseValueApplies": row.get("refuseValueApplies"),
        "refuseValueCategoryBase": row.get("refuseValueCategoryBase"),
        "refuseValueOdd": row.get("refuseValueOdd"),
        "refuseValueImpliedPct": row.get("refuseValueImpliedPct"),
        "refuseValueEvPct": row.get("refuseValueEvPct"),
        "refuseValueCote180": row.get("refuseValueCote180"),
        "refuseValueLarge": row.get("refuseValueLarge"),
        "refuseValueStrict": row.get("refuseValueStrict"),
        "refuseValueDanger": row.get("refuseValueDanger") or row.get("refuseDanger"),
        "refuseDanger": row.get("refuseValueDanger") or row.get("refuseDanger"),
        "refuseValueStatus": row.get("refuseValueStatus"),
        "refuseValueDecision": row.get("refuseValueDecision"),
        "refuseValueLabel": row.get("refuseValueLabel"),
        "refuseValueReason": row.get("refuseValueReason"),
        "vetoAudit": row.get("vetoAudit"),
        "vetoAuditActive": row.get("vetoAuditActive"),
        "vetoAuditPolicy": row.get("vetoAuditPolicy"),
    }
    return item


@app.get("/sync/refuse-value/history")
def sync_refuse_value_history(
    limit: int = Query(20000, ge=1, le=50000),
    filter: str = Query("all"),
    auto_settle: bool = Query(False),
    settle_days_back: int = Query(0, ge=0, le=36500),
) -> Dict[str, Any]:
    """STEP62 : historique durable PostgreSQL des Refusés Value.

    filter = all | cote180 | large | strict | danger | no_odds
    """
    store = PostgresPremiumStore()
    if not store.enabled:
        return {
            "status": "error",
            "databaseConfigured": False,
            "databaseStatus": "not_configured",
            "table": store.TABLE,
            "error": "DATABASE_URL absente dans le service web.",
            "items": [],
            "serviceVersion": "step62-refuse-value-persistent-history",
        }

    try:
        settle_result = None
        if auto_settle:
            syncer = PremiumHistorySyncer(store=store)
            settle_result = syncer.settle_pending_recent(days_back=settle_days_back, dry_run=False, provider="api_tennis")

        # Ensure schema has STEP62 columns and read historical rows from PostgreSQL.
        store.ensure_schema()
        rows = store.fetch_refuse_value_rows(limit=limit, value_filter=filter)
        summary = store.refuse_value_summary(limit=limit, value_filter=filter)
        items = [_refuse_value_item_from_row(row) for row in rows]

        return {
            "status": "ok",
            "databaseConfigured": True,
            "databaseStatus": "ok",
            "table": store.TABLE,
            "version": "step62-refuse-value-persistent-history",
            "category": "REFUSE_VALUE",
            "filter": filter,
            "limit": limit,
            "count": len(items),
            "counts": summary,
            "summary": summary,
            "chartDays": summary.get("chartDays", []),
            "items": items,
            "rows": rows,
            "settle": settle_result,
            "policy": "STEP62 : Refusés Value persistants en colonnes PostgreSQL; filtre durable cote<=1.80 / large / strict / danger; win/loss/void/pending conservés par l'historique moteur.",
            "serviceVersion": "step62-refuse-value-persistent-history",
        }
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "databaseStatus": "error",
            "table": store.TABLE,
            "filter": filter,
            "error": f"{type(exc).__name__}: {exc}",
            "items": [],
            "serviceVersion": "step62-refuse-value-persistent-history",
        }


@app.get("/sync/refuse-value/backfill")
def sync_refuse_value_backfill(
    limit: int = Query(50000, ge=1, le=100000),
    dry_run: bool = Query(False),
) -> Dict[str, Any]:
    """STEP62 : remplit les colonnes Refusés Value pour les anciennes lignes REFUSE."""
    store = PostgresPremiumStore()
    if not store.enabled:
        return {
            "status": "error",
            "databaseConfigured": False,
            "databaseStatus": "not_configured",
            "table": store.TABLE,
            "error": "DATABASE_URL absente dans le service web.",
            "serviceVersion": "step62-refuse-value-persistent-history",
        }
    try:
        result = store.backfill_refuse_value_columns(limit=limit, dry_run=dry_run)
        result["databaseConfigured"] = True
        result["databaseStatus"] = "ok"
        result["table"] = store.TABLE
        result["serviceVersion"] = "step62-refuse-value-persistent-history"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "databaseStatus": "error",
            "table": store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step62-refuse-value-persistent-history",
        }



@app.get("/sync/history/form-value")
def sync_history_form_value(
    category: str = Query("all"),
    limit: int = Query(50000, ge=1, le=100000),
) -> Dict[str, Any]:
    """STEP34 : rapport Form/Value actif multi-années par catégorie."""
    store = PostgresPremiumStore()
    cat = _normalize_history_category_for_api(category) if str(category).strip().lower() not in {"all", "tout", "*"} else "ALL"
    if not store.enabled:
        return {
            "status": "error",
            "databaseConfigured": False,
            "databaseStatus": "not_configured",
            "table": store.TABLE,
            "category": cat,
            "error": "DATABASE_URL absente dans le service web.",
            "serviceVersion": "step49-clay-veto-restored",
        }
    try:
        report = store.form_value_report(category=cat, limit=limit)
        report["databaseConfigured"] = True
        report["databaseStatus"] = "ok"
        report["table"] = store.TABLE
        report["serviceVersion"] = "step49-clay-veto-restored"
        return report
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "databaseStatus": "error",
            "table": store.TABLE,
            "category": cat,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step49-clay-veto-restored",
        }


@app.get("/sync/history/list")
def sync_history_list(
    category: str = Query("premium"),
    limit: int = Query(20000, ge=1, le=50000),
    auto_settle: bool = Query(False),
    settle_days_back: int = Query(0, ge=0, le=36500),
) -> Dict[str, Any]:
    """Liste historique moteur par catégorie : premium/proche/veto/refuse."""
    cat = _normalize_history_category_for_api(category)

    store = PostgresPremiumStore()
    if not store.enabled:
        return {
            "status": "error",
            "databaseConfigured": False,
            "databaseStatus": "not_configured",
            "table": store.TABLE,
            "category": cat,
            "error": "DATABASE_URL absente dans le service web.",
            "items": [],
            "rows": [],
            "serviceVersion": "step49-clay-veto-restored",
        }

    try:
        return _history_list_payload(store, cat, limit, auto_settle, settle_days_back)
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "databaseStatus": "error",
            "table": store.TABLE,
            "category": cat,
            "error": f"{type(exc).__name__}: {exc}",
            "items": [],
            "rows": [],
            "serviceVersion": "step49-clay-veto-restored",
        }


@app.get("/sync/premium/list")
def sync_premium_list(
    limit: int = Query(20000, ge=1, le=50000),
    auto_settle: bool = Query(False),
    settle_days_back: int = Query(0, ge=0, le=36500),
) -> Dict[str, Any]:
    """Compatibilité Unity : historique PREMIUM uniquement."""
    store = PostgresPremiumStore()
    if not store.enabled:
        return {
            "status": "error",
            "databaseConfigured": False,
            "databaseStatus": "not_configured",
            "table": store.TABLE,
            "category": "PREMIUM",
            "error": "DATABASE_URL absente dans le service web.",
            "items": [],
            "rows": [],
            "serviceVersion": "step49-clay-veto-restored",
        }
    try:
        return _history_list_payload(store, "PREMIUM", limit, auto_settle, settle_days_back)
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "databaseStatus": "error",
            "table": store.TABLE,
            "category": "PREMIUM",
            "error": f"{type(exc).__name__}: {exc}",
            "items": [],
            "rows": [],
            "serviceVersion": "step49-clay-veto-restored",
        }


@app.get("/sync/premium/settle")
def sync_premium_settle(day: str = Query("today"), dry_run: bool = Query(False)) -> Dict[str, Any]:
    """Règle les lignes Premium/Proche pending d'une date précise via API-Tennis.

    Ne crée aucun nouveau pick. Change uniquement les lignes existantes :
    pending -> win/loss.
    """
    target_day = normalize_day(day)
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.settle_day_from_api_tennis(target_day, dry_run=dry_run)
    result["serviceVersion"] = "step49-clay-veto-restored"
    return result


@app.get("/sync/premium/settle-pending")
def sync_premium_settle_pending(days_back: int = Query(7, ge=1, le=60), dry_run: bool = Query(False)) -> Dict[str, Any]:
    """Règle automatiquement les pending récents via API-Tennis.

    C'est l'endpoint à utiliser pour un cron Railway ou un contrôle manuel après les matchs.
    """
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.settle_pending_recent(days_back=days_back, dry_run=dry_run, provider="api_tennis")
    result["serviceVersion"] = "step49-clay-veto-restored"
    return result



@app.get("/sync/history/settle")
def sync_history_settle(day: str = Query("today"), dry_run: bool = Query(False)) -> Dict[str, Any]:
    target_day = normalize_day(day)
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.settle_day_from_api_tennis(target_day, dry_run=dry_run)
    result["serviceVersion"] = "step49-clay-veto-restored"
    return result


@app.get("/sync/history/settle-pending")
def sync_history_settle_pending(days_back: int = Query(7, ge=1, le=60), dry_run: bool = Query(False)) -> Dict[str, Any]:
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.settle_pending_recent(days_back=days_back, dry_run=dry_run, provider="api_tennis")
    result["serviceVersion"] = "step49-clay-veto-restored"
    return result




@app.get("/sync/history/reconcile-all")
def sync_history_reconcile_all(
    days_back: int = Query(0, ge=0, le=36500),
    dry_run: bool = Query(False),
) -> Dict[str, Any]:
    """STEP40 : remet les 4 historiques dans un état cohérent.

    - Règle tous les pending via API-Tennis, toutes catégories confondues.
    - Corrige les anciens IDs legacy par fallback noms joueurs.
    - Marque void/remboursé les lignes remplacées/forfait avant match.
    - Nettoie les doublons par catégorie sans supprimer les données valides.
    """
    store = PostgresPremiumStore()
    if not store.enabled:
        return {
            "status": "error",
            "databaseConfigured": False,
            "databaseStatus": "not_configured",
            "table": store.TABLE,
            "error": "DATABASE_URL absente dans le service web.",
            "serviceVersion": "step49-clay-veto-restored",
        }

    syncer = PremiumHistorySyncer(store=store)
    settle_result = syncer.settle_pending_recent(days_back=days_back, dry_run=dry_run, provider="api_tennis")

    cleanup: Dict[str, Any] = {}
    if not dry_run:
        for cat in ["PREMIUM", "PROCHE", "VETO", "REFUSE"]:
            try:
                cleanup[cat] = store.cleanup_duplicate_events(category=cat)
            except Exception as exc:
                cleanup[cat] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    else:
        cleanup = {cat: {"status": "dry_run_skipped"} for cat in ["PREMIUM", "PROCHE", "VETO", "REFUSE"]}

    categories: Dict[str, Any] = {}
    for cat in ["PREMIUM", "PROCHE", "VETO", "REFUSE"]:
        try:
            summary = store.summary(category=cat)
            categories[cat] = {
                "counts": store.counts(category=cat),
                "summary": summary.get("summary", {}),
                "chartDays": summary.get("chart", {}).get("days", []),
            }
        except Exception as exc:
            categories[cat] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    return {
        "status": "ok" if settle_result.get("status") in {"ok", "skipped"} else "partial",
        "provider": "api_tennis",
        "dryRun": dry_run,
        "daysBack": days_back,
        "settle": settle_result,
        "cleanup": cleanup,
        "categories": categories,
        "storage": {
            "mode": "postgres",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "status": store.status(),
        },
        "policy": "STEP40 : reconciliation globale Premium/Proche/Veto/Refusés; aucun transfert de pick quand adversaire remplacé, l'ancienne ligne passe en void/remboursé.",
        "serviceVersion": "step49-clay-veto-restored",
    }


@app.get("/sync/history/repair-dellien-royer")
def sync_history_repair_dellien_royer(confirm: str = Query("")) -> Dict[str, Any]:
    """Correctif ponctuel : Dellien/Royer 24/05/2026.

    Le nettoyage STEP29 avait gardé le mauvais pick Dellien.
    La ligne correcte à conserver est Royer gagnant.
    """
    store = PostgresPremiumStore()
    if str(confirm).strip() != "YES":
        return {
            "status": "refused",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "message": "Réparation refusée : ajoute confirm=YES.",
            "example": "/sync/history/repair-dellien-royer?confirm=YES",
            "serviceVersion": "step49-clay-veto-restored",
        }
    try:
        result = store.repair_dellien_royer_refuse()
        result["serviceVersion"] = "step49-clay-veto-restored"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step49-clay-veto-restored",
        }


@app.get("/sync/history/repair-shelton-merida")
def sync_history_repair_shelton_merida(confirm: str = Query("")) -> Dict[str, Any]:
    """Correctif ponctuel : Daniel Merida / Ben Shelton 25/05/2026.

    API-Tennis n'a pas relié automatiquement l'ancienne ligne legacy
    sr:sport_event:71642350. Le match réel a été joué et Shelton a gagné
    en trois sets. On règle donc uniquement cette ligne pending en win/loss
    selon le pick stocké, sans toucher aux autres catégories.
    """
    store = PostgresPremiumStore()
    if str(confirm).strip() != "YES":
        return {
            "status": "refused",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "message": "Réparation refusée : ajoute confirm=YES.",
            "example": "/sync/history/repair-shelton-merida?confirm=YES",
            "serviceVersion": "step49-clay-veto-restored",
        }
    try:
        result = store.repair_shelton_merida_20260525()
        result["serviceVersion"] = "step49-clay-veto-restored"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step49-clay-veto-restored",
        }


@app.get("/sync/history/repair-wawrinka-fils-dejong")
def sync_history_repair_wawrinka_fils_dejong(confirm: str = Query("")) -> Dict[str, Any]:
    """Correctif ponctuel : Wawrinka / Arthur Fils remplacé par De Jong le 25/05/2026.

    La ligne historique a été réglée comme Wawrinka vs De Jong en perte.
    Mais si le pari/pick original était Wawrinka vs Arthur Fils, le forfait de
    Fils avant match rend le marché remboursé. On passe uniquement cette ligne
    en void, sans transférer le pick sur De Jong.
    """
    store = PostgresPremiumStore()
    if str(confirm).strip() != "YES":
        return {
            "status": "refused",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "message": "Réparation refusée : ajoute confirm=YES.",
            "example": "/sync/history/repair-wawrinka-fils-dejong?confirm=YES",
            "serviceVersion": "step49-clay-veto-restored",
        }
    try:
        result = store.repair_wawrinka_fils_dejong_20260525()
        result["serviceVersion"] = "step49-clay-veto-restored"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step49-clay-veto-restored",
        }


@app.get("/sync/history/repair-van-assche-kypson-gaubas")
def sync_history_repair_van_assche_kypson_gaubas(confirm: str = Query("")) -> Dict[str, Any]:
    """Correctif ponctuel : Van Assche / Kypson remplacé par Gaubas le 25/05/2026.

    La ligne historique Van Assche vs Gaubas a été réglée comme une perte.
    Mais si le pick original venait du marché Van Assche vs Patrick Kypson,
    Kypson ayant déclaré forfait avant match, le marché initial doit être
    remboursé. On passe uniquement la ligne legacy Van Assche/Gaubas en void,
    sans transférer automatiquement le pick vers le nouveau match.
    """
    store = PostgresPremiumStore()
    if str(confirm).strip() != "YES":
        return {
            "status": "refused",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "message": "Réparation refusée : ajoute confirm=YES.",
            "example": "/sync/history/repair-van-assche-kypson-gaubas?confirm=YES",
            "serviceVersion": "step49-clay-veto-restored",
        }
    try:
        result = store.repair_van_assche_kypson_gaubas_20260525()
        result["serviceVersion"] = "step49-clay-veto-restored"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step49-clay-veto-restored",
        }


@app.get("/sync/history/reset")
def sync_history_reset(category: str = Query("premium"), confirm: str = Query("")) -> Dict[str, Any]:
    """Reset sécurisé d'une seule catégorie historique."""
    store = PostgresPremiumStore()
    cat = _normalize_history_category_for_api(category) if str(category).strip().lower() not in {"all", "tout"} else "ALL"
    if str(confirm).strip() != "YES":
        return {
            "status": "refused",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "category": cat,
            "message": "Reset refusé : ajoute confirm=YES.",
            "example": f"/sync/history/reset?category={cat.lower()}&confirm=YES",
            "serviceVersion": "step49-clay-veto-restored",
        }
    try:
        if cat == "ALL":
            result = store.reset_all()
        else:
            result = store.reset_category(cat)
        result["serviceVersion"] = "step49-clay-veto-restored"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "category": cat,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step49-clay-veto-restored",
        }


@app.get("/sync/premium/reset")
def sync_premium_reset(confirm: str = Query("")) -> Dict[str, Any]:
    """Reset sécurisé de l'historique PREMIUM uniquement."""
    store = PostgresPremiumStore()
    if str(confirm).strip() != "YES":
        return {
            "status": "refused",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "category": "PREMIUM",
            "message": "Reset refusé : ajoute ?confirm=YES pour vider l'historique PREMIUM uniquement.",
            "example": "/sync/premium/reset?confirm=YES",
            "serviceVersion": "step49-clay-veto-restored",
        }
    try:
        result = store.reset_category("PREMIUM")
        result["serviceVersion"] = "step49-clay-veto-restored"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "category": "PREMIUM",
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step49-clay-veto-restored",
        }


@app.get("/sync/premium/run")
def sync_premium_run(day: str = Query("today"), dry_run: bool = Query(False)) -> Dict[str, Any]:
    target_day = normalize_day(day)
    daily_result = daily(target_day, auto_history=False)
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.sync_daily_result(daily_result, target_day, dry_run=dry_run)
    result["serviceVersion"] = "step49-clay-veto-restored"
    return result


@app.get("/sync/daily-maintenance/run")
def sync_daily_maintenance_run(
    day: str = Query("today"),
    sync_results_day: str = Query("yesterday"),
    settle_days_back: int = Query(0, ge=0, le=36500),
    dry_run: bool = Query(False),
) -> Dict[str, Any]:
    """STEP48 : route quotidienne unique API-Tennis avec sync results2026 AVANT calcul du jour.

    Objectif opérationnel : ne plus dépendre de clics manuels ni de routes
    ponctuelles. Cette route peut être appelée par un cron Railway chaque jour.

    Elle fait, dans cet ordre :
    1) synchronise les résultats 2026 de la veille via API-Tennis ;
    2) force le reset du state moteur pour recharger data/2026.csv ;
    3) recharge les matchs ATP simples du jour via API-Tennis + cotes Flashscore ;
    4) écrit/actualise les historiques PREMIUM / PROCHE / VETO / REFUSE ;
    5) règle les anciens pending via API-Tennis uniquement ;
    6) nettoie les doublons par catégorie.

    Les routes de réparation STEP41/42/43 restent disponibles pour l'ancien
    historique 25/05, mais elles ne font pas partie du flux quotidien normal.
    """
    target_day = normalize_day(day)
    results_target_day = normalize_day(sync_results_day)
    store = PostgresPremiumStore()
    syncer = PremiumHistorySyncer(store=store)

    errors: List[str] = []
    out: Dict[str, Any] = {
        "status": "ok",
        "provider": "api_tennis",
        "step": "48",
        "serviceVersion": "step49-clay-veto-restored",
        "dryRun": dry_run,
        "targetDay": target_day,
        "resultsTargetDay": results_target_day,
        "settleDaysBack": settle_days_back,
        "apiTennisKeyConfigured": bool(os.environ.get("API_TENNIS_KEY", "").strip()),
        "databaseConfigured": store.enabled,
        "policy": "Flux quotidien unique : API-Tennis pour matchs/résultats/règlement, Flashscore pour cotes, aucun appel Sportradar.",
    }

    if not store.enabled:
        out.update({
            "status": "error",
            "errors": ["DATABASE_URL absente dans le service web."],
        })
        return out

    try:
        store.ensure_schema()
    except Exception as exc:
        out.update({
            "status": "error",
            "errors": [f"PostgreSQL error: {type(exc).__name__}: {exc}"],
        })
        return out

    # 0) STEP48 : synchroniser results2026 AVANT le calcul du jour.
    # Sinon le moteur peut analyser aujourd'hui avec un état Elo/Form qui ne
    # contient pas encore les résultats de la veille. Après l'export CSV, on
    # force le reset du cache moteur pour que /daily recharge 2026.csv.
    try:
        results_syncer = ApiTennisResults2026Syncer(base_dir=BASE_DIR)
        results2026 = results_syncer.sync_day(results_target_day, dry_run=dry_run)
        out["results2026"] = results2026

        reset_done = False
        reset_error = ""
        if not dry_run and results2026.get("status") == "ok":
            try:
                import motor as motor_module
                motor_module._STATE = None
                reset_done = True
            except Exception as exc:
                reset_error = f"{type(exc).__name__}: {exc}"
                errors.append(f"motor state reset failed after results2026: {reset_error}")
        out["motorStateResetAfterResults2026"] = reset_done
        if reset_error:
            out["motorStateResetError"] = reset_error

        if results2026.get("errors"):
            errors.extend([str(x) for x in results2026.get("errors") or []])
        if results2026.get("status") not in {"ok", "skipped"}:
            errors.append(f"results2026 status={results2026.get('status')}")
    except Exception as exc:
        errors.append(f"results2026 failed: {type(exc).__name__}: {exc}")
        out["results2026"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        out["motorStateResetAfterResults2026"] = False

    # 1) Build daily payload through the same public daily path, but without
    # auto_history to avoid double-writing; we write through sync_daily_result.
    try:
        daily_payload = daily(target_day, auto_history=False, provider="api_tennis")
        out["daily"] = {
            "status": daily_payload.get("status"),
            "provider": (daily_payload.get("daily") or {}).get("provider"),
            "providerRequested": (daily_payload.get("daily") or {}).get("providerRequested"),
            "sportradarForcedOff": (daily_payload.get("daily") or {}).get("sportradarForcedOff"),
            "payloadCount": (daily_payload.get("daily") or {}).get("payloadCount"),
            "summary": daily_payload.get("summary", {}),
            "odds": (daily_payload.get("daily") or {}).get("odds", {}),
            "formValueEngine": (daily_payload.get("daily") or {}).get("formValueEngine", {}),
        }
    except Exception as exc:
        daily_payload = {"status": "error", "daily": {}}
        errors.append(f"daily build failed: {type(exc).__name__}: {exc}")
        out["daily"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    # 2) Record all categorized engine outputs for target day.
    try:
        # STEP47 : robustesse cron.
        # /daily peut être considéré OK si le statut top-level est "ok" OU si
        # le payload contient bien un daily API-Tennis valide avec des lignes
        # analysées. Cela évite de bloquer l'écriture historique pour un simple
        # champ status manquant dans une réponse pourtant correcte.
        summary = daily_payload.get("summary") if isinstance(daily_payload, dict) else {}
        daily_meta = daily_payload.get("daily") if isinstance(daily_payload, dict) else {}
        daily_ok = (
            isinstance(daily_payload, dict)
            and (
                daily_payload.get("status") == "ok"
                or (
                    isinstance(daily_meta, dict)
                    and daily_meta.get("provider") == "api_tennis"
                    and int((summary or {}).get("validRows") or (summary or {}).get("totalRows") or 0) > 0
                    and int((summary or {}).get("errorRows") or 0) == 0
                )
            )
        )
        if daily_ok:
            daily_payload["status"] = "ok"
            history_sync = syncer.sync_daily_result(daily_payload, target_day, dry_run=dry_run)
        else:
            history_sync = {
                "status": "skipped",
                "reason": "daily_payload_not_ok",
                "dailyStatus": daily_payload.get("status") if isinstance(daily_payload, dict) else None,
                "provider": daily_meta.get("provider") if isinstance(daily_meta, dict) else None,
                "summary": summary if isinstance(summary, dict) else {},
            }
        out["historySync"] = history_sync
        if history_sync.get("errors"):
            errors.extend([str(x) for x in history_sync.get("errors") or []])
        if history_sync.get("status") not in {"ok", "skipped"}:
            errors.append(f"historySync status={history_sync.get('status')}")
    except Exception as exc:
        errors.append(f"history sync failed: {type(exc).__name__}: {exc}")
        out["historySync"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    # 3) Settle pending rows. days_back=0 means all dates containing pending rows.
    try:
        settle = syncer.settle_pending_recent(days_back=settle_days_back, dry_run=dry_run, provider="api_tennis")
        out["settle"] = settle
        if settle.get("errors"):
            errors.extend([str(x) for x in settle.get("errors") or []])
        if settle.get("status") not in {"ok", "skipped"}:
            errors.append(f"settle status={settle.get('status')}")
    except Exception as exc:
        errors.append(f"settle failed: {type(exc).__name__}: {exc}")
        out["settle"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    # 3B) Correctif legacy idempotent : ancienne ligne Merida/Shelton du 25/05.
    # Cette réparation ne touche qu'un row_id exact si encore pending, puis ne
    # modifie plus rien les jours suivants. Elle évite de garder un faux pending
    # permanent dans les tableaux historiques.
    try:
        repair = store.repair_shelton_merida_20260525()
        out["legacyRepairSheltonMerida"] = repair
        if repair.get("status") == "error":
            errors.append(f"legacy repair shelton/merida failed: {repair.get('error')}")
    except Exception as exc:
        errors.append(f"legacy repair shelton/merida failed: {type(exc).__name__}: {exc}")
        out["legacyRepairSheltonMerida"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    # 4) Clean duplicate rows per category. This is idempotent.
    cleanup: Dict[str, Any] = {}
    if dry_run:
        cleanup = {cat: {"status": "dry_run_skipped"} for cat in ["PREMIUM", "PROCHE", "VETO", "REFUSE"]}
    else:
        for cat in ["PREMIUM", "PROCHE", "VETO", "REFUSE"]:
            try:
                cleanup[cat] = store.cleanup_duplicate_events(category=cat)
            except Exception as exc:
                cleanup[cat] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                errors.append(f"cleanup {cat} failed: {type(exc).__name__}: {exc}")
    out["cleanup"] = cleanup

    # 5) STEP48 : results2026 a déjà été synchronisé avant /daily.
    # On ne le relance pas ici pour éviter des appels API-Tennis inutiles.

    # Final category snapshot for Unity / audit.
    categories: Dict[str, Any] = {}
    for cat in ["PREMIUM", "PROCHE", "VETO", "REFUSE"]:
        try:
            summary = store.summary(category=cat)
            categories[cat] = {
                "counts": store.counts(category=cat),
                "summary": summary.get("summary", {}),
                "chartDays": summary.get("chart", {}).get("days", []),
            }
        except Exception as exc:
            categories[cat] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            errors.append(f"summary {cat} failed: {type(exc).__name__}: {exc}")
    out["categories"] = categories
    out["storage"] = {
        "mode": "postgres",
        "databaseConfigured": store.enabled,
        "table": store.TABLE,
        "status": store.status(),
    }

    out["errors"] = errors
    out["status"] = "ok" if not errors else "partial"
    return out


@app.get("/v3/status")
def v3_status() -> Dict[str, Any]:
    """Tennis Motor V3.1.1 — qualification detection + prioritized shadow rules status."""
    history_store = PostgresPremiumStore()
    v3_store = V3LearningMemoryStore()
    v3_status_payload = v3_store.status()
    return {
        "status": "ok",
        "version": V3_VERSION,
        "mode": "learning_memory_plus_priority_shadow_rules",
        "databaseConfigured": history_store.enabled,
        "historyTable": history_store.TABLE,
        "v3": v3_status_payload,
        "endpoints": [
            "/v3/status",
            "/v3/report?category=all&limit=50000&min_settled=10",
            "/v3/memory/status",
            "/v3/learn/run?category=all&limit=50000&min_settled=10",
            "/v3/rules/list?status=shadow",
            "/v3/rules/refresh?category=all&limit=50000&min_settled=10",
            "/v3/shadow/daily?day=today&persist=false",
        ],
        "policy": "V3 apprend, corrige la qualification, priorise les règles shadow et les teste. Elle ne remplace jamais STEP56/STEP62 sans validation hors échantillon.",
    }


@app.get("/v3/report")
def v3_report(
    category: str = Query("all"),
    limit: int = Query(50000, ge=1, le=100000),
    min_settled: int = Query(10, ge=1, le=1000),
    odds_cutoff: float = Query(1.90, ge=1.01, le=100.0),
    dedupe: bool = Query(True),
    include_rows: bool = Query(False),
    source: str = Query("history", description="history ou memory"),
) -> Dict[str, Any]:
    """V3 report from raw history or from V3 learning memory."""
    history_store = PostgresPremiumStore()
    v3_store = V3LearningMemoryStore()
    if not history_store.enabled:
        return {
            "status": "error",
            "version": V3_VERSION,
            "databaseConfigured": False,
            "databaseStatus": "not_configured",
            "table": history_store.TABLE,
            "error": "DATABASE_URL absente dans le service web.",
        }

    cat_raw = (category or "all").strip().lower()
    category_map = {
        "all": "ALL", "tout": "ALL", "*": "ALL",
        "premium": "PREMIUM",
        "proche": "PROCHE", "proches": "PROCHE",
        "veto": "VETO",
        "refuse": "REFUSE", "refus": "REFUSE", "refuses": "REFUSE", "refusé": "REFUSE", "refusés": "REFUSE",
    }
    cat = category_map.get(cat_raw, "ALL")

    try:
        if (source or "history").strip().lower() == "memory":
            v3_store.ensure_schema()
            rows = v3_store.fetch_memory_rows(limit=limit, category=None if cat == "ALL" else cat)
            source_mode = "v3_memory"
        else:
            history_store.ensure_schema()
            rows = history_store.fetch_rows(limit=limit, category=None if cat == "ALL" else cat)
            source_mode = "history"
        report = build_v3_learning_report(
            rows,
            category=cat,
            min_settled=min_settled,
            odds_cutoff=odds_cutoff,
            dedupe=dedupe,
            include_rows=include_rows,
        )
        report["databaseConfigured"] = True
        report["databaseStatus"] = "ok"
        report["historyTable"] = history_store.TABLE
        report["v3MemoryTable"] = v3_store.MEMORY_TABLE
        report["sourceMode"] = source_mode
        report["sourceRows"] = len(rows)
        return report
    except Exception as exc:
        return {
            "status": "error",
            "version": V3_VERSION,
            "databaseConfigured": history_store.enabled,
            "databaseStatus": "error",
            "table": history_store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
        }


@app.get("/v3/memory/status")
def v3_memory_status() -> Dict[str, Any]:
    """Status for V3 persistent learning memory and shadow tables."""
    store = V3LearningMemoryStore()
    status_payload = store.status()
    status_payload["version"] = V3_VERSION
    return status_payload


@app.get("/v3/memory/sync")
def v3_memory_sync(
    category: str = Query("all"),
    limit: int = Query(50000, ge=1, le=100000),
    odds_cutoff: float = Query(1.90, ge=1.01, le=100.0),
) -> Dict[str, Any]:
    """Copy normalized history rows into the persistent V3 learning memory table."""
    history_store = PostgresPremiumStore()
    v3_store = V3LearningMemoryStore()
    if not history_store.enabled:
        return {"status": "error", "version": V3_VERSION, "error": "DATABASE_URL absente"}

    category_map = {
        "all": "ALL", "tout": "ALL", "*": "ALL",
        "premium": "PREMIUM", "proche": "PROCHE", "proches": "PROCHE", "veto": "VETO",
        "refuse": "REFUSE", "refus": "REFUSE", "refuses": "REFUSE", "refusé": "REFUSE", "refusés": "REFUSE",
    }
    cat = category_map.get((category or "all").strip().lower(), "ALL")
    try:
        history_store.ensure_schema()
        rows = history_store.fetch_rows(limit=limit, category=None if cat == "ALL" else cat)
        sync_info = v3_store.upsert_memory_rows(rows, odds_cutoff=odds_cutoff)
        return {
            "status": "ok",
            "version": V3_VERSION,
            "category": cat,
            "sourceRows": len(rows),
            "sync": sync_info,
            "v3": v3_store.status(),
        }
    except Exception as exc:
        return {"status": "error", "version": V3_VERSION, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/v3/rules/refresh")
def v3_rules_refresh(
    category: str = Query("all"),
    limit: int = Query(50000, ge=1, le=100000),
    min_settled: int = Query(10, ge=1, le=1000),
    odds_cutoff: float = Query(1.90, ge=1.01, le=100.0),
    max_rules: int = Query(50, ge=1, le=500),
    sync_memory: bool = Query(True),
) -> Dict[str, Any]:
    """Generate and persist automatic V3 shadow rules from historical learning segments."""
    history_store = PostgresPremiumStore()
    v3_store = V3LearningMemoryStore()
    if not history_store.enabled:
        return {"status": "error", "version": V3_VERSION, "error": "DATABASE_URL absente"}

    category_map = {
        "all": "ALL", "tout": "ALL", "*": "ALL",
        "premium": "PREMIUM", "proche": "PROCHE", "proches": "PROCHE", "veto": "VETO",
        "refuse": "REFUSE", "refus": "REFUSE", "refuses": "REFUSE", "refusé": "REFUSE", "refusés": "REFUSE",
    }
    cat = category_map.get((category or "all").strip().lower(), "ALL")
    try:
        history_store.ensure_schema()
        rows = history_store.fetch_rows(limit=limit, category=None if cat == "ALL" else cat)
        sync_info = None
        if sync_memory:
            sync_info = v3_store.upsert_memory_rows(rows, odds_cutoff=odds_cutoff)
        rules_payload = build_v3_rules_from_history(
            rows,
            category=cat,
            min_settled=min_settled,
            odds_cutoff=odds_cutoff,
            max_rules=max_rules,
        )
        write_info = v3_store.upsert_shadow_rules(rules_payload.get("rules", []))
        return {
            "status": "ok",
            "version": V3_VERSION,
            "category": cat,
            "sourceRows": len(rows),
            "memorySync": sync_info,
            "rulesGenerated": rules_payload.get("rulesCount", 0),
            "rulesWritten": write_info.get("rulesWritten", 0),
            "autoRulesDeprecatedBeforeRefresh": write_info.get("autoRulesDeprecatedBeforeRefresh", 0),
            "rules": rules_payload.get("rules", []),
            "learningSummary": rules_payload.get("learningReport", {}).get("summary", {}),
        }
    except Exception as exc:
        return {"status": "error", "version": V3_VERSION, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/v3/rules/list")
def v3_rules_list(
    status: str = Query("shadow"),
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    """List persisted V3 shadow rules."""
    store = V3LearningMemoryStore()
    try:
        rules = store.list_shadow_rules(status=status, limit=limit)
        return {"status": "ok", "version": V3_VERSION, "rulesCount": len(rules), "rules": rules}
    except Exception as exc:
        return {"status": "error", "version": V3_VERSION, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/v3/learn/run")
def v3_learn_run(
    category: str = Query("all"),
    limit: int = Query(50000, ge=1, le=100000),
    min_settled: int = Query(10, ge=1, le=1000),
    odds_cutoff: float = Query(1.90, ge=1.01, le=100.0),
    max_rules: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """One-click V3 learning run: sync memory + refresh automatic shadow rules."""
    memory = v3_memory_sync(category=category, limit=limit, odds_cutoff=odds_cutoff)
    rules = v3_rules_refresh(category=category, limit=limit, min_settled=min_settled, odds_cutoff=odds_cutoff, max_rules=max_rules, sync_memory=False)
    return {
        "status": "ok" if memory.get("status") == "ok" and rules.get("status") == "ok" else "partial",
        "version": V3_VERSION,
        "memory": memory,
        "rules": rules,
        "policy": "Les règles générées sont en shadow mode. Elles apprennent et testent; elles ne changent pas encore les décisions officielles.",
    }


@app.get("/v3/shadow/daily")
def v3_shadow_daily(
    day: str = Query("today"),
    provider: str = Query("api_tennis"),
    status: str = Query("shadow"),
    persist: bool = Query(False),
    odds_cutoff: float = Query(1.90, ge=1.01, le=100.0),
) -> Dict[str, Any]:
    """Run V3 shadow rules against today's/tomorrow's current daily matches.

    This endpoint reads the official /daily payload with auto_history=false and adds V3
    shadow decisions. It does not alter official STEP56/STEP62 decisions.
    """
    v3_store = V3LearningMemoryStore()
    try:
        daily_payload = daily(day=day, auto_history=False, provider=provider)
        matches = daily_payload.get("matches", []) or []
        rules = v3_store.list_shadow_rules(status=status, limit=500)
        shadow = evaluate_shadow_matches(matches, rules, day=normalize_day(day), odds_cutoff=odds_cutoff)
        write_info = None
        if persist:
            write_info = v3_store.persist_shadow_decisions(normalize_day(day), shadow.get("decisions", []))
        return {
            "status": "ok",
            "version": V3_VERSION,
            "day": normalize_day(day),
            "provider": provider,
            "officialDailyStatus": daily_payload.get("status"),
            "rulesLoaded": len(rules),
            "persisted": write_info,
            "shadow": shadow,
            "policy": "Shadow seulement : aucune décision officielle n'est remplacée.",
        }
    except Exception as exc:
        return {"status": "error", "version": V3_VERSION, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
