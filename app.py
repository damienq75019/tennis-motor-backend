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
from sportradar_client import SportradarClient
from sportradar_daily_builder import SportradarDailyBuilder
from sportradar_2026_results_sync import Results2026Syncer
from postgres_premium_store import PostgresPremiumStore
from sportradar_premium_sync import PremiumHistorySyncer
from flashscore_odds import FlashscoreOddsProvider


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
AUDIT_DIR = OUTPUT_DIR / "audits"
PAYLOAD_DIR = OUTPUT_DIR / "payloads"

# Règle utilisateur verrouillée : Jannik Sinner reste exclu de l'analyse.
EXCLUDED_ANALYSIS_PLAYERS = ["Jannik Sinner"]

app = FastAPI(title="Tennis Motor Backend Clean", version="step2.7.3-flashscore-name-abbrev-fix")
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
    prediction["contextSource"] = "clean_step1_manual_or_future_sportradar"
    prediction["contextOrientation"] = orientation
    prediction["contextPlayerATournamentWins"] = display_a_wins
    prediction["contextPlayerBTournamentWins"] = display_b_wins

    # Marqueurs de prudence pour la future intégration Sportradar.
    if "player_b_qualifier_confidence" in source_match:
        prediction["player_b_qualifier_confidence"] = source_match.get("player_b_qualifier_confidence")
    if "player_b_qualifier_source" in source_match:
        prediction["player_b_qualifier_source"] = source_match.get("player_b_qualifier_source")

    # Métadonnées Sportradar conservées pour l'historique Premium et le règlement officiel.
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
            "version": "Bayesian Shrinkage",
            "historyYears": list(HISTORY_YEARS),
            "historyRowsLoaded": history_rows_loaded,
            "premiumFormula": "Bayesian shrinkage blend of SWE, ATP, Rank, Form5, Form10, SurfaceForm5, Dominance",
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


def _is_placeholder_match(match: Dict[str, Any]) -> bool:
    def is_placeholder_name(value: Any) -> bool:
        name = str(value or "").strip()
        if not name:
            return True
        return bool(re.fullmatch(r"(?i)(qf|sf|pf|qualifier|winner|loser|bye)\s*\d*", name))

    player_a = _get_first_existing(match, ["playerA", "player_a"], "")
    player_b = _get_first_existing(match, ["playerB", "player_b"], "")
    return is_placeholder_name(player_a) or is_placeholder_name(player_b)


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
    matches, not_analyzed_matches = _split_analyzable_matches(matches)

    if not matches and not not_analyzed_matches:
        response = _empty_response(
            status="empty_payload_after_exclusion" if excluded_removed else "empty_payload",
            message="Aucun match exploitable après exclusion joueur." if excluded_removed else "Aucun match exploitable dans le payload.",
        )
        response["daily"]["excludedMatches"] = len(excluded_removed)
        response["daily"]["excludedSample"] = excluded_removed[:10]
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
            "version": "Bayesian Shrinkage",
            "historyYears": list(HISTORY_YEARS),
            "historyRowsLoaded": state["history_rows_loaded"],
            "premiumFormula": "Bayesian shrinkage blend of SWE, ATP, Rank, Form5, Form10, SurfaceForm5, Dominance",
            "threshold": "> 0.80",
            "orientationMode": "double_side_pairwise_best_premium",
        },
        "daily": {
            "doubleSideStatus": "ok",
            "doubleSideMode": "pairwise_best_premium_no_zip_after_sort",
            "doubleSideMatches": len(final_matches),
            "doubleSideReversedChosen": reversed_chosen,
            "contextPropagation": "clean_step2_5_preserved",
            "excludedPlayers": _excluded_analysis_names(),
            "excludedMatches": len(excluded_removed),
            "excludedSample": excluded_removed[:10],
            "notAnalyzedMatches": len(not_analyzed_matches),
            "notAnalyzedReasons": _rebuild_not_analyzed_reasons(not_analyzed_matches),
            "originalPayloadMatches": original_match_count,
        },
    }


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "Tennis Motor Backend Clean",
        "version": "step2.7.3-flashscore-name-abbrev-fix",
        "message": "Backend propre étape 2.7.3 : Sportradar + PostgreSQL + cotes Flashscore avec date target day corrigée, affichage uniquement.",
        "endpoints": ["/health", "/calculate", "/predictions", "/state", "/history", "/daily", "/odds/status", "/sync/results2026/status", "/sync/results2026/run", "/sync/results2026/postgres/status", "/sync/results2026/postgres/export", "/sync/premium/status", "/sync/premium/run"],
        "excludedAnalysisPlayers": _excluded_analysis_names(),
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    state = get_state()
    return {
        "status": "ok",
        "service": "Tennis Motor Backend Clean",
        "version": "step2.7.3-flashscore-name-abbrev-fix",
        "historyYears": list(HISTORY_YEARS),
        "historyRowsLoaded": state.get("history_rows_loaded", 0),
        "excludedAnalysisPlayers": _excluded_analysis_names(),
        "dailyProvider": "sportradar",
        "oddsProvider": "flashscore",
        "oddsUsage": "display_only_not_used_by_engine",
        "results2026Sync": "enabled",
        "results2026Storage": "postgres" if os.environ.get("DATABASE_URL", "").strip() else "csv",
        "premiumHistoryStorage": "postgres" if os.environ.get("DATABASE_URL", "").strip() else "unavailable",
        "databaseUrlConfigured": bool(os.environ.get("DATABASE_URL", "").strip()),
        "sportradarApiKeyConfigured": bool(os.environ.get("SPORTRADAR_API_KEY", "").strip()),
        "sportradarAccessLevel": os.environ.get("SPORTRADAR_ACCESS_LEVEL", "trial"),
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
def daily(day: str = Query("today")) -> Dict[str, Any]:
    target_day = normalize_day(day)

    builder = SportradarDailyBuilder(audit_dir=AUDIT_DIR)
    built = builder.build_matches_for_day(target_day)

    if built.get("status") != "ok":
        response = _empty_response(
            status="sportradar_error",
            message=str(built.get("error") or "Erreur Sportradar inconnue."),
            target_day=target_day,
        )
        response["daily"].update({
            "provider": "sportradar",
            "step": "2.5",
            "targetDay": target_day,
            "audit": built.get("audit", {}),
            "apiKeyConfigured": bool(os.environ.get("SPORTRADAR_API_KEY", "").strip()),
        })
        return response

    source_matches = built.get("matches", [])
    response = calculate_from_matches(source_matches)

    # Step 2.7.2 : cotes Flashscore uniquement pour affichage Unity.
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
        "provider": "sportradar",
        "step": "2.7.2",
        "targetDay": target_day,
        "payloadCount": len(source_matches),
        "audit": built.get("audit", {}),
        "manualReviewPolicy": "points ATP absents ou à 0 = non analysé; tournament_wins = matchs terminés strictement avant le match courant et gagnés par le joueur; placeholders à vérifier; qualifié B non fiable reste à vérifier; aucune donnée n'est inventée.",
        "oddsPolicy": "Flashscore odds are display-only and never used by the Tennis Motor decision.",
    })
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
        "serviceVersion": "step2.7.3-flashscore-name-abbrev-fix",
    }


@app.get("/sportradar/status")
def sportradar_status() -> Dict[str, Any]:
    client = SportradarClient()
    return {
        "status": "ok",
        "provider": "sportradar",
        "apiKeyConfigured": client.enabled,
        "accessLevel": client.config.access_level,
        "language": client.config.language,
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
            summary = store.summary()
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
    syncer = Results2026Syncer(client=SportradarClient(), base_dir=BASE_DIR)
    status = syncer.status()
    status["serviceVersion"] = "step2.7.3-flashscore-name-abbrev-fix"
    return status


@app.get("/sync/results2026/postgres/status")
def sync_results2026_postgres_status() -> Dict[str, Any]:
    syncer = Results2026Syncer(client=SportradarClient(), base_dir=BASE_DIR)
    status = syncer.postgres_status()
    status["serviceVersion"] = "step2.7.3-flashscore-name-abbrev-fix"
    return status


@app.get("/sync/results2026/postgres/export")
def sync_results2026_postgres_export() -> Dict[str, Any]:
    syncer = Results2026Syncer(client=SportradarClient(), base_dir=BASE_DIR)
    result = syncer.export_postgres_to_csv()
    result["serviceVersion"] = "step2.7.3-flashscore-name-abbrev-fix"

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
    syncer = Results2026Syncer(client=SportradarClient(), base_dir=BASE_DIR)
    result = syncer.sync_day(target_day, dry_run=dry_run)
    result["serviceVersion"] = "step2.7.3-flashscore-name-abbrev-fix"

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
    status["serviceVersion"] = "step2.7.3-flashscore-name-abbrev-fix"
    return status


@app.get("/sync/premium/run")
def sync_premium_run(day: str = Query("today"), dry_run: bool = Query(False)) -> Dict[str, Any]:
    target_day = normalize_day(day)
    daily_result = daily(target_day)
    syncer = PremiumHistorySyncer(store=PostgresPremiumStore())
    result = syncer.sync_daily_result(daily_result, target_day, dry_run=dry_run)
    result["serviceVersion"] = "step2.7.3-flashscore-name-abbrev-fix"
    return result


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
