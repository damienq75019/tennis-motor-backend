from __future__ import annotations

import csv
import datetime as dt
import json
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sportradar_client import SportradarClient, SportradarError

CSV_HEADER = [
    "tourney_id", "tourney_name", "surface", "draw_size", "tourney_level", "indoor", "tourney_date",
    "match_num", "winner_id", "winner_seed", "winner_entry", "winner_name", "winner_hand", "winner_ht",
    "winner_ioc", "winner_age", "winner_rank", "winner_rank_points", "loser_id", "loser_seed",
    "loser_entry", "loser_name", "loser_hand", "loser_ht", "loser_ioc", "loser_age", "loser_rank",
    "loser_rank_points", "score", "best_of", "round", "minutes", "w_ace", "w_df", "w_svpt",
    "w_1stIn", "w_1stWon", "w_2ndWon", "w_SvGms", "w_bpSaved", "w_bpFaced", "l_ace",
    "l_df", "l_svpt", "l_1stIn", "l_1stWon", "l_2ndWon", "l_SvGms", "l_bpSaved", "l_bpFaced",
]

INVALID_SCORE_TOKENS = ("RET", "W/O", "WO", "DEF", "ABD", "ABN")


def _s(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _s(value).lower()


def _canon(value: Any) -> str:
    text = _s(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def _parse_time(value: Any) -> Optional[dt.datetime]:
    raw = _s(value)
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _walk(obj: Any, path: str = "") -> Iterable[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, v
            yield from _walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            yield p, v
            yield from _walk(v, p)


def _surface_to_engine(value: Any) -> str:
    text = _lower(value)
    if "clay" in text:
        return "Clay"
    if "grass" in text:
        return "Grass"
    if "hard" in text or "carpet" in text:
        return "Hard"
    return ""


def _extract_surface_from_payload(payload: Dict[str, Any]) -> str:
    for path, value in _walk(payload):
        if isinstance(value, (dict, list)):
            continue
        key = path.split(".")[-1].lower()
        if "surface" in key or "court" in key:
            mapped = _surface_to_engine(value)
            if mapped:
                return mapped
    return ""


def _extract_summaries(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    summaries = payload.get("summaries")
    if isinstance(summaries, list):
        return [x for x in summaries if isinstance(x, dict)]
    return []


def _get_event(summary: Dict[str, Any]) -> Dict[str, Any]:
    return summary.get("sport_event") or {}


def _get_status(summary: Dict[str, Any]) -> Dict[str, Any]:
    return summary.get("sport_event_status") or {}


def _event_id(summary: Dict[str, Any]) -> str:
    return _s(_get_event(summary).get("id"))


def _winner_id(summary: Dict[str, Any]) -> str:
    return _s(_get_status(summary).get("winner_id"))


def _is_finished(summary: Dict[str, Any]) -> bool:
    status = _get_status(summary)
    status_text = _lower(status.get("status"))
    match_status = _lower(status.get("match_status"))
    return bool(_winner_id(summary)) and (
        status_text in {"closed", "ended", "finished", "complete", "completed"}
        or match_status in {"ended", "finished", "complete", "completed"}
        or bool(status.get("period_scores"))
    )


def _event_start(summary: Dict[str, Any]) -> Optional[dt.datetime]:
    return _parse_time(_get_event(summary).get("start_time"))


def _is_atp_men_singles(summary: Dict[str, Any]) -> bool:
    ctx = (_get_event(summary).get("sport_event_context") or {})
    category = _lower((ctx.get("category") or {}).get("name"))
    competition = ctx.get("competition") or {}
    comp_type = _lower(competition.get("type"))
    gender = _lower(competition.get("gender"))
    return category == "atp" and comp_type == "singles" and gender == "men"


def _extract_competitors(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = _get_event(summary).get("competitors") or []
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        comp = item.get("competitor") if isinstance(item.get("competitor"), dict) else item
        if not isinstance(comp, dict):
            continue
        out.append({
            "id": _s(comp.get("id")),
            "name": _s(comp.get("name")),
            "country_code": _s(comp.get("country_code")),
            "country": _s(comp.get("country")),
            "qualifier": _lower(item.get("qualifier") or comp.get("qualifier")),
        })
    return out


def _competition_context(summary: Dict[str, Any]) -> Dict[str, Any]:
    return (_get_event(summary).get("sport_event_context") or {})


def _competition(summary: Dict[str, Any]) -> Dict[str, Any]:
    return _competition_context(summary).get("competition") or {}


def _season(summary: Dict[str, Any]) -> Dict[str, Any]:
    return _competition_context(summary).get("season") or {}


def _round_name(summary: Dict[str, Any]) -> str:
    ctx = _competition_context(summary)
    rnd = ctx.get("round") or {}
    if isinstance(rnd, dict):
        return _s(rnd.get("name") or rnd.get("cup_round_match_number") or rnd.get("number"))
    return _s(rnd)


def _map_round(value: str) -> str:
    text = _lower(value).replace(" ", "_").replace("-", "_")
    mapping = {
        "final": "F",
        "semi_final": "SF",
        "semifinal": "SF",
        "quarter_final": "QF",
        "quarterfinal": "QF",
        "round_of_16": "R16",
        "round_of_32": "R32",
        "round_of_64": "R64",
        "round_of_128": "R128",
        "qualification_round_1": "Q1",
        "qualification_round_2": "Q2",
        "qualification_round_3": "Q3",
        "qualification_final": "Q3",
    }
    if text in mapping:
        return mapping[text]
    if "qualification" in text and "1" in text:
        return "Q1"
    if "qualification" in text and "2" in text:
        return "Q2"
    if "qualification" in text and ("final" in text or "3" in text):
        return "Q3"
    return text.upper()[:12] or "R128"


def _build_points_by_id(rankings_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    rankings = rankings_payload.get("rankings") or []
    if not isinstance(rankings, list):
        return out
    for ranking in rankings:
        if not isinstance(ranking, dict):
            continue
        if _lower(ranking.get("name")) != "atp" or _lower(ranking.get("gender")) != "men":
            continue
        entries = ranking.get("competitor_rankings") or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            comp = entry.get("competitor") or {}
            if not isinstance(comp, dict):
                continue
            cid = _s(comp.get("id"))
            if not cid:
                continue
            out[cid] = {
                "rank": _safe_int(entry.get("rank"), 0),
                "points": _safe_int(entry.get("points"), 0),
                "name": _s(comp.get("name")),
                "country_code": _s(comp.get("country_code")),
            }
    return out


def _home_away_ids(competitors: List[Dict[str, Any]]) -> Tuple[str, str]:
    home = ""
    away = ""
    for comp in competitors:
        if comp.get("qualifier") == "home":
            home = comp.get("id", "")
        elif comp.get("qualifier") == "away":
            away = comp.get("id", "")
    if not home and competitors:
        home = competitors[0].get("id", "")
    if not away and len(competitors) > 1:
        away = competitors[1].get("id", "")
    return home, away


def _format_score(summary: Dict[str, Any], competitors: List[Dict[str, Any]]) -> str:
    status = _get_status(summary)
    period_scores = status.get("period_scores") or []
    if not isinstance(period_scores, list) or not period_scores:
        return ""

    winner = _winner_id(summary)
    home_id, away_id = _home_away_ids(competitors)
    winner_is_home = winner == home_id
    winner_is_away = winner == away_id
    if not winner_is_home and not winner_is_away:
        return ""

    sets: List[Tuple[int, int, int]] = []
    for period in period_scores:
        if not isinstance(period, dict):
            continue
        ptype = _lower(period.get("type"))
        if ptype and ptype not in {"set", "regular"}:
            continue
        number = _safe_int(period.get("number"), len(sets) + 1)
        hs = _safe_int(period.get("home_score"), -1)
        aw = _safe_int(period.get("away_score"), -1)
        if hs < 0 or aw < 0:
            continue
        if winner_is_home:
            sets.append((number, hs, aw))
        else:
            sets.append((number, aw, hs))

    sets.sort(key=lambda x: x[0])
    parts = [f"{w}-{l}" for _, w, l in sets if w >= 0 and l >= 0]
    return " ".join(parts)


def _tourney_level(tourney_name: str) -> str:
    name = _lower(tourney_name)
    if any(token in name for token in ["french open", "australian open", "wimbledon", "us open"]):
        return "G"
    if "atp finals" in name or "united cup" in name or "davis cup" in name:
        return "A"
    return "A"


def _best_of(tourney_name: str, round_code: str, score: str) -> str:
    name = _lower(tourney_name)
    if any(token in name for token in ["french open", "australian open", "wimbledon", "us open"]):
        if not round_code.startswith("Q"):
            return "5"
    return "3"


def _match_num_from_event_id(event_id: str, fallback: int) -> int:
    digits = re.findall(r"\d+", event_id)
    if not digits:
        return fallback
    return int(digits[-1][-6:])


def _existing_keys(csv_path: Path) -> Set[Tuple[str, str, str, str, str]]:
    keys: Set[Tuple[str, str, str, str, str]] = set()
    if not csv_path.exists():
        return keys
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (
                    _s(row.get("tourney_date")),
                    _canon(row.get("tourney_name")),
                    _canon(row.get("winner_name")),
                    _canon(row.get("loser_name")),
                    _s(row.get("score")),
                )
                keys.add(key)
    except Exception:
        return keys
    return keys


def _count_csv_rows(csv_path: Path) -> int:
    if not csv_path.exists():
        return 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return max(0, sum(1 for _ in f) - 1)


class Results2026Syncer:
    """Synchronise les résultats ATP men singles terminés vers un CSV 2026 séparé du Premium.

    Règles :
    - uniquement ATP / hommes / simple ;
    - uniquement matchs terminés avec winner_id et score lisible ;
    - aucun WTA, double, match futur ou résultat inventé ;
    - aucune dépendance à l'historique Premium.
    """

    def __init__(self, client: Optional[SportradarClient] = None, base_dir: Optional[Path] = None) -> None:
        self.client = client or SportradarClient()
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent)
        self.data_dir = self.base_dir / "data"
        self.output_dir = self.base_dir / "output" / "results2026"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.data_dir / "2026.csv"

    def status(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "provider": "sportradar",
            "target": "data/2026.csv",
            "csvPath": str(self.csv_path),
            "csvExists": self.csv_path.exists(),
            "rows": _count_csv_rows(self.csv_path),
            "policy": "ATP men singles finished only; winner_id required; score required; no duplicates; Premium history untouched",
        }

    def sync_day(self, target_day: str, *, dry_run: bool = False) -> Dict[str, Any]:
        if not self.client.enabled:
            return {
                "status": "error",
                "provider": "sportradar",
                "targetDay": target_day,
                "error": "SPORTRADAR_API_KEY absente côté backend.",
                "addedRows": 0,
            }

        audit: Dict[str, Any] = {
            "provider": "sportradar",
            "targetDay": target_day,
            "dryRun": dry_run,
            "status": "ok",
            "errors": [],
            "warnings": [],
            "counts": {
                "daily_total_summaries": 0,
                "atp_men_singles_summaries": 0,
                "finished_with_winner": 0,
                "skipped_not_finished": 0,
                "skipped_missing_competitors": 0,
                "skipped_missing_score": 0,
                "skipped_invalid_score": 0,
                "skipped_duplicate": 0,
                "rows_prepared": 0,
                "rows_added": 0,
            },
            "addedSample": [],
            "skippedSample": [],
        }

        try:
            daily = self.client.daily_summaries(target_day)
            rankings = self.client.rankings()
        except SportradarError as exc:
            audit["status"] = "error"
            audit["errors"].append(str(exc))
            return audit

        points_by_id = _build_points_by_id(rankings)
        summaries = _extract_summaries(daily)
        audit["counts"]["daily_total_summaries"] = len(summaries)
        atp_summaries = [s for s in summaries if _is_atp_men_singles(s)]
        audit["counts"]["atp_men_singles_summaries"] = len(atp_summaries)

        season_ids = sorted({_s(_season(s).get("id")) for s in atp_summaries if _s(_season(s).get("id"))})
        surface_by_season: Dict[str, str] = {}
        for sid in season_ids:
            try:
                surface_by_season[sid] = _extract_surface_from_payload(self.client.season_info(sid)) or ""
            except SportradarError as exc:
                audit["warnings"].append(f"surface missing for {sid}: {exc}")
                surface_by_season[sid] = ""

        existing = _existing_keys(self.csv_path)
        rows_to_add: List[Dict[str, str]] = []

        for idx, summary in enumerate(atp_summaries, start=1):
            if not _is_finished(summary):
                audit["counts"]["skipped_not_finished"] += 1
                continue

            winner = _winner_id(summary)
            if not winner:
                audit["counts"]["skipped_not_finished"] += 1
                continue
            audit["counts"]["finished_with_winner"] += 1

            competitors = _extract_competitors(summary)
            if len(competitors) < 2:
                audit["counts"]["skipped_missing_competitors"] += 1
                audit["skippedSample"].append({"eventId": _event_id(summary), "reason": "missing_competitors"})
                continue

            winner_comp = next((c for c in competitors if c.get("id") == winner), None)
            loser_comp = next((c for c in competitors if c.get("id") != winner), None)
            if not winner_comp or not loser_comp:
                audit["counts"]["skipped_missing_competitors"] += 1
                audit["skippedSample"].append({"eventId": _event_id(summary), "reason": "winner_or_loser_not_resolved"})
                continue

            score = _format_score(summary, competitors)
            if not score:
                audit["counts"]["skipped_missing_score"] += 1
                audit["skippedSample"].append({"eventId": _event_id(summary), "reason": "missing_score"})
                continue
            if any(token in score.upper() for token in INVALID_SCORE_TOKENS):
                audit["counts"]["skipped_invalid_score"] += 1
                continue

            start = _event_start(summary)
            if start is None:
                audit["warnings"].append(f"start_time absent: {_event_id(summary)}")
                continue
            tourney_date = start.strftime("%Y%m%d")

            comp = _competition(summary)
            season = _season(summary)
            season_id = _s(season.get("id"))
            tourney_name = _s(season.get("name") or comp.get("name") or "Sportradar ATP")
            surface = surface_by_season.get(season_id) or "Hard"
            round_code = _map_round(_round_name(summary))
            event_id = _event_id(summary)

            win_rank = points_by_id.get(winner_comp["id"], {})
            lose_rank = points_by_id.get(loser_comp["id"], {})

            key = (
                tourney_date,
                _canon(tourney_name),
                _canon(winner_comp["name"]),
                _canon(loser_comp["name"]),
                score,
            )
            if key in existing:
                audit["counts"]["skipped_duplicate"] += 1
                continue
            existing.add(key)

            row = {k: "" for k in CSV_HEADER}
            row.update({
                "tourney_id": f"sr-{season_id.replace(':', '_') or target_day}",
                "tourney_name": tourney_name,
                "surface": surface,
                "draw_size": "",
                "tourney_level": _tourney_level(tourney_name),
                "indoor": "",
                "tourney_date": tourney_date,
                "match_num": str(_match_num_from_event_id(event_id, idx)),
                "winner_id": winner_comp["id"],
                "winner_name": winner_comp["name"],
                "winner_ioc": winner_comp.get("country_code", ""),
                "winner_rank": str(win_rank.get("rank", "") or ""),
                "winner_rank_points": str(win_rank.get("points", "") or ""),
                "loser_id": loser_comp["id"],
                "loser_name": loser_comp["name"],
                "loser_ioc": loser_comp.get("country_code", ""),
                "loser_rank": str(lose_rank.get("rank", "") or ""),
                "loser_rank_points": str(lose_rank.get("points", "") or ""),
                "score": score,
                "best_of": _best_of(tourney_name, round_code, score),
                "round": round_code,
            })
            rows_to_add.append(row)
            if len(audit["addedSample"]) < 10:
                audit["addedSample"].append({
                    "eventId": event_id,
                    "tourney": tourney_name,
                    "winner": winner_comp["name"],
                    "loser": loser_comp["name"],
                    "score": score,
                    "round": round_code,
                })

        audit["counts"]["rows_prepared"] = len(rows_to_add)

        if rows_to_add and not dry_run:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if self.csv_path.exists():
                backup = self.output_dir / f"2026_backup_before_{target_day}.csv"
                if not backup.exists():
                    shutil.copy2(self.csv_path, backup)
            write_header = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
            with self.csv_path.open("a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADER, extrasaction="ignore")
                if write_header:
                    writer.writeheader()
                for row in rows_to_add:
                    writer.writerow(row)
            audit["counts"]["rows_added"] = len(rows_to_add)
        else:
            audit["counts"]["rows_added"] = 0 if dry_run else len(rows_to_add)

        audit_path = self.output_dir / f"results2026_sync_{target_day}.json"
        audit["auditPath"] = str(audit_path)
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

        return audit
