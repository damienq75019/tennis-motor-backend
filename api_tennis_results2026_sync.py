from __future__ import annotations

import csv
import datetime as dt
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from api_tennis_daily_builder import ApiTennisDailyBuilder
from postgres_results_store import CSV_HEADER, PostgresResultsStore


INVALID_SCORE_TOKENS = ("RET", "W/O", "WO", "DEF", "ABD", "ABN", "CANCEL", "WALKOVER", "WITHDRAW")


def _s(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    text = _s(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _same_player(a: Any, b: Any) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = na.split(), nb.split()
    if not ta or not tb:
        return False
    if ta[-1] == tb[-1]:
        return ta[0][0] == tb[0][0] or ta[0] in tb or tb[0] in ta
    return set(ta) == set(tb)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def _is_finished(match: Dict[str, Any]) -> bool:
    status = _s(match.get("status")).lower()
    match_status = _s(match.get("matchStatus")).lower()
    return status == "finished" or match_status == "finished"


def _is_void(match: Dict[str, Any]) -> bool:
    blob = " ".join([
        _s(match.get("status")),
        _s(match.get("matchStatus")),
        _s(match.get("apiTennisRawStatus")),
        _s(match.get("score")),
    ]).lower()
    return any(tok in blob for tok in ["retired", "walkover", "cancelled", "canceled", "abandoned", "withdrawn", "forfeit", "ret", "w/o"])


def _valid_score(score: Any) -> bool:
    raw = _s(score)
    if not raw or raw == "0-0":
        return False
    upper = raw.upper()
    return not any(tok in upper for tok in INVALID_SCORE_TOKENS)


def _date_to_yyyymmdd(value: str) -> str:
    try:
        return dt.date.fromisoformat(value).strftime("%Y%m%d")
    except Exception:
        return _s(value).replace("-", "")


def _round_code(value: Any) -> str:
    text = _s(value).lower()
    if "final" in text and "semi" not in text and "1/" not in text:
        return "F"
    if "semi" in text or "1/2" in text:
        return "SF"
    if "quarter" in text or "1/4" in text:
        return "QF"
    if "1/8" in text or "16" in text:
        return "R16"
    if "1/16" in text or "32" in text:
        return "R32"
    if "1/32" in text or "64" in text:
        return "R64"
    if "1/64" in text or "128" in text:
        return "R128"
    return _s(value) or "R128"


def _row_from_match(match: Dict[str, Any], target_day: str, match_num: int) -> Optional[Dict[str, Any]]:
    if not _is_finished(match) or _is_void(match):
        return None
    score = _s(match.get("score"))
    if not _valid_score(score):
        return None

    a = _s(match.get("sourcePlayerA") or match.get("playerA"))
    b = _s(match.get("sourcePlayerB") or match.get("playerB"))
    winner_id = _s(match.get("winnerId"))
    a_id = _s(match.get("sportradarPlayerAId") or match.get("apiTennisFirstPlayerKey"))
    b_id = _s(match.get("sportradarPlayerBId") or match.get("apiTennisSecondPlayerKey"))

    if winner_id and a_id and winner_id == a_id:
        winner, loser = a, b
        wrank, lrank = _safe_int(match.get("playerARank")), _safe_int(match.get("playerBRank"))
        wpts, lpts = _safe_int(match.get("playerAPoints")), _safe_int(match.get("playerBPoints"))
    elif winner_id and b_id and winner_id == b_id:
        winner, loser = b, a
        wrank, lrank = _safe_int(match.get("playerBRank")), _safe_int(match.get("playerARank"))
        wpts, lpts = _safe_int(match.get("playerBPoints")), _safe_int(match.get("playerAPoints"))
    else:
        # API-Tennis normally supplies event_winner as First Player / Second Player.
        # If not, skip rather than inventing a result.
        return None

    if not winner or not loser:
        return None

    row = {k: "" for k in CSV_HEADER}
    row.update({
        "tourney_id": f"api_tennis_{target_day.replace('-', '')}_{_s(match.get('sportradarCompetitionId') or match.get('apiTennisEventKey'))}",
        "tourney_name": _s(match.get("tournament") or match.get("seasonName") or "ATP"),
        "surface": _s(match.get("surface") or "Hard"),
        "draw_size": "",
        "tourney_level": "G" if "open" in _s(match.get("tournament")).lower() and "french" in _s(match.get("tournament")).lower() else "A",
        "indoor": "",
        "tourney_date": _date_to_yyyymmdd(target_day),
        "match_num": str(match_num),
        "winner_id": _s(winner_id.replace("api_tennis:player:", "")),
        "winner_name": winner,
        "winner_rank": str(wrank or ""),
        "winner_rank_points": str(wpts or ""),
        "loser_id": "",
        "loser_name": loser,
        "loser_rank": str(lrank or ""),
        "loser_rank_points": str(lpts or ""),
        "score": score,
        "best_of": "5" if "french open" in _s(match.get("tournament")).lower() else "",
        "round": _round_code(match.get("round")),
        "sport_event_id": _s(match.get("sportEventId") or match.get("sportradarSportEventId")),
    })
    return row


class ApiTennisResults2026Syncer:
    """API-Tennis-only replacement for the old Sportradar results syncer.

    Kept separate from the engine. It writes completed ATP singles results into
    PostgreSQL and can rebuild data/2026.csv for motor compatibility.
    """

    def __init__(self, base_dir: Optional[Path] = None, builder: Optional[ApiTennisDailyBuilder] = None) -> None:
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent)
        self.data_dir = self.base_dir / "data"
        self.csv_path = self.data_dir / "2026.csv"
        self.builder = builder or ApiTennisDailyBuilder(audit_dir=self.base_dir / "output" / "audits")
        self.store = PostgresResultsStore()

    def status(self) -> Dict[str, Any]:
        return {
            "status": "ok" if self.builder.enabled else "missing_key",
            "provider": "api_tennis",
            "apiTennisKeyConfigured": self.builder.enabled,
            "results2026Storage": "postgres" if self.store.enabled else "csv_unavailable_without_database",
            "csvPath": str(self.csv_path),
            "policy": "STEP44 : résultats 2026 synchronisés via API-Tennis uniquement.",
        }

    def postgres_status(self) -> Dict[str, Any]:
        out = self.store.status()
        out["provider"] = "api_tennis"
        out["policy"] = "STEP44 : stockage results2026 alimenté par API-Tennis uniquement."
        return out

    def export_postgres_to_csv(self) -> Dict[str, Any]:
        if not self.store.enabled:
            return {
                "status": "error",
                "provider": "api_tennis",
                "error": "DATABASE_URL absente.",
            }
        try:
            self.store.import_csv_if_empty(self.csv_path)
            result = self.store.export_csv(self.csv_path, backup=True)
            return {
                "status": "ok",
                "provider": "api_tennis",
                **result,
            }
        except Exception as exc:
            return {
                "status": "error",
                "provider": "api_tennis",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def sync_day(self, target_day: str, *, dry_run: bool = False) -> Dict[str, Any]:
        counts = {
            "raw_matches": 0,
            "finished_matches": 0,
            "rows_prepared": 0,
            "rows_added": 0,
            "skipped_duplicate": 0,
            "skipped_not_finished": 0,
            "skipped_void": 0,
            "skipped_missing_score": 0,
            "skipped_unresolved_winner": 0,
        }
        errors: List[str] = []
        samples: List[Dict[str, Any]] = []

        built = self.builder.build_matches_for_day(target_day)
        if built.get("status") != "ok":
            return {
                "status": "error",
                "provider": "api_tennis",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": [str(built.get("error") or "API-Tennis build failed")],
                "counts": counts,
                "audit": built.get("audit", {}),
            }

        matches = built.get("matches") if isinstance(built.get("matches"), list) else []
        counts["raw_matches"] = len(matches)

        rows: List[Dict[str, Any]] = []
        match_num = 1
        for match in matches:
            if not isinstance(match, dict):
                continue
            if _is_void(match):
                counts["skipped_void"] += 1
                continue
            if not _is_finished(match):
                counts["skipped_not_finished"] += 1
                continue
            counts["finished_matches"] += 1
            if not _valid_score(match.get("score")):
                counts["skipped_missing_score"] += 1
                continue
            row = _row_from_match(match, target_day, match_num)
            if row is None:
                counts["skipped_unresolved_winner"] += 1
                continue
            rows.append(row)
            match_num += 1
            if len(samples) < 10:
                samples.append({
                    "eventId": row.get("sport_event_id"),
                    "winner": row.get("winner_name"),
                    "loser": row.get("loser_name"),
                    "score": row.get("score"),
                    "round": row.get("round"),
                })

        counts["rows_prepared"] = len(rows)

        if dry_run:
            return {
                "status": "ok",
                "provider": "api_tennis",
                "targetDay": target_day,
                "dryRun": True,
                "errors": errors,
                "counts": counts,
                "sample": samples,
                "audit": built.get("audit", {}),
                "storage": {"mode": "dry_run"},
                "policy": "STEP44 dry-run : aucune écriture.",
            }

        if not self.store.enabled:
            return {
                "status": "error",
                "provider": "api_tennis",
                "targetDay": target_day,
                "dryRun": False,
                "errors": ["DATABASE_URL absente : results2026 PostgreSQL indisponible."],
                "counts": counts,
                "sample": samples,
            }

        try:
            self.store.import_csv_if_empty(self.csv_path)
            inserted, skipped = self.store.insert_rows(rows, source="api_tennis")
            counts["rows_added"] = inserted
            counts["skipped_duplicate"] = skipped
            export_result = self.store.export_csv(self.csv_path, backup=True)
        except Exception as exc:
            return {
                "status": "error",
                "provider": "api_tennis",
                "targetDay": target_day,
                "dryRun": False,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "counts": counts,
                "sample": samples,
            }

        return {
            "status": "ok",
            "provider": "api_tennis",
            "targetDay": target_day,
            "dryRun": False,
            "errors": errors,
            "counts": counts,
            "sample": samples,
            "audit": built.get("audit", {}),
            "storage": {
                "mode": "postgres",
                "databaseConfigured": self.store.enabled,
                "table": self.store.TABLE,
                "export": export_result,
                "status": self.store.status(),
            },
            "policy": "STEP44 : get_fixtures API-Tennis -> results2026 PostgreSQL -> data/2026.csv.",
        }


# Compatibility alias for older imports.
Results2026Syncer = ApiTennisResults2026Syncer
