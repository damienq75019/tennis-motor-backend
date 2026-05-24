from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

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
        "source": "sportradar",
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
        "result": "pending",
        "realWinner": "",
        "settledAt": "",
        "settleSource": "",
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
                "provider": "sportradar",
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
        if not pending_rows:
            return {
                "status": "ok",
                "provider": "sportradar",
                "targetDay": target_day,
                "dryRun": dry_run,
                "generatedAt": datetime.utcnow().isoformat() + "Z",
                "errors": [],
                "counts": counts,
                "settledSample": [],
                "policy": "Aucune ligne pending à régler pour cette date.",
            }

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
                "provider": "sportradar",
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
                "provider": "sportradar",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "counts": counts,
                "settledSample": settled_sample,
                "unresolvedSample": unresolved_sample,
            }

        summaries_by_event = _summary_map_by_event_id(payload)
        counts["sportradar_summaries"] = len(summaries_by_event)

        for event_id, rows in pending_by_event.items():
            summary = summaries_by_event.get(event_id)
            if not summary:
                counts["missing_in_sportradar"] += len(rows)
                if len(unresolved_sample) < 10:
                    unresolved_sample.append({"eventId": event_id, "reason": "missing_in_sportradar", "rows": len(rows)})
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
            "provider": "sportradar",
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
            "policy": "Règlement strict : seules les lignes déjà enregistrées en pending sont changées en win/loss via winner_id Sportradar.",
        }

    def settle_pending_recent(self, *, days_back: int = 7, dry_run: bool = False, client: Optional[SportradarClient] = None) -> Dict[str, Any]:
        days_back = max(1, min(int(days_back or 7), 60))
        counts = {
            "days_requested": days_back,
            "dates_with_pending": 0,
            "settled": 0,
            "pending_before": 0,
            "errors": 0,
        }
        results: List[Dict[str, Any]] = []

        if not self.store.enabled:
            return {
                "status": "error",
                "provider": "sportradar",
                "dryRun": dry_run,
                "errors": ["DATABASE_URL absente : historique moteur PostgreSQL indisponible."],
                "counts": counts,
                "results": results,
            }

        try:
            dates = self.store.pending_dates(days_back=days_back)
        except Exception as exc:
            return {
                "status": "error",
                "provider": "postgres",
                "dryRun": dry_run,
                "errors": [f"PostgreSQL error: {type(exc).__name__}: {exc}"],
                "counts": counts,
                "results": results,
            }

        counts["dates_with_pending"] = len(dates)
        client = client or SportradarClient()

        for day in dates:
            result = self.settle_day_from_sportradar(day, dry_run=dry_run, client=client)
            results.append(result)
            c = result.get("counts") or {}
            counts["settled"] += int(c.get("settled") or 0)
            counts["pending_before"] += int(c.get("pending_before") or 0)
            if result.get("status") not in {"ok", "skipped"}:
                counts["errors"] += 1

        status = "ok" if counts["errors"] == 0 else "partial"
        return {
            "status": status,
            "provider": "sportradar",
            "dryRun": dry_run,
            "generatedAt": datetime.utcnow().isoformat() + "Z",
            "counts": counts,
            "dates": dates,
            "results": results,
            "policy": "Règle les pending des derniers jours via Sportradar daily summaries; aucun nouveau pick n'est créé.",
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
            "unresolved_finished": 0,
        }
        errors: List[str] = []
        added_sample: List[Dict[str, Any]] = []
        settled_sample: List[Dict[str, Any]] = []

        if not self.store.enabled:
            return {
                "status": "error",
                "provider": "sportradar",
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
                "provider": "sportradar",
                "targetDay": target_day,
                "dryRun": dry_run,
                "errors": [f"PostgreSQL error: {type(exc).__name__}: {exc}"],
                "counts": counts,
            }

        # 1) Record all categorized engine outputs: Premium + Proches + Veto + Refusés.
        for match in matches:
            if not isinstance(match, dict):
                counts["ignored_not_tracked"] += 1
                continue

            category = tracked_category(match)
            if not category:
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
                elif action == "kept_settled":
                    counts["rows_kept_settled"] += 1
            except Exception as exc:
                errors.append(f"upsert failed: {type(exc).__name__}: {exc}")

        # 2) Settle pending tracked rows using the same Sportradar daily payload.
        for match in matches:
            if not isinstance(match, dict) or not _is_finished(match):
                continue
            real_winner = _winner_name_from_match(match)
            if not real_winner:
                counts["unresolved_finished"] += 1
                continue
            event_id = _s(match.get("sportradarSportEventId"))
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
            "provider": "sportradar",
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
            "policy": "Historique moteur catégorisé : Premium, Proches, Veto et Refusés enregistrés séparément; règlement via winner_id Sportradar.",
        }
