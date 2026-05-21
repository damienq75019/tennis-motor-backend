from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from postgres_premium_store import PostgresPremiumStore, premium_history_key


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


def is_premium_jouable(match: Dict[str, Any]) -> bool:
    if not isinstance(match, dict):
        return False
    if match.get("nonAnalyzable") or match.get("analysisStatus") == "not_analyzed":
        return False
    if _is_veto(match):
        return False
    if _premium_pct(match) < 80.0:
        return False
    decision = _s(match.get("decision")).lower()
    if "pas jouable" in decision or "refus" in decision or "non analys" in decision:
        return False
    return True


def _is_finished(match: Dict[str, Any]) -> bool:
    winner_id = _s(match.get("winnerId"))
    if not winner_id:
        return False
    status = _s(match.get("status") or match.get("sportradarStatus")).lower()
    match_status = _s(match.get("matchStatus") or match.get("sportradarMatchStatus")).lower()
    return status in {"closed", "ended", "finished", "complete", "completed"} or match_status in {"ended", "finished", "complete", "completed"}


def _winner_name_from_match(match: Dict[str, Any]) -> str:
    winner_id = _s(match.get("winnerId"))
    a_id = _s(match.get("sportradarPlayerAId"))
    b_id = _s(match.get("sportradarPlayerBId"))
    if winner_id and a_id and winner_id == a_id:
        return _s(match.get("playerA"))
    if winner_id and b_id and winner_id == b_id:
        return _s(match.get("playerB"))
    return ""


def _build_premium_row(match: Dict[str, Any], target_day: str) -> Dict[str, Any]:
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
        "status": "PREMIUM",
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

    def sync_daily_result(self, daily_result: Dict[str, Any], target_day: str, *, dry_run: bool = False) -> Dict[str, Any]:
        matches = daily_result.get("matches") if isinstance(daily_result, dict) else []
        if not isinstance(matches, list):
            return {"status": "error", "errors": ["daily_result sans matches[]"], "targetDay": target_day}

        counts = {
            "daily_matches": len(matches),
            "premium_candidates": 0,
            "rows_prepared": 0,
            "rows_added": 0,
            "rows_updated": 0,
            "rows_kept_settled": 0,
            "ignored_non_premium": 0,
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
                "errors": ["DATABASE_URL absente : historique Premium PostgreSQL indisponible."],
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

        # 1) Record Premium jouables.
        for match in matches:
            if not isinstance(match, dict):
                counts["ignored_non_premium"] += 1
                continue
            if not is_premium_jouable(match):
                counts["ignored_non_premium"] += 1
                continue
            counts["premium_candidates"] += 1
            row = _build_premium_row(match, target_day)
            counts["rows_prepared"] += 1
            if len(added_sample) < 10:
                added_sample.append({
                    "eventId": row.get("sportradarSportEventId"),
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

        # 2) Settle pending Premiums using the same Sportradar daily payload.
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
                    counts["settled"] += 1
                    if len(settled_sample) < 10:
                        settled_sample.append({
                            "eventId": event_id,
                            "realWinner": real_winner,
                            "score": _s(match.get("score")),
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
            "policy": "Historique Premium séparé : seuls les Premium jouables sont enregistrés; règlement uniquement via winner_id Sportradar.",
        }
