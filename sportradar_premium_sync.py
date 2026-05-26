from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from api_tennis_daily_builder import ApiTennisDailyBuilder
from postgres_premium_store import PostgresPremiumStore, premium_history_key
from sportradar_client import SportradarClient, SportradarError


def _s(value: Any) -> str:
    return str(value or "").strip()


def _premium_pct(match: Dict[str, Any]) -> float:
    try:
        value = match.get("premiumPct", match.get("premium", 0.0))
        score = float(str(value).replace(",", "."))
        if 0.0 <= score <= 1.0:
            score *= 100.0
        return score
    except Exception:
        return 0.0


def _is_veto(match: Dict[str, Any]) -> bool:
    return _s(match.get("veto")).lower() in {"oui", "yes", "true", "1"}


def _is_not_analyzable(match: Dict[str, Any]) -> bool:
    return bool(match.get("nonAnalyzable")) or _s(match.get("analysisStatus")).lower() == "not_analyzed" or bool(match.get("error"))


def is_premium_jouable(match: Dict[str, Any]) -> bool:
    if not isinstance(match, dict):
        return False
    if _is_not_analyzable(match) or _is_veto(match):
        return False
    if _premium_pct(match) < 80.0:
        return False
    decision = _s(match.get("decision")).lower()
    if "pas jouable" in decision or "refus" in decision or "non analys" in decision:
        return False
    return True


def is_proche_jouable(match: Dict[str, Any]) -> bool:
    if not isinstance(match, dict):
        return False
    if _is_not_analyzable(match) or _is_veto(match):
        return False
    pct = _premium_pct(match)
    return 75.0 <= pct < 80.0



def _status_values(match: Dict[str, Any]) -> List[str]:
    values = [
        match.get("status"),
        match.get("sportradarStatus"),
        match.get("matchStatus"),
        match.get("sportradarMatchStatus"),
    ]
    raw = match.get("raw")
    if isinstance(raw, dict):
        values.extend([
            raw.get("status"),
            raw.get("sportradarStatus"),
            raw.get("matchStatus"),
            raw.get("sportradarMatchStatus"),
        ])
    return [_s(v).lower() for v in values if _s(v)]


def _is_void_match(match: Dict[str, Any]) -> bool:
    """Betting settlement safety: retired/walkover/cancelled/abandoned = void/refund."""
    statuses = set(_status_values(match))
    void_tokens = {"retired", "walkover", "abandoned", "cancelled", "canceled", "withdrawn", "forfeit", "forfeited"}
    if statuses.intersection(void_tokens):
        return True

    # Some providers encode retirement in generic text fields.
    blob = " ".join(_s(match.get(k)).lower() for k in ("reason", "statusReason", "matchStatusReason", "score"))
    return any(token in blob for token in ("retired", "walkover", "abandoned", "cancelled", "canceled", "withdrawn"))


def _summary_is_void(summary: Dict[str, Any]) -> bool:
    status = _get_status(summary)
    values = [
        status.get("status"),
        status.get("match_status"),
        status.get("status_reason"),
        status.get("match_status_reason"),
    ]
    blob = " ".join(_s(v).lower() for v in values if _s(v))
    return any(token in blob for token in ("retired", "walkover", "abandoned", "cancelled", "canceled", "withdrawn", "forfeit"))


def _is_cancelled_before_play(match: Dict[str, Any]) -> bool:
    statuses = set(_status_values(match))
    return bool(statuses.intersection({"cancelled", "canceled"})) and not _s(match.get("winnerId"))

def tracked_category(match: Dict[str, Any]) -> str:
    """Catégorie historique moteur.

    PREMIUM : score >= 80, jouable, sans veto.
    PROCHE  : 75 <= score < 80, sans veto.
    VETO    : match analysé mais bloqué par veto moteur.
    REFUSE  : match analysé, sans veto, sous 75 ou refusé sans veto.
    """
    if not isinstance(match, dict):
        return ""
    if _is_not_analyzable(match):
        return ""
    if _is_veto(match):
        return "VETO"
    if is_premium_jouable(match):
        return "PREMIUM"
    if is_proche_jouable(match):
        return "PROCHE"
    return "REFUSE"


def _is_finished(match: Dict[str, Any]) -> bool:
    winner_id = _s(match.get("winnerId"))
    if not winner_id:
        return False
    status = _s(match.get("status") or match.get("sportradarStatus")).lower()
    match_status = _s(match.get("matchStatus") or match.get("sportradarMatchStatus")).lower()
    return status in {"closed", "ended", "finished", "complete", "completed"} or match_status in {"ended", "finished", "complete", "completed", "retired"}


def _winner_name_from_match(match: Dict[str, Any]) -> str:
    winner_id = _s(match.get("winnerId"))
    a_id = _s(match.get("sportradarPlayerAId"))
    b_id = _s(match.get("sportradarPlayerBId"))
    if winner_id and a_id and winner_id == a_id:
        return _s(match.get("playerA"))
    if winner_id and b_id and winner_id == b_id:
        return _s(match.get("playerB"))
    return ""


def _get_event(summary: Dict[str, Any]) -> Dict[str, Any]:
    return summary.get("sport_event") or {}


def _get_status(summary: Dict[str, Any]) -> Dict[str, Any]:
    return summary.get("sport_event_status") or {}


def _summary_event_id(summary: Dict[str, Any]) -> str:
    return _s(_get_event(summary).get("id"))


def _summary_winner_id(summary: Dict[str, Any]) -> str:
    return _s(_get_status(summary).get("winner_id"))


def _summary_is_finished(summary: Dict[str, Any]) -> bool:
    winner_id = _summary_winner_id(summary)
    if not winner_id:
        return False
    status = _s(_get_status(summary).get("status")).lower()
    match_status = _s(_get_status(summary).get("match_status")).lower()
    return status in {"closed", "ended", "finished", "complete", "completed"} or match_status in {"ended", "finished", "complete", "completed", "retired"}


def _summary_competitors(summary: Dict[str, Any]) -> List[Dict[str, str]]:
    raw = _get_event(summary).get("competitors") or []
    out: List[Dict[str, str]] = []
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
            "qualifier": _s(item.get("qualifier") or comp.get("qualifier")),
        })
    return out


def _summary_winner_name(summary: Dict[str, Any]) -> str:
    winner_id = _summary_winner_id(summary)
    if not winner_id:
        return ""
    for comp in _summary_competitors(summary):
        if comp.get("id") == winner_id:
            return _s(comp.get("name"))
    return ""


def _home_away_ids(summary: Dict[str, Any]) -> tuple[str, str]:
    competitors = _summary_competitors(summary)
    home = ""
    away = ""
    for comp in competitors:
        q = _s(comp.get("qualifier")).lower()
        if q == "home":
            home = _s(comp.get("id"))
        elif q == "away":
            away = _s(comp.get("id"))
    if not home and competitors:
        home = _s(competitors[0].get("id"))
    if not away and len(competitors) > 1:
        away = _s(competitors[1].get("id"))
    return home, away


def _summary_score(summary: Dict[str, Any]) -> str:
    status = _get_status(summary)
    period_scores = status.get("period_scores") or []
    if not isinstance(period_scores, list) or not period_scores:
        return ""

    winner_id = _summary_winner_id(summary)
    home_id, away_id = _home_away_ids(summary)
    sets: List[str] = []

    for period in period_scores:
        if not isinstance(period, dict):
            continue
        home_score = _s(period.get("home_score"))
        away_score = _s(period.get("away_score"))
        if home_score == "" or away_score == "":
            continue
        if winner_id and away_id and winner_id == away_id:
            sets.append(f"{away_score}-{home_score}")
        else:
            sets.append(f"{home_score}-{away_score}")
    return " ".join(sets)


def _summary_map_by_event_id(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    summaries = payload.get("summaries") if isinstance(payload, dict) else []
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(summaries, list):
        return out
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        event_id = _summary_event_id(summary)
        if event_id:
            out[event_id] = summary
    return out


def _build_history_row(match: Dict[str, Any], target_day: str, category: str) -> Dict[str, Any]:
    predicted = _s(match.get("playerA"))
    opponent = _s(match.get("playerB"))
    source_a = _s(match.get("sourcePlayerA") or predicted)
    source_b = _s(match.get("sourcePlayerB") or opponent)
    event_id = _s(match.get("sportradarSportEventId"))
    row_id = premium_history_key(target_day, event_id, source_a, source_b, predicted)

    return {
        "id": row_id,
        "date": target_day,
        "sportradarSportEventId": event_id,
        "source": _s(match.get("source") or match.get("dataProvider") or "api_tennis"),
        "sourcePlayerA": source_a,
        "sourcePlayerB": source_b,
        "predictedWinner": predicted,
        "opponent": opponent,
        "surface": _s(match.get("surface")),
        "premiumPct": round(_premium_pct(match), 3),
        "status": category,
        "category": category,
        "veto": _s(match.get("veto") or "non"),
        "decision": _s(match.get("decision")),
        "oddPredicted": _s(match.get("oddA") or match.get("playerAOdd") or match.get("coteA")),
        "oddOpponent": _s(match.get("oddB") or match.get("playerBOdd") or match.get("coteB")),
        "oddsSource": _s(match.get("oddsSource")),
        "result": "void" if _is_void_match(match) else "pending",
        "realWinner": _winner_name_from_match(match) if _is_void_match(match) else "",
        "settledAt": target_day if _is_void_match(match) else "",
        "settleSource": f"{_s(match.get("source") or match.get("dataProvider") or "api_tennis")}_void" if _is_void_match(match) else "",
        "tournament": _s(match.get("tournament")),
        "seasonName": _s(match.get("seasonName")),
        "round": _s(match.get("round")),
        "startTime": _s(match.get("startTime")),
        "score": _s(match.get("score")),
        "winnerId": _s(match.get("winnerId")),
        "sportradarPlayerAId": _s(match.get("sportradarPlayerAId")),
        "sportradarPlayerBId": _s(match.get("sportradarPlayerBId")),
        "raw": match,
    }


class PremiumHistorySyncer:
    def __init__(self, store: Optional[PostgresPremiumStore] = None) -> None:
        self.store = store or PostgresPremiumStore()

    def status(self) -> Dict[str, Any]:
        return self.store.status()

    def settle_day_from_api_tennis(self, target_day: str, *, dry_run: bool = False, builder: Optional[ApiTennisDailyBuilder] = None) -> Dict[str, Any]:
        """Settle pending history rows for one day using API-Tennis fixtures/results.

        This is the normal STEP38 settlement path after migration away from Sportradar.
        It does not create new picks. It only changes existing rows:
        pending -> win/loss/void.
        """
        counts = {
            "pending_before": 0,
            "pending_with_event_id": 0,
            "api_tennis_matches": 0,
            "finished_matches": 0,
            "matched_pending_events": 0,
            "settled": 0,
            "voided": 0,
            "still_pending": 0,
            "missing_event_id": 0,
            "missing_in_api_tennis": 0,
            "not_finished": 0,
            "unresolved_winner": 0,
        }
        errors: List[str] = []
        settled_sample: List[Dict[str, Any]] = []
        unresolved_sample: List[Dict[str, Any]] = []

        if not self.store.enabled:
            return {
                "status": "error",
                "provider": "api_tennis",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": ["DATABASE_URL absente : historique moteur PostgreSQL indisponible."],
                "counts": counts,
            }

        try:
            self.store.ensure_schema()
            pending_rows = self.store.fetch_pending_rows(day=target_day)
        except Exception as exc:
            return {
                "status": "error",
                "provider": "postgres",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": [f"PostgreSQL error: {type(exc).__name__}: {exc}"],
                "counts": counts,
            }

        counts["pending_before"] = len(pending_rows)

        pending_by_event: Dict[str, List[Dict[str, Any]]] = {}
        for row in pending_rows:
            event_id = _s(row.get("sportradarSportEventId"))
            if not event_id:
                counts["missing_event_id"] += 1
                if len(unresolved_sample) < 10:
                    unresolved_sample.append({"id": row.get("id"), "reason": "missing_event_id", "pick": row.get("predictedWinner")})
                continue
            pending_by_event.setdefault(event_id, []).append(row)

        counts["pending_with_event_id"] = sum(len(v) for v in pending_by_event.values())

        builder = builder or ApiTennisDailyBuilder()
        payload = builder.build_matches_for_day(target_day)
        if payload.get("status") != "ok":
            return {
                "status": "error",
                "provider": "api_tennis",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": [str(payload.get("error") or "Erreur API-Tennis inconnue")],
                "counts": counts,
                "settledSample": settled_sample,
                "unresolvedSample": unresolved_sample,
                "audit": payload.get("audit", {}),
            }

        matches = payload.get("matches") if isinstance(payload, dict) else []
        if not isinstance(matches, list):
            matches = []
        matches_by_event: Dict[str, Dict[str, Any]] = {}
        for match in matches:
            if not isinstance(match, dict):
                continue
            event_id = _s(match.get("sportradarSportEventId") or match.get("sportEventId"))
            if event_id:
                matches_by_event[event_id] = match

        counts["api_tennis_matches"] = len(matches_by_event)

        # First pass: void/refund overrides even if the row was already settled earlier.
        for event_id, match in matches_by_event.items():
            if not _is_void_match(match):
                continue
            real_winner = _winner_name_from_match(match)
            winner_id = _s(match.get("winnerId"))
            score = _s(match.get("score"))
            if dry_run:
                changed = len(self.store.fetch_rows_by_event(event_id)) if self.store.enabled else 0
            else:
                try:
                    changed = self.store.void_rows_by_event(
                        event_id,
                        score,
                        winner_id,
                        reason="api_tennis_fixtures_void",
                        real_winner=real_winner,
                    )
                except Exception as exc:
                    errors.append(f"void event {event_id} failed: {type(exc).__name__}: {exc}")
                    changed = 0
            if changed:
                counts["voided"] += int(changed)
                if len(settled_sample) < 10:
                    settled_sample.append({
                        "eventId": event_id,
                        "result": "void",
                        "winnerId": winner_id,
                        "realWinner": real_winner,
                        "score": score,
                        "rowsChanged": int(changed),
                    })

        for event_id, rows in pending_by_event.items():
            match = matches_by_event.get(event_id)
            if not match:
                counts["missing_in_api_tennis"] += len(rows)
                if len(unresolved_sample) < 10:
                    unresolved_sample.append({"eventId": event_id, "reason": "missing_in_api_tennis", "rows": len(rows)})
                continue

            if _is_void_match(match):
                # Already handled by void pass above.
                continue

            if not _is_finished(match):
                counts["not_finished"] += len(rows)
                if len(unresolved_sample) < 10:
                    unresolved_sample.append({"eventId": event_id, "reason": "not_finished", "rows": len(rows), "status": _s(match.get("status")), "matchStatus": _s(match.get("matchStatus"))})
                continue

            counts["finished_matches"] += 1
            real_winner = _winner_name_from_match(match)
            winner_id = _s(match.get("winnerId"))
            score = _s(match.get("score"))

            if not real_winner:
                counts["unresolved_winner"] += len(rows)
                if len(unresolved_sample) < 10:
                    unresolved_sample.append({"eventId": event_id, "reason": "winner_name_not_resolved", "winnerId": winner_id})
                continue

            counts["matched_pending_events"] += len(rows)

            if dry_run:
                changed = len(rows)
            else:
                try:
                    changed = self.store.settle_pending_by_event(
                        event_id,
                        real_winner,
                        score,
                        winner_id,
                        source="api_tennis_fixtures",
                    )
                except Exception as exc:
                    errors.append(f"settle event {event_id} failed: {type(exc).__name__}: {exc}")
                    changed = 0

            counts["settled"] += int(changed)
            if changed and len(settled_sample) < 10:
                settled_sample.append({
                    "eventId": event_id,
                    "realWinner": real_winner,
                    "winnerId": winner_id,
                    "score": score,
                    "rowsChanged": int(changed),
                })

        counts["still_pending"] = max(0, counts["pending_before"] - counts["settled"] - counts["voided"])
        status = "ok" if not errors else "partial"
        return {
            "status": status,
            "provider": "api_tennis",
            "targetDay": target_day,
            "dryRun": dry_run,
            "generatedAt": datetime.utcnow().isoformat() + "Z",
            "errors": errors,
            "counts": counts,
            "settledSample": settled_sample,
            "unresolvedSample": unresolved_sample,
            "audit": payload.get("audit", {}),
            "storage": {
                "mode": "postgres",
                "databaseConfigured": self.store.enabled,
                "table": self.store.TABLE,
                "status": self.store.status(),
            },
            "policy": "Règlement STEP38 API-Tennis : pending -> win/loss via winner_id API-Tennis; retired/walkover/cancelled/abandoned -> void/remboursé.",
        }

    def settle_day_from_sportradar(self, target_day: str, *, dry_run: bool = False, client: Optional[SportradarClient] = None) -> Dict[str, Any]:
        """Settle pending Premium/Proche rows for one day using Sportradar summaries.

        This does not create new picks. It only changes existing rows from:
        result='pending' -> result='win' or result='loss'.
        """
        counts = {
            "pending_before": 0,
            "pending_with_event_id": 0,
            "sportradar_summaries": 0,
            "finished_summaries": 0,
            "matched_pending_events": 0,
            "settled": 0,
            "voided": 0,
            "still_pending": 0,
            "missing_event_id": 0,
            "missing_in_sportradar": 0,
            "not_finished": 0,
            "unresolved_winner": 0,
        }
        errors: List[str] = []
        settled_sample: List[Dict[str, Any]] = []
        unresolved_sample: List[Dict[str, Any]] = []

        if not self.store.enabled:
            return {
                "status": "error",
                "provider": "history_provider",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": ["DATABASE_URL absente : historique moteur PostgreSQL indisponible."],
                "counts": counts,
            }

        try:
            self.store.ensure_schema()
            pending_rows = self.store.fetch_pending_rows(day=target_day)
        except Exception as exc:
            return {
                "status": "error",
                "provider": "postgres",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": [f"PostgreSQL error: {type(exc).__name__}: {exc}"],
                "counts": counts,
            }

        counts["pending_before"] = len(pending_rows)

        pending_by_event: Dict[str, List[Dict[str, Any]]] = {}
        for row in pending_rows:
            event_id = _s(row.get("sportradarSportEventId"))
            if not event_id:
                counts["missing_event_id"] += 1
                if len(unresolved_sample) < 10:
                    unresolved_sample.append({"id": row.get("id"), "reason": "missing_event_id", "pick": row.get("predictedWinner")})
                continue
            pending_by_event.setdefault(event_id, []).append(row)

        counts["pending_with_event_id"] = sum(len(v) for v in pending_by_event.values())

        client = client or SportradarClient()
        try:
            payload = client.daily_summaries(target_day)
        except SportradarError as exc:
            return {
                "status": "error",
                "provider": "history_provider",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": [str(exc)],
                "counts": counts,
                "settledSample": settled_sample,
                "unresolvedSample": unresolved_sample,
            }
        except Exception as exc:
            return {
                "status": "error",
                "provider": "history_provider",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "counts": counts,
                "settledSample": settled_sample,
                "unresolvedSample": unresolved_sample,
            }

        summaries_by_event = _summary_map_by_event_id(payload)
        counts["sportradar_summaries"] = len(summaries_by_event)

        # First pass: void/refund events must override even old rows already marked win/loss.
        for event_id, summary in summaries_by_event.items():
            if not _summary_is_void(summary):
                continue
            real_winner = _summary_winner_name(summary)
            winner_id = _summary_winner_id(summary)
            score = _summary_score(summary)
            if dry_run:
                changed = len(self.store.fetch_rows_by_event(event_id)) if self.store.enabled else 0
            else:
                try:
                    changed = self.store.void_rows_by_event(
                        event_id,
                        score,
                        winner_id,
                        reason="provider_daily_summaries_void",
                        real_winner=real_winner,
                    )
                except TypeError:
                    # Backward fallback if an older store signature is somehow loaded.
                    changed = 0
                except Exception as exc:
                    errors.append(f"void event {event_id} failed: {type(exc).__name__}: {exc}")
                    changed = 0
            if changed:
                counts["voided"] += int(changed)
                if len(settled_sample) < 10:
                    settled_sample.append({
                        "eventId": event_id,
                        "result": "void",
                        "winnerId": winner_id,
                        "score": score,
                        "rowsChanged": int(changed),
                    })

        for event_id, rows in pending_by_event.items():
            summary = summaries_by_event.get(event_id)
            if not summary:
                counts["missing_in_sportradar"] += len(rows)
                if len(unresolved_sample) < 10:
                    unresolved_sample.append({"eventId": event_id, "reason": "missing_in_sportradar", "rows": len(rows)})
                continue

            if _summary_is_void(summary):
                # Already handled by the void pass above.
                continue

            if not _summary_is_finished(summary):
                counts["not_finished"] += len(rows)
                if len(unresolved_sample) < 10:
                    unresolved_sample.append({"eventId": event_id, "reason": "not_finished", "rows": len(rows)})
                continue

            counts["finished_summaries"] += 1
            real_winner = _summary_winner_name(summary)
            winner_id = _summary_winner_id(summary)
            score = _summary_score(summary)

            if not real_winner:
                counts["unresolved_winner"] += len(rows)
                if len(unresolved_sample) < 10:
                    unresolved_sample.append({"eventId": event_id, "reason": "winner_name_not_resolved", "winnerId": winner_id})
                continue

            counts["matched_pending_events"] += len(rows)

            if dry_run:
                changed = len(rows)
            else:
                try:
                    changed = self.store.settle_pending_by_event(
                        event_id,
                        real_winner,
                        score,
                        winner_id,
                        source="sportradar_daily_summaries",
                    )
                except Exception as exc:
                    errors.append(f"settle event {event_id} failed: {type(exc).__name__}: {exc}")
                    changed = 0

            counts["settled"] += int(changed)
            if changed and len(settled_sample) < 10:
                settled_sample.append({
                    "eventId": event_id,
                    "realWinner": real_winner,
                    "winnerId": winner_id,
                    "score": score,
                    "rowsChanged": int(changed),
                })

        counts["still_pending"] = max(0, counts["pending_before"] - counts["settled"])
        status = "ok" if not errors else "partial"
        return {
            "status": status,
            "provider": "history_provider",
            "targetDay": target_day,
            "dryRun": dry_run,
            "generatedAt": datetime.utcnow().isoformat() + "Z",
            "errors": errors,
            "counts": counts,
            "settledSample": settled_sample,
            "unresolvedSample": unresolved_sample,
            "storage": {
                "mode": "postgres",
                "databaseConfigured": self.store.enabled,
                "table": self.store.TABLE,
                "status": self.store.status(),
            },
            "policy": "Règlement strict STEP34 : pending -> win/loss via winner_id; retired/walkover/cancelled/abandoned -> void/remboursé, même si la ligne était déjà réglée.",
        }

    def settle_pending_recent(self, *, days_back: int = 0, dry_run: bool = False, client: Optional[SportradarClient] = None, provider: str = "api_tennis") -> Dict[str, Any]:
        # STEP34: days_back=0 means no calendar limit: settle every pending history date, year after year.
        days_back = max(0, min(int(days_back or 0), 36500))
        counts = {
            "days_requested": days_back,
            "scope": "all_pending_dates" if days_back == 0 else "recent_history_dates",
            "dates_with_history": 0,
            "settled": 0,
            "voided": 0,
            "pending_before": 0,
            "errors": 0,
        }
        results: List[Dict[str, Any]] = []

        if not self.store.enabled:
            return {
                "status": "error",
                "provider": "history_provider",
                "dryRun": dry_run,
                "errors": ["DATABASE_URL absente : historique moteur PostgreSQL indisponible."],
                "counts": counts,
                "results": results,
            }

        try:
            dates = self.store.pending_dates(days_back=days_back, limit=36500) if days_back == 0 else self.store.history_dates(days_back=days_back, limit=36500)
        except Exception as exc:
            return {
                "status": "error",
                "provider": "postgres",
                "dryRun": dry_run,
                "errors": [f"PostgreSQL error: {type(exc).__name__}: {exc}"],
                "counts": counts,
                "results": results,
            }

        counts["dates_with_history"] = len(dates)
        provider_norm = _s(provider or "api_tennis").lower()
        api_builder = ApiTennisDailyBuilder() if provider_norm in {"api_tennis", "api-tennis", "apitennis"} else None
        client = client or SportradarClient()

        for day in dates:
            if provider_norm in {"api_tennis", "api-tennis", "apitennis"}:
                result = self.settle_day_from_api_tennis(day, dry_run=dry_run, builder=api_builder)
            else:
                result = self.settle_day_from_sportradar(day, dry_run=dry_run, client=client)
            results.append(result)
            c = result.get("counts") or {}
            counts["settled"] += int(c.get("settled") or 0)
            counts["voided"] += int(c.get("voided") or 0)
            counts["pending_before"] += int(c.get("pending_before") or 0)
            if result.get("status") not in {"ok", "skipped"}:
                counts["errors"] += 1

        status = "ok" if counts["errors"] == 0 else "partial"
        return {
            "status": status,
            "provider": "api_tennis" if _s(provider or "api_tennis").lower() in {"api_tennis", "api-tennis", "apitennis"} else "sportradar",
            "dryRun": dry_run,
            "generatedAt": datetime.utcnow().isoformat() + "Z",
            "counts": counts,
            "dates": dates,
            "results": results,
            "policy": "STEP38 : si days_back=0, vérifie toutes les dates ayant des lignes pending; provider par défaut API-Tennis; pending -> win/loss et retired/walkover/cancelled/abandoned -> void/remboursé.",
        }

    def sync_daily_result(self, daily_result: Dict[str, Any], target_day: str, *, dry_run: bool = False) -> Dict[str, Any]:
        matches = daily_result.get("matches") if isinstance(daily_result, dict) else []
        if not isinstance(matches, list):
            return {"status": "error", "errors": ["daily_result sans matches[]"], "targetDay": target_day}

        counts = {
            "daily_matches": len(matches),
            "premium_candidates": 0,
            "proche_candidates": 0,
            "veto_candidates": 0,
            "refuse_candidates": 0,
            "tracked_candidates": 0,
            "rows_prepared": 0,
            "rows_added": 0,
            "rows_updated": 0,
            "rows_kept_settled": 0,
            "ignored_not_tracked": 0,
            "settled": 0,
            "voided": 0,
            "deduped_input_matches": 0,
            "unresolved_finished": 0,
        }
        errors: List[str] = []
        added_sample: List[Dict[str, Any]] = []
        settled_sample: List[Dict[str, Any]] = []

        if not self.store.enabled:
            return {
                "status": "error",
                "provider": "history_provider",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": ["DATABASE_URL absente : historique moteur PostgreSQL indisponible."],
                "counts": counts,
            }

        try:
            self.store.ensure_schema()
        except Exception as exc:
            return {
                "status": "error",
                "provider": "history_provider",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": [f"PostgreSQL error: {type(exc).__name__}: {exc}"],
                "counts": counts,
            }

        # 1) Record all categorized engine outputs: Premium + Proches + Veto + Refusés.
        # STEP34: dedupe raw daily payload by sport_event_id before writing history.
        # One physical tennis match must create at most one history row.
        # IMPORTANT: keep the FIRST engine pick seen for the event. Do not replace it
        # with a later duplicate that has a slightly higher percentage.
        deduped_matches: List[Dict[str, Any]] = []
        seen_events: Dict[str, int] = {}

        for m in matches:
            if not isinstance(m, dict):
                continue
            event_id = _s(m.get("sportradarSportEventId"))
            if not event_id:
                deduped_matches.append(m)
                continue
            if event_id not in seen_events:
                seen_events[event_id] = len(deduped_matches)
                deduped_matches.append(m)
                continue
            # Duplicate physical match: keep the original/first row.
            # This avoids flipping the pick when Sportradar/Flashscore returns
            # the same event twice with reversed orientation.
            counts["deduped_input_matches"] += 1
            continue

        for match in deduped_matches:
            if not isinstance(match, dict):
                counts["ignored_not_tracked"] += 1
                continue

            category = tracked_category(match)
            if not category:
                # Cancelled/retired non-analyzable rows are not engine picks.
                counts["ignored_not_tracked"] += 1
                continue

            if category == "PREMIUM":
                counts["premium_candidates"] += 1
            elif category == "PROCHE":
                counts["proche_candidates"] += 1
            elif category == "VETO":
                counts["veto_candidates"] += 1
            elif category == "REFUSE":
                counts["refuse_candidates"] += 1
            counts["tracked_candidates"] += 1

            row = _build_history_row(match, target_day, category)
            counts["rows_prepared"] += 1

            if len(added_sample) < 10:
                added_sample.append({
                    "eventId": row.get("sportradarSportEventId"),
                    "category": category,
                    "pick": row.get("predictedWinner"),
                    "opponent": row.get("opponent"),
                    "premiumPct": row.get("premiumPct"),
                    "tournament": row.get("tournament"),
                    "round": row.get("round"),
                })

            if dry_run:
                continue

            try:
                action = self.store.upsert_premium_row(row)
                if action == "inserted":
                    counts["rows_added"] += 1
                elif action == "updated":
                    counts["rows_updated"] += 1
                elif action in {"kept_settled", "kept_existing_pick"}:
                    counts["rows_kept_settled"] += 1
            except Exception as exc:
                errors.append(f"upsert failed: {type(exc).__name__}: {exc}")

        # 2) Settle tracked rows using the same Sportradar daily payload.
        # Retired / walkover / cancelled / abandoned must be void/refunded, not win/loss.
        for match in deduped_matches:
            if not isinstance(match, dict):
                continue
            event_id = _s(match.get("sportradarSportEventId"))
            if not event_id:
                continue

            if _is_void_match(match):
                if dry_run:
                    changed = len(self.store.fetch_rows_by_event(event_id)) if self.store.enabled else 0
                else:
                    try:
                        changed = self.store.void_rows_by_event(
                            event_id,
                            _s(match.get("score")),
                            _s(match.get("winnerId")),
                            reason=f"{_s(match.get("source") or match.get("dataProvider") or "provider")}_daily_void",
                            real_winner=_winner_name_from_match(match),
                        )
                    except Exception as exc:
                        errors.append(f"void failed: {type(exc).__name__}: {exc}")
                        changed = 0
                if changed:
                    counts["voided"] += int(changed)
                    if len(settled_sample) < 10:
                        settled_sample.append({
                            "eventId": event_id,
                            "result": "void",
                            "score": _s(match.get("score")),
                            "rowsChanged": int(changed),
                        })
                continue

            if not _is_finished(match):
                continue

            real_winner = _winner_name_from_match(match)
            if not real_winner:
                counts["unresolved_finished"] += 1
                continue

            if dry_run:
                continue
            try:
                changed = self.store.settle_pending_by_event(
                    event_id,
                    real_winner,
                    _s(match.get("score")),
                    _s(match.get("winnerId")),
                )
                if changed:
                    counts["settled"] += int(changed)
                    if len(settled_sample) < 10:
                        settled_sample.append({
                            "eventId": event_id,
                            "realWinner": real_winner,
                            "score": _s(match.get("score")),
                            "rowsChanged": int(changed),
                        })
            except Exception as exc:
                errors.append(f"settle failed: {type(exc).__name__}: {exc}")

        status = "ok" if not errors else "partial"
        return {
            "status": status,
            "provider": "history_provider",
            "targetDay": target_day,
            "dryRun": dry_run,
            "generatedAt": datetime.utcnow().isoformat() + "Z",
            "errors": errors,
            "counts": counts,
            "addedSample": added_sample,
            "settledSample": settled_sample,
            "storage": {
                "mode": "postgres",
                "databaseConfigured": self.store.enabled,
                "table": self.store.TABLE,
                "status": self.store.status(),
            },
            "daily": {
                "payloadCount": (daily_result.get("daily") or {}).get("payloadCount"),
                "step": (daily_result.get("daily") or {}).get("step"),
                "summary": daily_result.get("summary", {}),
            },
            "policy": "Historique moteur STEP36 : Premium/Proches/Veto/Refusés; 1 ligne par match; retired/walkover/cancelled/abandoned -> void/remboursé; règlement win/loss via winner_id provider.",
        }
