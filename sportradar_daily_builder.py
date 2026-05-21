from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sportradar_client import SportradarClient, SportradarError


def _s(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _s(value).lower()


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


def _parse_time(value: Any) -> Optional[dt.datetime]:
    raw = _s(value)
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def _surface_to_engine(value: Any) -> str:
    text = _lower(value)
    if not text:
        return ""
    if "clay" in text:
        return "Clay"
    if "grass" in text:
        return "Grass"
    if "hard" in text or "carpet" in text:
        return "Hard"
    return "Hard"


def _extract_surface_from_payload(payload: Dict[str, Any]) -> str:
    candidates: List[str] = []
    for path, value in _walk(payload):
        if isinstance(value, (dict, list)):
            continue
        key = path.split(".")[-1].lower()
        if "surface" in key or "court" in key:
            candidates.append(_s(value))

    for value in candidates:
        mapped = _surface_to_engine(value)
        if mapped:
            return mapped
    return ""


def _extract_summaries(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    summaries = payload.get("summaries")
    if isinstance(summaries, list):
        return [x for x in summaries if isinstance(x, dict)]
    return []


def _extract_competitors(sport_event: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = sport_event.get("competitors") or []
    competitors: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return competitors

    for item in raw:
        if not isinstance(item, dict):
            continue
        comp = item.get("competitor") if isinstance(item.get("competitor"), dict) else item
        if not isinstance(comp, dict):
            continue
        competitors.append({
            "id": _s(comp.get("id")),
            "name": _s(comp.get("name")),
            "country": _s(comp.get("country")),
            "country_code": _s(comp.get("country_code")),
            # Attention : ce champ est souvent home/away, pas issu des qualifications.
            "sr_qualifier_field": _s(item.get("qualifier") or comp.get("qualifier")),
            "seed": _s(item.get("seed") or comp.get("seed")),
            "raw": item,
        })
    return competitors


def _is_atp_men_singles(summary: Dict[str, Any]) -> bool:
    ctx = ((summary.get("sport_event") or {}).get("sport_event_context") or {})
    category = _lower(((ctx.get("category") or {}).get("name")))
    competition = ctx.get("competition") or {}
    comp_type = _lower(competition.get("type"))
    gender = _lower(competition.get("gender"))
    return category == "atp" and comp_type == "singles" and gender == "men"


def _is_placeholder_player(name: str) -> bool:
    n = _s(name)
    if not n:
        return True
    return bool(re.fullmatch(r"(?i)(qf|sf|pf|qualifier|winner|loser|bye)\s*\d*", n))


def _build_points_by_id(rankings_payload: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    points_by_id: Dict[str, Dict[str, Any]] = {}
    debug = {
        "rankings_total": 0,
        "atp_men_rankings_seen": 0,
        "atp_entries_seen": 0,
        "sample_top_5": [],
    }

    rankings = rankings_payload.get("rankings") or []
    if not isinstance(rankings, list):
        return points_by_id, debug

    debug["rankings_total"] = len(rankings)

    for ranking in rankings:
        if not isinstance(ranking, dict):
            continue
        name = _lower(ranking.get("name"))
        gender = _lower(ranking.get("gender"))
        if name != "atp" or gender != "men":
            continue

        debug["atp_men_rankings_seen"] += 1
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
            record = {
                "name": _s(comp.get("name")),
                "rank": _safe_int(entry.get("rank"), 0) or 0,
                "points": _safe_int(entry.get("points"), None),
                "country": _s(comp.get("country")),
                "country_code": _s(comp.get("country_code")),
            }
            points_by_id[cid] = record
            debug["atp_entries_seen"] += 1
            if len(debug["sample_top_5"]) < 5:
                debug["sample_top_5"].append(record)

    return points_by_id, debug


def _get_status(summary: Dict[str, Any]) -> Dict[str, Any]:
    return summary.get("sport_event_status") or {}


def _get_event(summary: Dict[str, Any]) -> Dict[str, Any]:
    return summary.get("sport_event") or {}


def _event_id(summary: Dict[str, Any]) -> str:
    return _s(_get_event(summary).get("id"))


def _winner_id(summary: Dict[str, Any]) -> str:
    return _s(_get_status(summary).get("winner_id"))


def _is_finished(summary: Dict[str, Any]) -> bool:
    status = _get_status(summary)
    status_text = _lower(status.get("status"))
    match_status = _lower(status.get("match_status"))
    if status_text in {"closed", "ended", "finished", "complete", "completed"}:
        return True
    if match_status in {"ended", "finished", "complete", "completed"}:
        return True
    return bool(_winner_id(summary))


def _event_start(summary: Dict[str, Any]) -> Optional[dt.datetime]:
    return _parse_time(_get_event(summary).get("start_time"))


def _count_wins_before(
    season_summaries: List[Dict[str, Any]],
    competitor_id: str,
    current_event_id: str,
    current_start: Optional[dt.datetime],
) -> int:
    """Compte strictement les victoires acquises AVANT le match courant.

    Règle verrouillée Tennis Motor :
    - le match courant n'est jamais compté ;
    - une ligne non terminée n'est jamais comptée ;
    - une défaite n'est jamais comptée : winner_id doit être exactement le joueur ;
    - le match compté doit avoir une heure de début strictement antérieure au match courant.

    Si l'heure du match courant ou d'un match candidat est absente, on ne compte pas le candidat.
    C'est volontairement prudent : pas d'invention et pas de comptage approximatif.
    """
    if not competitor_id or current_start is None:
        return 0

    wins = 0
    for summary in season_summaries:
        candidate_event_id = _event_id(summary)
        if candidate_event_id == current_event_id:
            continue

        if not _is_finished(summary):
            continue

        if _winner_id(summary) != competitor_id:
            continue

        candidate_start = _event_start(summary)
        if candidate_start is None:
            continue

        if candidate_start >= current_start:
            continue

        wins += 1

    return wins


def _reliable_player_qualifier_status(competitor: Dict[str, Any], summary: Dict[str, Any]) -> Tuple[bool, str, str]:
    """Retourne uniquement une preuve fiable.

    On ignore volontairement sr_qualifier_field=home/away.
    Si aucune preuve fiable par joueur n'est trouvée : manual_required.
    """
    raw = competitor.get("raw") if isinstance(competitor.get("raw"), dict) else {}
    evidence: List[str] = []

    for path, value in _walk(raw):
        if isinstance(value, (dict, list)):
            continue
        low_path = path.lower()
        low_value = _lower(value)
        if low_path.endswith("qualifier") and low_value in {"home", "away", "1", "2"}:
            continue
        if low_value in {"qualifier", "qualified", "qualification", "q", "lucky loser", "lucky_loser", "ll"}:
            evidence.append(f"competitor.{path}={value}")
        if any(k in low_path for k in ["entry_type", "draw_status", "qualification_status", "lucky_loser"]):
            evidence.append(f"competitor.{path}={value}")

    name = _s(competitor.get("name"))
    if re.search(r"(?i)(\(q\)|\bq\b|\(ll\)|\bll\b)", name):
        evidence.append(f"name_marker={name}")

    if evidence:
        return True, "auto_reliable", " | ".join(evidence[:5])

    return False, "manual_required", "no_reliable_player_level_qualifier_evidence"


def _score_string(status: Dict[str, Any]) -> str:
    period_scores = status.get("period_scores") or []
    if isinstance(period_scores, list) and period_scores:
        parts: List[str] = []
        for p in period_scores:
            if not isinstance(p, dict):
                continue
            home = p.get("home_score")
            away = p.get("away_score")
            if home is not None and away is not None:
                parts.append(f"{home}-{away}")
        if parts:
            return " ".join(parts)
    home = status.get("home_score")
    away = status.get("away_score")
    if home is not None and away is not None:
        return f"{home}-{away}"
    return ""


class SportradarDailyBuilder:
    def __init__(self, client: Optional[SportradarClient] = None, *, audit_dir: Optional[Path] = None) -> None:
        self.client = client or SportradarClient()
        self.audit_dir = audit_dir

    def build_matches_for_day(self, target_day: str) -> Dict[str, Any]:
        audit: Dict[str, Any] = {
            "provider": "sportradar",
            "targetDay": target_day,
            "status": "started",
            "errors": [],
            "warnings": [],
            "counts": {},
            "tournamentWinsPolicy": "strict_before_start_finished_winner_only_no_current_match",
        }

        try:
            rankings_payload = self.client.rankings()
            points_by_id, rankings_debug = _build_points_by_id(rankings_payload)
            audit["rankings"] = rankings_debug

            daily_payload = self.client.daily_summaries(target_day)
            summaries = _extract_summaries(daily_payload)
            atp_summaries = [s for s in summaries if _is_atp_men_singles(s)]
            audit["counts"]["daily_total_summaries"] = len(summaries)
            audit["counts"]["atp_men_singles_summaries"] = len(atp_summaries)

            season_ids: List[str] = []
            for summary in atp_summaries:
                ctx = ((_get_event(summary).get("sport_event_context") or {}))
                sid = _s((ctx.get("season") or {}).get("id"))
                if sid and sid not in season_ids:
                    season_ids.append(sid)

            surface_by_season: Dict[str, str] = {}
            summaries_by_season: Dict[str, List[Dict[str, Any]]] = {}

            for sid in season_ids:
                try:
                    info = self.client.season_info(sid)
                    surface_by_season[sid] = _extract_surface_from_payload(info)
                except Exception as exc:
                    audit["warnings"].append(f"season_info_failed {sid}: {type(exc).__name__}: {exc}")
                    surface_by_season[sid] = ""

                try:
                    season_payload = self.client.season_summaries(sid, limit=200)
                    summaries_by_season[sid] = _extract_summaries(season_payload)
                except Exception as exc:
                    audit["warnings"].append(f"season_summaries_failed {sid}: {type(exc).__name__}: {exc}")
                    summaries_by_season[sid] = []

            matches: List[Dict[str, Any]] = []
            manual_points_required = 0
            placeholders = 0
            qualifier_manual_required = 0
            surfaces_missing = 0

            for summary in atp_summaries:
                event = _get_event(summary)
                ctx = event.get("sport_event_context") or {}
                competition = ctx.get("competition") or {}
                season = ctx.get("season") or {}
                round_info = ctx.get("round") or {}
                status = _get_status(summary)
                competitors = _extract_competitors(event)
                if len(competitors) < 2:
                    audit["warnings"].append(f"event_without_two_competitors {event.get('id')}")
                    continue

                a = competitors[0]
                b = competitors[1]
                event_id = _s(event.get("id"))
                season_id = _s(season.get("id"))
                current_start = _parse_time(event.get("start_time"))
                season_summaries = summaries_by_season.get(season_id, [])
                surface = surface_by_season.get(season_id, "")
                if not surface:
                    surfaces_missing += 1
                    surface = "Hard"  # fallback neutre pour affichage; match marqué audit.

                points_a_record = points_by_id.get(a["id"], {})
                points_b_record = points_by_id.get(b["id"], {})
                points_a = points_a_record.get("points")
                points_b = points_b_record.get("points")

                placeholder_a = _is_placeholder_player(a["name"])
                placeholder_b = _is_placeholder_player(b["name"])
                if placeholder_a or placeholder_b:
                    placeholders += 1

                if points_a is None or points_b is None:
                    manual_points_required += 1

                a_qual, a_qual_conf, a_qual_source = _reliable_player_qualifier_status(a, summary)
                b_qual, b_qual_conf, b_qual_source = _reliable_player_qualifier_status(b, summary)
                if b_qual_conf == "manual_required":
                    qualifier_manual_required += 1

                match = {
                    "playerA": a["name"],
                    "playerB": b["name"],
                    "surface": surface,
                    "playerAPoints": points_a,
                    "playerBPoints": points_b,
                    "player_a_is_qualifier": a_qual,
                    "player_b_is_qualifier": b_qual,
                    "player_a_tournament_wins": _count_wins_before(season_summaries, a["id"], event_id, current_start),
                    "player_b_tournament_wins": _count_wins_before(season_summaries, b["id"], event_id, current_start),
                    "player_a_qualifier_confidence": a_qual_conf,
                    "player_b_qualifier_confidence": b_qual_conf,
                    "player_a_qualifier_source": a_qual_source,
                    "player_b_qualifier_source": b_qual_source,
                    "manualReviewRequired": bool(
                        placeholder_a or placeholder_b or points_a is None or points_b is None or b_qual_conf == "manual_required"
                    ),
                    "manualReviewReasons": [],
                    "sportradarSportEventId": event_id,
                    "sportradarSeasonId": season_id,
                    "sportradarCompetitionId": _s(competition.get("id")),
                    "sportradarPlayerAId": a["id"],
                    "sportradarPlayerBId": b["id"],
                    "tournament": _s(competition.get("name")),
                    "seasonName": _s(season.get("name")),
                    "round": _s(round_info.get("name") or round_info.get("type")),
                    "startTime": _s(event.get("start_time")),
                    "status": _s(status.get("status")),
                    "matchStatus": _s(status.get("match_status")),
                    "winnerId": _s(status.get("winner_id")),
                    "score": _score_string(status),
                    "source": "sportradar",
                    "tournamentWinsPolicy": "strict_before_start_finished_winner_only_no_current_match",
                }

                if placeholder_a or placeholder_b:
                    match["manualReviewReasons"].append("placeholder_player")
                if points_a is None:
                    match["manualReviewReasons"].append("player_a_points_missing")
                if points_b is None:
                    match["manualReviewReasons"].append("player_b_points_missing")
                if b_qual_conf == "manual_required":
                    match["manualReviewReasons"].append("player_b_qualifier_manual_required")
                if surface_by_season.get(season_id, "") == "":
                    match["manualReviewReasons"].append("surface_missing_defaulted_hard")

                matches.append(match)

            audit["counts"].update({
                "built_matches": len(matches),
                "unique_seasons": len(season_ids),
                "manual_points_required": manual_points_required,
                "placeholder_matches": placeholders,
                "player_b_qualifier_manual_required": qualifier_manual_required,
                "surfaces_missing": surfaces_missing,
            })
            audit["status"] = "ok"

            if self.audit_dir:
                self.audit_dir.mkdir(parents=True, exist_ok=True)
                audit_path = self.audit_dir / f"sportradar_daily_{target_day}.json"
                audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
                audit["auditPath"] = str(audit_path)

            return {"status": "ok", "matches": matches, "audit": audit}

        except SportradarError as exc:
            audit["status"] = "error"
            audit["errors"].append(str(exc))
            return {"status": "error", "matches": [], "audit": audit, "error": str(exc)}
        except Exception as exc:
            audit["status"] = "error"
            audit["errors"].append(f"{type(exc).__name__}: {exc}")
            return {"status": "error", "matches": [], "audit": audit, "error": f"{type(exc).__name__}: {exc}"}
