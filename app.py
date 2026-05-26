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


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
AUDIT_DIR = OUTPUT_DIR / "audits"
PAYLOAD_DIR = OUTPUT_DIR / "payloads"

# Règle utilisateur verrouillée : Jannik Sinner reste exclu de l'analyse.
EXCLUDED_ANALYSIS_PLAYERS = ["Jannik Sinner"]

app = FastAPI(title="Tennis Motor Backend Clean", version="step47-daily-api-tennis-maintenance-fix")
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
        if premium_pct >= 80.0 and not veto:
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
    final_matches: List[Dict[str, Any]] = []
    reversed_chosen = 0

    for match in matches:
        original_prediction = calculate_match_prediction(match, state)
        reversed_prediction = calculate_match_prediction(_reverse_match_for_engine(match), state)

        original_score = _premium_score(original_prediction)
        reversed_score = _premium_score(reversed_prediction)

        if reversed_score > original_score:
            chosen = dict(reversed_prediction)
            orientation = "reversed"
            reversed_chosen += 1
        else:
            chosen = dict(original_prediction)
            orientation = "original"

        chosen = _copy_daily_context_to_prediction(match, chosen, orientation)

        source_player_a = str(match.get("playerA") or match.get("player_a") or "")
        source_player_b = str(match.get("playerB") or match.get("player_b") or "")
        chosen["engineOrientation"] = orientation
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

    return {
        "matches": all_matches,
        "summary": _rebuild_summary_from_matches(all_matches),
        "engine": {
            "name": "Tennis Motor V7",
            "version": "Bayesian Shrinkage + Real History Signals",
            "historyYears": list(HISTORY_YEARS),
            "historyRowsLoaded": state["history_rows_loaded"],
            "premiumFormula": "Bayesian shrinkage blend of SWE, ATP, Rank, Form5, Form10, SurfaceForm5, Dominance; API-Tennis names resolved to historical keys",
            "threshold": "> 0.80",
            "orientationMode": "double_side_pairwise_best_premium",
        },
        "daily": {
            "doubleSideStatus": "ok",
            "doubleSideMode": "pairwise_best_premium_no_zip_after_sort",
            "doubleSideMatches": len(final_matches),
            "doubleSideReversedChosen": reversed_chosen,
            "contextPropagation": "clean_step47-daily-api-tennis-maintenance-fix_11_real_history_signals_placeholder_filter_qualifier_audit_only",
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
            "version": "step47-daily-api-tennis-maintenance-fix",
        }
        return

    store = PostgresPremiumStore()
    if not store.enabled:
        response["daily"]["formValueEngine"] = {
            "status": "skipped",
            "reason": "database_not_configured",
            "version": "step47-daily-api-tennis-maintenance-fix",
        }
        return

    try:
        report = store.form_value_report(category="ALL", limit=50000)
    except Exception as exc:
        response["daily"]["formValueEngine"] = {
            "status": "error",
            "version": "step47-daily-api-tennis-maintenance-fix",
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
        match["formValueEngineVersion"] = "step47-daily-api-tennis-maintenance-fix"
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
        "version": "step47-daily-api-tennis-maintenance-fix",
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
        "version": "step47-daily-api-tennis-maintenance-fix",
        "message": "Backend STEP47 : API-Tennis uniquement. Aucun appel Sportradar en production.",
        "endpoints": ["/health", "/calculate", "/predictions", "/state", "/history", "/daily", "/api-tennis/status", "/odds/status", "/sync/results2026/status", "/sync/results2026/run", "/sync/results2026/postgres/status", "/sync/results2026/postgres/export", "/sync/premium/status", "/sync/premium/list", "/sync/premium/reset", "/sync/premium/run", "/sync/premium/settle", "/sync/premium/settle-pending", "/sync/history/form-value", "/sync/history/list", "/sync/history/reset", "/sync/history/repair-dellien-royer", "/sync/history/repair-shelton-merida", "/sync/history/repair-wawrinka-fils-dejong", "/sync/history/repair-van-assche-kypson-gaubas", "/sync/history/settle", "/sync/history/settle-pending", "/sync/daily-maintenance/run"],
        "excludedAnalysisPlayers": _excluded_analysis_names(),
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    state = get_state()
    return {
        "status": "ok",
        "service": "Tennis Motor Backend Clean",
        "version": "step47-daily-api-tennis-maintenance-fix",
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
        "qualifierDetector": "api_tennis_audit_only",
        "qualifierDetectorActivation": "audit_only_no_engine_veto",
        "placeholderFilter": "api_tennis_atp_singles_only",
        "motorSignals": "real_history_name_resolution_plus_score_dominance_fallback",
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
            "step": "47",
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
        "step": "47",
        "targetDay": target_day,
        "payloadCount": len(source_matches),
        "audit": built.get("audit", {}),
        "manualReviewPolicy": "points ATP absents ou à 0 = non analysé; API-Tennis fournit les matchs ATP simples via event_type_key=265; noms API-Tennis enrichis par get_standings ATP quand possible; aucune donnée n'est inventée.",
        "oddsPolicy": "Flashscore odds are display-only for the original core; STEP34/44 formValue uses odds against historical win-rate/ROI.",
        "apiTennisProviderActive": provider_name == "api_tennis",
        "apiTennisKeyConfigured": bool(os.environ.get("API_TENNIS_KEY", "").strip()),
    })

    # STEP34 : couche Form/Historique active, après enrichissement des cotes.
    _apply_form_value_engine(response)

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
        "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
    }


@app.get("/sportradar/status")
def sportradar_status() -> Dict[str, Any]:
    return {
        "status": "disabled",
        "provider": "api_tennis",
        "sportradarDisabled": True,
        "apiTennisOnly": True,
        "message": "STEP47 : Sportradar est désactivé. Utilise /api-tennis/status et /daily avec API-Tennis.",
        "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
    status["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
    return status


@app.get("/sync/results2026/postgres/status")
def sync_results2026_postgres_status() -> Dict[str, Any]:
    syncer = ApiTennisResults2026Syncer(base_dir=BASE_DIR)
    status = syncer.postgres_status()
    status["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
    return status


@app.get("/sync/results2026/postgres/export")
def sync_results2026_postgres_export() -> Dict[str, Any]:
    syncer = ApiTennisResults2026Syncer(base_dir=BASE_DIR)
    result = syncer.export_postgres_to_csv()
    result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"

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
    result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"

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
    status["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
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
        "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
        }
    try:
        report = store.form_value_report(category=cat, limit=limit)
        report["databaseConfigured"] = True
        report["databaseStatus"] = "ok"
        report["table"] = store.TABLE
        report["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
        return report
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "databaseStatus": "error",
            "table": store.TABLE,
            "category": cat,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
    result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
    return result


@app.get("/sync/premium/settle-pending")
def sync_premium_settle_pending(days_back: int = Query(7, ge=1, le=60), dry_run: bool = Query(False)) -> Dict[str, Any]:
    """Règle automatiquement les pending récents via API-Tennis.

    C'est l'endpoint à utiliser pour un cron Railway ou un contrôle manuel après les matchs.
    """
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.settle_pending_recent(days_back=days_back, dry_run=dry_run, provider="api_tennis")
    result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
    return result



@app.get("/sync/history/settle")
def sync_history_settle(day: str = Query("today"), dry_run: bool = Query(False)) -> Dict[str, Any]:
    target_day = normalize_day(day)
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.settle_day_from_api_tennis(target_day, dry_run=dry_run)
    result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
    return result


@app.get("/sync/history/settle-pending")
def sync_history_settle_pending(days_back: int = Query(7, ge=1, le=60), dry_run: bool = Query(False)) -> Dict[str, Any]:
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.settle_pending_recent(days_back=days_back, dry_run=dry_run, provider="api_tennis")
    result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
        "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
        }
    try:
        result = store.repair_dellien_royer_refuse()
        result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
        }
    try:
        result = store.repair_shelton_merida_20260525()
        result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
        }
    try:
        result = store.repair_wawrinka_fils_dejong_20260525()
        result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
        }
    try:
        result = store.repair_van_assche_kypson_gaubas_20260525()
        result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
        }
    try:
        if cat == "ALL":
            result = store.reset_all()
        else:
            result = store.reset_category(cat)
        result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "category": cat,
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
        }
    try:
        result = store.reset_category("PREMIUM")
        result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
        return result
    except Exception as exc:
        return {
            "status": "error",
            "databaseConfigured": store.enabled,
            "table": store.TABLE,
            "category": "PREMIUM",
            "error": f"{type(exc).__name__}: {exc}",
            "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
        }


@app.get("/sync/premium/run")
def sync_premium_run(day: str = Query("today"), dry_run: bool = Query(False)) -> Dict[str, Any]:
    target_day = normalize_day(day)
    daily_result = daily(target_day, auto_history=False)
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.sync_daily_result(daily_result, target_day, dry_run=dry_run)
    result["serviceVersion"] = "step47-daily-api-tennis-maintenance-fix"
    return result


@app.get("/sync/daily-maintenance/run")
def sync_daily_maintenance_run(
    day: str = Query("today"),
    sync_results_day: str = Query("yesterday"),
    settle_days_back: int = Query(0, ge=0, le=36500),
    dry_run: bool = Query(False),
) -> Dict[str, Any]:
    """STEP47 : route quotidienne unique API-Tennis.

    Objectif opérationnel : ne plus dépendre de clics manuels ni de routes
    ponctuelles. Cette route peut être appelée par un cron Railway chaque jour.

    Elle fait, dans cet ordre :
    1) recharge les matchs ATP simples du jour via API-Tennis + cotes Flashscore ;
    2) écrit/actualise les historiques PREMIUM / PROCHE / VETO / REFUSE ;
    3) règle les anciens pending via API-Tennis uniquement ;
    4) nettoie les doublons par catégorie ;
    5) synchronise les résultats 2026 de la veille via API-Tennis.

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
        "step": "47",
        "serviceVersion": "step47-daily-api-tennis-maintenance-fix",
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

    # 5) Sync results2026 for the requested result day, usually yesterday.
    try:
        results_syncer = ApiTennisResults2026Syncer(base_dir=BASE_DIR)
        results2026 = results_syncer.sync_day(results_target_day, dry_run=dry_run)
        out["results2026"] = results2026
        if results2026.get("errors"):
            errors.extend([str(x) for x in results2026.get("errors") or []])
        if results2026.get("status") not in {"ok", "skipped"}:
            errors.append(f"results2026 status={results2026.get('status')}")
    except Exception as exc:
        errors.append(f"results2026 failed: {type(exc).__name__}: {exc}")
        out["results2026"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

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


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
