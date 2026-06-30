from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Response


V5_COTE250_VERSION = "STEP68_V5_COTE250_HORS_GRAND_CHELEM_2026-06-30"
V5_RULE_ID = "V5_NO_GRAND_SLAM_ODD_GT_250"
V5_ODD_MIN_EXCLUSIVE = 2.50
V5_ALLOWED_CATEGORIES = {"PREMIUM", "PROCHE", "REFUSE"}
V5_GRAND_SLAM_KEYWORDS = (
    "wimbledon",
    "french open",
    "roland garros",
    "australian open",
    "us open",
    "u.s. open",
    "grand slam",
)

router = APIRouter()


def _database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL absente : V5 Postgres indisponible.")
    return url


def _connect():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"psycopg indisponible: {type(exc).__name__}: {exc}") from exc
    return psycopg.connect(_database_url(), connect_timeout=10, row_factory=dict_row)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _json_response(payload: Any) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False, default=_json_default),
        media_type="application/json; charset=utf-8",
    )


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _upper(value: Any) -> str:
    return _s(value).upper()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def _load_json(value: Any, default: Any = None) -> Any:
    if default is None:
        default = {}
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _paris_today() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:
        return date.today()


def _normalize_day(day: str) -> str:
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


def _check_export_token(token: str) -> None:
    expected = (os.environ.get("TENNIS_MOTOR_EXPORT_TOKEN") or "v5export").strip()
    if not token or token.strip() != expected:
        raise HTTPException(status_code=403, detail="Token V5 invalide.")


def _table_exists(table: str) -> bool:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name = %s
                LIMIT 1
                """,
                (table,),
            )
            return cur.fetchone() is not None


def _fetch_history_rows_for_day(target_day: str) -> List[Dict[str, Any]]:
    if not _table_exists("tennis_premium_history"):
        raise HTTPException(status_code=404, detail="Table tennis_premium_history absente.")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM tennis_premium_history
                WHERE date = %s
                ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST, id DESC
                """,
                (target_day,),
            )
            return [dict(row) for row in cur.fetchall()]


def _fetch_all_history_rows(limit: int = 300000) -> List[Dict[str, Any]]:
    if not _table_exists("tennis_premium_history"):
        raise HTTPException(status_code=404, detail="Table tennis_premium_history absente.")
    limit = max(1, min(int(limit), 300000))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM tennis_premium_history
                ORDER BY date DESC, COALESCE(updated_at, created_at) DESC NULLS LAST, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]


def _first_nonempty(*values: Any) -> str:
    for value in values:
        s = _s(value)
        if s:
            return s
    return ""


def _pick_odd(row: Dict[str, Any], raw: Dict[str, Any]) -> float:
    # V5 utilise la cote du joueur prédit. odd_predicted est la source principale.
    candidates = [
        row.get("odd_predicted"),
        raw.get("oddPredicted"),
        raw.get("odd"),
        row.get("refuse_value_odd"),
        raw.get("refuseValueOdd"),
    ]
    for candidate in candidates:
        odd = _float(candidate, 0.0)
        if odd > 1.0:
            return odd
    return 0.0


def _is_grand_slam(row: Dict[str, Any], raw: Dict[str, Any]) -> bool:
    text = " ".join(
        _s(value).lower()
        for value in [
            row.get("tournament"),
            row.get("season_name"),
            row.get("round"),
            raw.get("tournament"),
            raw.get("seasonName"),
            raw.get("round"),
        ]
        if value is not None
    )
    return any(keyword in text for keyword in V5_GRAND_SLAM_KEYWORDS)


def _profit_for_result(result: str, odd: float, stake: float = 100.0) -> Tuple[float, float]:
    r = _s(result).lower()
    if r == "win" and odd > 1.0:
        return round((odd - 1.0) * stake, 2), stake
    if r == "loss":
        return -stake, stake
    return 0.0, 0.0


def _dedupe_key(row: Dict[str, Any], raw: Dict[str, Any]) -> str:
    sport_event_id = _first_nonempty(
        row.get("sport_event_id"),
        raw.get("sportradarSportEventId"),
        raw.get("sportEventId"),
        raw.get("id"),
    )
    if sport_event_id:
        return "event:" + sport_event_id
    return "fallback:" + "|".join(
        [
            _s(row.get("date")),
            _s(row.get("tournament")),
            _s(row.get("source_player_a")),
            _s(row.get("source_player_b")),
            _s(row.get("predicted_winner")),
        ]
    ).lower()


def _dedupe_rows(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    seen = set()
    unique: List[Dict[str, Any]] = []
    duplicates = 0
    for row in rows:
        raw = _load_json(row.get("raw_json"), {})
        key = _dedupe_key(row, raw)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(row)
    return unique, duplicates


def _row_to_match(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = _load_json(row.get("raw_json"), {})
    category = _upper(row.get("status") or raw.get("status") or raw.get("category"))
    odd = _pick_odd(row, raw)
    result = _s(row.get("result") or raw.get("result") or "pending").lower()
    profit, staked = _profit_for_result(result, odd)
    sport_event_id = _first_nonempty(
        row.get("sport_event_id"),
        raw.get("sportradarSportEventId"),
        raw.get("sportEventId"),
        raw.get("id"),
    )
    tournament = row.get("tournament") or raw.get("tournament")
    season_name = row.get("season_name") or raw.get("seasonName")
    round_name = row.get("round") or raw.get("round")
    source_player_a = row.get("source_player_a") or raw.get("sourcePlayerA") or raw.get("playerA")
    source_player_b = row.get("source_player_b") or raw.get("sourcePlayerB") or raw.get("playerB")
    pick = row.get("predicted_winner") or raw.get("predictedWinner")
    opponent = row.get("opponent") or raw.get("opponent")
    is_grand_slam = _is_grand_slam(row, raw)

    return {
        "date": row.get("date"),
        "sportEventId": sport_event_id,
        "historyId": row.get("id"),
        "tournament": tournament,
        "seasonName": season_name,
        "round": round_name,
        "surface": row.get("surface") or raw.get("surface"),
        "sourcePlayerA": source_player_a,
        "sourcePlayerB": source_player_b,
        "match": f"{source_player_a} vs {source_player_b}",
        "pick": pick,
        "opponent": opponent,
        "category": category,
        "premiumPct": row.get("premium_pct") if row.get("premium_pct") is not None else raw.get("premiumPct"),
        "odd": odd,
        "oddOpponent": _float(row.get("odd_opponent") or raw.get("oddOpponent"), 0.0),
        "oddsSource": row.get("odds_source") or raw.get("oddsSource"),
        "result": result,
        "realWinner": row.get("real_winner") or raw.get("realWinner"),
        "score": row.get("score") or raw.get("score"),
        "profitEur100": profit,
        "stakedEur100": staked,
        "isGrandSlam": is_grand_slam,
        "v5Decision": "V5_BET",
        "v5RuleId": V5_RULE_ID,
        "v5Reason": "HORS_GRAND_CHELEM_ET_COTE_SUP_2_50",
        "v5Version": V5_COTE250_VERSION,
    }


def _v5_accepts(row: Dict[str, Any]) -> Tuple[bool, str]:
    raw = _load_json(row.get("raw_json"), {})
    category = _upper(row.get("status") or raw.get("status") or raw.get("category"))
    if category not in V5_ALLOWED_CATEGORIES:
        return False, "CATEGORY_NOT_ALLOWED"
    if _is_grand_slam(row, raw):
        return False, "GRAND_CHELEM_EXCLUDED"
    odd = _pick_odd(row, raw)
    if odd <= V5_ODD_MIN_EXCLUSIVE:
        return False, "ODD_LTE_2_50"
    return True, "HORS_GRAND_CHELEM_ET_COTE_SUP_2_50"


def _summary(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_category: Dict[str, Dict[str, Any]] = {}
    wins = losses = voids = pending = 0
    settled = 0
    profit = 0.0
    staked = 0.0
    for match in matches:
        category = _upper(match.get("category")) or "UNKNOWN"
        bucket = by_category.setdefault(
            category,
            {"total": 0, "settled": 0, "wins": 0, "losses": 0, "voids": 0, "pending": 0, "profitEur": 0.0, "stakedEur": 0.0, "roiPct": 0.0},
        )
        bucket["total"] += 1
        result = _s(match.get("result")).lower()
        if result == "win":
            wins += 1
            settled += 1
            bucket["wins"] += 1
            bucket["settled"] += 1
        elif result == "loss":
            losses += 1
            settled += 1
            bucket["losses"] += 1
            bucket["settled"] += 1
        elif result in {"void", "refunded", "refund"}:
            voids += 1
            bucket["voids"] += 1
        else:
            pending += 1
            bucket["pending"] += 1
        bucket["profitEur"] = round(float(bucket["profitEur"]) + _float(match.get("profitEur100"), 0.0), 2)
        bucket["stakedEur"] = round(float(bucket["stakedEur"]) + _float(match.get("stakedEur100"), 0.0), 2)
        profit += _float(match.get("profitEur100"), 0.0)
        staked += _float(match.get("stakedEur100"), 0.0)
    for bucket in by_category.values():
        bucket["roiPct"] = round((bucket["profitEur"] / bucket["stakedEur"] * 100.0), 2) if bucket["stakedEur"] else 0.0
    return {
        "total": len(matches),
        "settled": settled,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "pending": pending,
        "profitEur": round(profit, 2),
        "stakedEur": round(staked, 2),
        "roiPct": round((profit / staked * 100.0), 2) if staked else 0.0,
        "byCategory": by_category,
    }


@router.get("/v5/status")
def v5_status() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": V5_COTE250_VERSION,
        "endpoint": "/v5/status",
        "officialMutation": False,
        "replacesV2V3V4": False,
        "rule": {
            "ruleId": V5_RULE_ID,
            "decision": "V5_BET",
            "conditions": [
                "tournament/seasonName/round ne contient pas Wimbledon, French Open, Roland Garros, Australian Open ou US Open",
                "oddPredicted strictement > 2.50",
                "category dans PREMIUM, PROCHE ou REFUSE",
                "sportEventId unique pour éviter les doublons",
            ],
            "noBetOtherwise": True,
        },
        "policy": "V5 pré-match parallèle : filtre chirurgical hors Grand Chelem + cote > 2.50. Ne remplace pas V2/V3/V4.",
    }


def _v5_daily_payload(target_day: str) -> Dict[str, Any]:
    rows = _fetch_history_rows_for_day(target_day)
    unique_rows, duplicates_skipped = _dedupe_rows(rows)
    matches: List[Dict[str, Any]] = []
    rejected_reasons: Dict[str, int] = {}
    for row in unique_rows:
        accepted, reason = _v5_accepts(row)
        if accepted:
            matches.append(_row_to_match(row))
        else:
            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1

    matches.sort(key=lambda m: (_float(m.get("odd"), 0.0), _s(m.get("tournament")), _s(m.get("pick"))), reverse=True)
    return {
        "status": "ok",
        "version": V5_COTE250_VERSION,
        "endpoint": "/v5/daily",
        "targetDay": target_day,
        "source": "postgres:tennis_premium_history",
        "officialMutation": False,
        "replacesV2V3V4": False,
        "ruleId": V5_RULE_ID,
        "decision": "V5_BET",
        "counts": {
            "rowsForDay": len(rows),
            "uniqueRowsForDay": len(unique_rows),
            "duplicatesSkipped": duplicates_skipped,
            "v5Bet": len(matches),
            "noBet": len(unique_rows) - len(matches),
            "noBetReasons": rejected_reasons,
        },
        "summary": _summary(matches),
        "matches": matches,
        "policy": "V5 ne fait qu'un filtre : hors Grand Chelem + cote du joueur prédit > 2.50. Aucun live. Aucun remplacement V2/V3/V4.",
    }


@router.get("/v5/daily")
def v5_daily(day: str = Query("today")) -> Response:
    target_day = _normalize_day(day if isinstance(day, str) else "today")
    return _json_response(_v5_daily_payload(target_day))


@router.get("/v5/cote250")
def v5_cote250(day: str = Query("today")) -> Response:
    target_day = _normalize_day(day if isinstance(day, str) else "today")
    payload = _v5_daily_payload(target_day)
    payload["endpoint"] = "/v5/cote250"
    return _json_response(payload)


@router.get("/v5/backtest/cote250")
def v5_backtest_cote250(
    token: str = Query("", description="Token export"),
    limit: int = Query(300000, ge=1, le=300000),
) -> Response:
    _check_export_token(token)
    rows = _fetch_all_history_rows(limit=limit)
    unique_rows, duplicates_skipped = _dedupe_rows(rows)
    matches = [_row_to_match(row) for row in unique_rows if _v5_accepts(row)[0]]
    matches.sort(key=lambda m: (_s(m.get("date")), _s(m.get("tournament")), _float(m.get("odd"), 0.0)), reverse=True)
    settled_matches = [m for m in matches if _s(m.get("result")).lower() in {"win", "loss"}]
    return _json_response({
        "status": "ok",
        "version": V5_COTE250_VERSION,
        "endpoint": "/v5/backtest/cote250",
        "ruleId": V5_RULE_ID,
        "officialMutation": False,
        "rowsLoaded": len(rows),
        "uniqueRows": len(unique_rows),
        "duplicatesSkipped": duplicates_skipped,
        "totalV5MatchesIncludingVoidPending": len(matches),
        "summaryAllV5Matches": _summary(matches),
        "summarySettledOnly": _summary(settled_matches),
        "matches": matches,
        "policy": "Backtest lecture seule du filtre V5 : hors Grand Chelem + cote > 2.50. ROI calculé seulement sur win/loss car void/pending ont stake 0.",
    })
