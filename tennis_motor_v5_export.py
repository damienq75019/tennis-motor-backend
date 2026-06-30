from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Response


EXPORT_VERSION = "V5_EXPORT_DATASET_2026-06-30"
router = APIRouter()


def _database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL absente : export Postgres indisponible.")
    return url


def _connect():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"psycopg indisponible: {type(exc).__name__}: {exc}") from exc
    return psycopg.connect(_database_url(), connect_timeout=10, row_factory=dict_row)


def _check_export_token(token: str) -> None:
    """Protection simple pour ne pas exposer l'historique publiquement par erreur.

    Recommandé sur Railway : définir TENNIS_MOTOR_EXPORT_TOKEN avec une valeur privée.
    Si la variable n'existe pas encore, le token temporaire par défaut est v5export.
    """
    expected = (os.environ.get("TENNIS_MOTOR_EXPORT_TOKEN") or "v5export").strip()
    if not token or token.strip() != expected:
        raise HTTPException(
            status_code=403,
            detail="Token export invalide. Ajoute ?token=... ou définis TENNIS_MOTOR_EXPORT_TOKEN sur Railway.",
        )


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _json_response(payload: Any, *, filename: Optional[str] = None) -> Response:
    headers = {}
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(
        content=json.dumps(payload, ensure_ascii=False, default=_json_default),
        media_type="application/json; charset=utf-8",
        headers=headers,
    )


def _csv_response(rows: List[Dict[str, Any]], *, filename: str) -> Response:
    out = io.StringIO()
    # Union stable des colonnes : colonnes de la première ligne puis nouvelles colonnes ensuite.
    fieldnames: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        clean = {}
        for key in fieldnames:
            value = row.get(key)
            if isinstance(value, (dict, list)):
                clean[key] = json.dumps(value, ensure_ascii=False, default=_json_default)
            elif isinstance(value, (datetime, date)):
                clean[key] = value.isoformat()
            elif isinstance(value, Decimal):
                clean[key] = float(value)
            elif value is None:
                clean[key] = ""
            else:
                clean[key] = value
        writer.writerow(clean)
    return Response(
        content=out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


def _pick_odd(row: Dict[str, Any], raw: Dict[str, Any]) -> float:
    candidates = [
        row.get("odd_predicted"),
        row.get("refuse_value_odd"),
        raw.get("refuseValueOdd"),
        raw.get("odd"),
        raw.get("playerAOdd"),
        raw.get("playerBOdd"),
    ]
    for candidate in candidates:
        odd = _float(candidate, 0.0)
        if odd > 1.0:
            return odd
    return 0.0


def _profit_for_result(result: str, odd: float, stake: float = 100.0) -> Tuple[float, float]:
    r = _s(result).lower()
    if r == "win" and odd > 1.0:
        return round((odd - 1.0) * stake, 2), stake
    if r == "loss":
        return -stake, stake
    return 0.0, 0.0


def _is_qualification(*values: Any) -> bool:
    text = " ".join(_s(v).lower() for v in values if v is not None)
    # API-Tennis / anciennes sources peuvent écrire Qualification, Qualifying, Q ou qualifiers.
    return any(token in text for token in ["qualification", "qualifying", "qualifier", "qualifiers", " - q", " q-"])


def _draw_type(row: Dict[str, Any], raw: Dict[str, Any]) -> str:
    explicit = _s(raw.get("drawType") or raw.get("draw_type") or row.get("draw_type"))
    if explicit:
        return explicit.upper()
    if _is_qualification(row.get("round"), row.get("tournament"), row.get("season_name"), raw.get("round"), raw.get("tournament")):
        return "QUALIFICATION"
    return "MAIN_OR_UNKNOWN"


def _first_nonempty(*values: Any) -> str:
    for value in values:
        s = _s(value)
        if s:
            return s
    return ""


def _table_names() -> List[str]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name LIKE 'tennis_%'
                ORDER BY table_name
                """
            )
            return [str(row["table_name"]) for row in cur.fetchall()]


def _validate_table_name(table: str) -> str:
    table = _s(table)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise HTTPException(status_code=400, detail="Nom de table invalide.")
    allowed = set(_table_names())
    if table not in allowed:
        raise HTTPException(status_code=404, detail=f"Table inconnue ou non autorisée: {table}")
    return table


def _columns_for_table(table: str) -> List[str]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table,),
            )
            return [str(row["column_name"]) for row in cur.fetchall()]


def _count_table(table: str) -> int:
    from psycopg import sql

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT COUNT(*) AS c FROM {}").format(sql.Identifier(table)))
            row = cur.fetchone()
            return int(row["c"] or 0)


def _fetch_table(table: str, *, limit: int = 100000, offset: int = 0) -> List[Dict[str, Any]]:
    from psycopg import sql

    table = _validate_table_name(table)
    limit = max(1, min(int(limit), 300000))
    offset = max(0, int(offset))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT * FROM {} LIMIT %s OFFSET %s").format(sql.Identifier(table)),
                (limit, offset),
            )
            return [dict(row) for row in cur.fetchall()]


def _fetch_shadow_decisions_map() -> Dict[str, Dict[str, Any]]:
    table_names = set(_table_names())
    if "tennis_v3_shadow_decisions" not in table_names:
        return {}
    rows = _fetch_table("tennis_v3_shadow_decisions", limit=300000)
    # Dernière décision active par match_key, en privilégiant updated_at descendant si présent.
    rows.sort(key=lambda r: _s(r.get("updated_at") or r.get("created_at")), reverse=True)
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row.get("is_active") is False:
            continue
        key = _s(row.get("match_key"))
        if key and key not in out:
            out[key] = row
    return out


def _fetch_rules_map() -> Dict[str, Dict[str, Any]]:
    table_names = set(_table_names())
    if "tennis_v3_shadow_rules" not in table_names:
        return {}
    return {str(row.get("rule_id")): row for row in _fetch_table("tennis_v3_shadow_rules", limit=300000)}


def _v5_dataset_rows(limit: int = 300000) -> List[Dict[str, Any]]:
    table_names = set(_table_names())
    if "tennis_premium_history" not in table_names:
        raise HTTPException(status_code=404, detail="Table tennis_premium_history absente.")

    history_rows = _fetch_table("tennis_premium_history", limit=limit)
    decisions_by_key = _fetch_shadow_decisions_map()
    rules_by_id = _fetch_rules_map()

    out: List[Dict[str, Any]] = []
    for row in history_rows:
        raw = _load_json(row.get("raw_json"), {})
        sport_event_id = _first_nonempty(row.get("sport_event_id"), raw.get("sportradarSportEventId"), raw.get("sportEventId"), raw.get("id"))
        match_keys = []
        if sport_event_id:
            match_keys.extend([sport_event_id, "event:" + sport_event_id])
        match_keys.append(_s(row.get("id")))
        decision = {}
        for key in match_keys:
            if key in decisions_by_key:
                decision = decisions_by_key[key]
                break

        tracking = _load_json(decision.get("tracking_payload_json"), {}) if decision else {}
        source_payload = _load_json(decision.get("source_payload_json"), {}) if decision else {}
        primary_rule_id = _first_nonempty(decision.get("primary_rule_id"), decision.get("rule_id"), source_payload.get("primaryRuleId"))
        rule = rules_by_id.get(primary_rule_id, {}) if primary_rule_id else {}

        v4 = raw.get("v4Lab") if isinstance(raw.get("v4Lab"), dict) else {}
        v4_legacy = raw.get("v4Legacy") if isinstance(raw.get("v4Legacy"), dict) else {}
        step56 = raw.get("step56Official") if isinstance(raw.get("step56Official"), dict) else {}
        audit = raw.get("audit") if isinstance(raw.get("audit"), dict) else {}

        category = _upper(row.get("status") or raw.get("status") or raw.get("category"))
        result = _s(row.get("result") or raw.get("result") or "pending").lower()
        odd = _pick_odd(row, raw)
        profit, staked = _profit_for_result(result, odd)
        draw_type = _draw_type(row, raw)
        is_qualification = draw_type == "QUALIFICATION" or _is_qualification(row.get("round"), row.get("tournament"), raw.get("round"), raw.get("tournament"))

        data_quality = v4.get("dataQualityScore") if v4 else ""
        edge = v4.get("edge") if v4 else ""
        expected_value_pct = v4.get("expectedValuePct") if v4 else ""

        out.append({
            "date": row.get("date"),
            "tournament": row.get("tournament") or raw.get("tournament"),
            "seasonName": row.get("season_name") or raw.get("seasonName"),
            "round": row.get("round") or raw.get("round"),
            "surface": row.get("surface") or raw.get("surface"),
            "drawType": draw_type,
            "isQualification": bool(is_qualification),
            "sportEventId": sport_event_id,
            "historyId": row.get("id"),
            "sourcePlayerA": row.get("source_player_a") or raw.get("sourcePlayerA") or raw.get("playerA"),
            "sourcePlayerB": row.get("source_player_b") or raw.get("sourcePlayerB") or raw.get("playerB"),
            "pick": row.get("predicted_winner") or raw.get("predictedWinner"),
            "opponent": row.get("opponent") or raw.get("opponent"),
            "category": category,
            "premiumPct": row.get("premium_pct") or raw.get("premiumPct"),
            "decisionV2": row.get("decision") or raw.get("decision"),
            "veto": row.get("veto") or raw.get("veto"),
            "odd": odd,
            "oddOpponent": _float(row.get("odd_opponent"), 0.0),
            "oddsSource": row.get("odds_source") or raw.get("oddsSource"),
            "result": result,
            "realWinner": row.get("real_winner") or raw.get("realWinner"),
            "score": row.get("score") or raw.get("score"),
            "profitEur100": profit,
            "stakedEur100": staked,
            "step56PredictedWinner": step56.get("predictedWinner") or raw.get("step56PredictedWinner"),
            "step56ConfidencePct": step56.get("confidencePct") or raw.get("step56ConfidencePct"),
            "step56Category": raw.get("step56OfficialCategory"),
            "pSwe": raw.get("pSwe"),
            "pAtp": raw.get("pAtp"),
            "pRank": raw.get("pRank"),
            "pForm5": raw.get("pForm5"),
            "pForm10": raw.get("pForm10"),
            "pSurfaceForm5": raw.get("pSurfaceForm5"),
            "pDominance": raw.get("pDominance"),
            "auditStatus": audit.get("status"),
            "auditSeverity": audit.get("severity"),
            "auditFlags": ",".join(audit.get("flagCodes") or []),
            "refuseValueApplies": row.get("refuse_value_applies") if row.get("refuse_value_applies") is not None else raw.get("refuseValueApplies"),
            "refuseValueStatus": row.get("refuse_value_status") or raw.get("refuseValueStatus"),
            "refuseValueDecision": row.get("refuse_value_decision") or raw.get("refuseValueDecision"),
            "refuseValueEvPct": row.get("refuse_value_ev_pct") if row.get("refuse_value_ev_pct") is not None else raw.get("refuseValueEvPct"),
            "v3ShadowDecision": decision.get("shadow_decision") or source_payload.get("shadowDecision"),
            "v3RuleId": primary_rule_id,
            "v3RuleName": rule.get("name") or source_payload.get("primaryRuleName") or decision.get("reason"),
            "v3RuleSegment": rule.get("source_segment") or source_payload.get("sourceSegment"),
            "v3FinalResult": decision.get("final_result") or tracking.get("result"),
            "v3FinalProfitEur": decision.get("final_profit_eur") if decision else "",
            "v3DeltaVsV2Eur": decision.get("delta_vs_v2_eur") if decision else "",
            "v4Decision": v4.get("v4Decision") or v4.get("decision"),
            "v4Action": v4.get("v4Action"),
            "v4Grade": v4.get("grade"),
            "v4DataQualityScore": data_quality,
            "v4Edge": edge,
            "v4ExpectedValuePct": expected_value_pct,
            "v4FavoriteTrapRisk": v4.get("favoriteTrapRisk"),
            "v4ShortReason": v4.get("shortReason"),
            "v4LegacyDecision": v4_legacy.get("decision"),
            "v4LegacyGrade": v4_legacy.get("grade"),
            "exportVersion": EXPORT_VERSION,
        })

    out.sort(key=lambda r: (_s(r.get("date")), _s(r.get("tournament")), _s(r.get("pick"))), reverse=True)
    return out


@router.get("/export/v5/status")
def export_v5_status(token: str = Query("", description="Token export")) -> Response:
    _check_export_token(token)
    tables = []
    for table in _table_names():
        try:
            tables.append({
                "table": table,
                "rows": _count_table(table),
                "columns": _columns_for_table(table),
            })
        except Exception as exc:
            tables.append({"table": table, "error": f"{type(exc).__name__}: {exc}"})
    return _json_response({
        "status": "ok",
        "version": EXPORT_VERSION,
        "endpoint": "/export/v5/status",
        "tables": tables,
        "policy": "Export lecture seule. Ne modifie pas la base.",
    })


@router.get("/export/v5/table")
def export_v5_table(
    table: str = Query(..., description="Nom exact de la table tennis_*"),
    token: str = Query("", description="Token export"),
    format: str = Query("json", pattern="^(json|csv)$"),
    limit: int = Query(100000, ge=1, le=300000),
    offset: int = Query(0, ge=0),
) -> Response:
    _check_export_token(token)
    rows = _fetch_table(table, limit=limit, offset=offset)
    safe_table = _validate_table_name(table)
    if format == "csv":
        return _csv_response(rows, filename=f"{safe_table}_export.csv")
    return _json_response({
        "status": "ok",
        "version": EXPORT_VERSION,
        "endpoint": "/export/v5/table",
        "table": safe_table,
        "limit": limit,
        "offset": offset,
        "count": len(rows),
        "rows": rows,
    }, filename=f"{safe_table}_export.json")


@router.get("/export/v5/all")
def export_v5_all(
    token: str = Query("", description="Token export"),
    limit_per_table: int = Query(300000, ge=1, le=300000),
) -> Response:
    _check_export_token(token)
    bundle: Dict[str, Any] = {}
    for table in _table_names():
        bundle[table] = {
            "rows": _fetch_table(table, limit=limit_per_table),
        }
    return _json_response({
        "status": "ok",
        "version": EXPORT_VERSION,
        "endpoint": "/export/v5/all",
        "tables": bundle,
    }, filename="tennis_motor_all_tables_v5_export.json")


@router.get("/export/v5-dataset")
def export_v5_dataset(
    token: str = Query("", description="Token export"),
    format: str = Query("csv", pattern="^(json|csv)$"),
    limit: int = Query(300000, ge=1, le=300000),
) -> Response:
    _check_export_token(token)
    rows = _v5_dataset_rows(limit=limit)
    if format == "csv":
        return _csv_response(rows, filename="tennis_motor_v5_dataset.csv")
    return _json_response({
        "status": "ok",
        "version": EXPORT_VERSION,
        "endpoint": "/export/v5-dataset",
        "count": len(rows),
        "rows": rows,
    }, filename="tennis_motor_v5_dataset.json")
