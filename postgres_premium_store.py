from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

STAKE_EUR = 100.0


def _s(value: Any) -> str:
    return str(value or "").strip()


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", ".").strip())
    except Exception:
        return default


def _canon(value: Any) -> str:
    text = _s(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def same_player(a: str, b: str) -> bool:
    na = _canon(a)
    nb = _canon(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    pa = na.split()
    pb = nb.split()
    if not pa or not pb:
        return False
    if len(pa[-1]) >= 4 and pa[-1] == pb[-1]:
        return True
    if set(pa) == set(pb):
        return True
    return False


def premium_history_key(day: str, sport_event_id: str, source_a: str, source_b: str, predicted: str) -> str:
    if sport_event_id:
        return f"{day}__{sport_event_id}__pick_{_canon(predicted)}"
    pair = sorted([_canon(source_a), _canon(source_b)])
    return f"{day}__{pair[0]}__{pair[1]}__pick_{_canon(predicted)}"


def today_paris() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:
        return date.today()


def parse_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value))
    except Exception:
        return None


class PostgresPremiumStore:
    """Persistent premium-pick history for Tennis Motor.

    This store is separate from tennis_results_2026. It tracks only Premium jouable picks,
    and settles them from Sportradar winner_id when the match is closed.
    """

    TABLE = "tennis_premium_history"

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = (database_url or os.environ.get("DATABASE_URL") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.database_url)

    def _connect(self):
        if not self.enabled:
            raise RuntimeError("DATABASE_URL absente")
        try:
            import psycopg
        except Exception as exc:
            raise RuntimeError("Dépendance PostgreSQL manquante. Ajoute psycopg[binary] dans requirements.txt.") from exc
        return psycopg.connect(self.database_url, connect_timeout=10)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE} (
                        id TEXT PRIMARY KEY,
                        date TEXT NOT NULL,
                        sport_event_id TEXT,
                        source TEXT NOT NULL DEFAULT 'sportradar',
                        source_player_a TEXT NOT NULL,
                        source_player_b TEXT NOT NULL,
                        predicted_winner TEXT NOT NULL,
                        opponent TEXT NOT NULL,
                        surface TEXT,
                        premium_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'PREMIUM',
                        veto TEXT,
                        decision TEXT,
                        odd_predicted TEXT,
                        odd_opponent TEXT,
                        odds_source TEXT,
                        result TEXT NOT NULL DEFAULT 'pending',
                        real_winner TEXT,
                        settled_at TEXT,
                        settle_source TEXT,
                        tournament TEXT,
                        season_name TEXT,
                        round TEXT,
                        start_time TEXT,
                        score TEXT,
                        winner_id TEXT,
                        sportradar_player_a_id TEXT,
                        sportradar_player_b_id TEXT,
                        raw_json TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_date ON {self.TABLE}(date)
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_result ON {self.TABLE}(result)
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_sport_event_id ON {self.TABLE}(sport_event_id)
                    """
                )
            conn.commit()

    def status(self) -> Dict[str, Any]:
        if not self.enabled:
            return {
                "databaseConfigured": False,
                "databaseStatus": "not_configured",
                "table": self.TABLE,
                "error": "DATABASE_URL absente dans le service web.",
            }
        try:
            self.ensure_schema()
            counts = self.counts()
            return {
                "status": "ok",
                "databaseConfigured": True,
                "databaseStatus": "ok",
                "table": self.TABLE,
                "counts": counts,
                "policy": "PostgreSQL persistent store for Premium history, separate from Elo/results2026",
            }
        except Exception as exc:
            return {
                "status": "error",
                "databaseConfigured": True,
                "databaseStatus": "error",
                "table": self.TABLE,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def counts(self) -> Dict[str, int]:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE}")
                total = int(cur.fetchone()[0] or 0)
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE} WHERE result = 'pending'")
                pending = int(cur.fetchone()[0] or 0)
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE} WHERE result = 'win'")
                wins = int(cur.fetchone()[0] or 0)
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE} WHERE result = 'loss'")
                losses = int(cur.fetchone()[0] or 0)
        return {"total": total, "pending": pending, "wins": wins, "losses": losses, "settled": wins + losses}

    def fetch_rows(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        self.ensure_schema()
        sql = f"""
            SELECT id, date, sport_event_id, source_player_a, source_player_b,
                   predicted_winner, opponent, surface, premium_pct, status, veto,
                   decision, odd_predicted, odd_opponent, odds_source, result,
                   real_winner, settled_at, settle_source, tournament, season_name,
                   round, start_time, score, winner_id, sportradar_player_a_id,
                   sportradar_player_b_id, raw_json
            FROM {self.TABLE}
            ORDER BY date DESC, created_at DESC
        """
        params: Tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT %s"
            params = (int(limit),)
        out: List[Dict[str, Any]] = []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                for db_row in cur.fetchall():
                    row = dict(zip(cols, db_row))
                    raw_json = row.pop("raw_json", "")
                    try:
                        raw = json.loads(raw_json) if raw_json else {}
                    except Exception:
                        raw = {}
                    row["raw"] = raw
                    # Backward-compatible field names for Unity/history screens.
                    row["sourcePlayerA"] = row.pop("source_player_a")
                    row["sourcePlayerB"] = row.pop("source_player_b")
                    row["predictedWinner"] = row.pop("predicted_winner")
                    row["premiumPct"] = row.pop("premium_pct")
                    row["oddPredicted"] = row.pop("odd_predicted")
                    row["oddOpponent"] = row.pop("odd_opponent")
                    row["oddsSource"] = row.pop("odds_source")
                    row["realWinner"] = row.pop("real_winner")
                    row["settledAt"] = row.pop("settled_at")
                    row["settleSource"] = row.pop("settle_source")
                    row["seasonName"] = row.pop("season_name")
                    row["startTime"] = row.pop("start_time")
                    row["winnerId"] = row.pop("winner_id")
                    row["sportradarSportEventId"] = row.pop("sport_event_id")
                    row["sportradarPlayerAId"] = row.pop("sportradar_player_a_id")
                    row["sportradarPlayerBId"] = row.pop("sportradar_player_b_id")
                    out.append(row)
        return out

    def upsert_premium_row(self, row: Dict[str, Any]) -> str:
        """Insert/update a Premium row without overwriting a settled result.

        Returns: inserted | updated | kept_settled
        """
        self.ensure_schema()
        row_id = _s(row.get("id"))
        if not row_id:
            raise ValueError("premium row id absent")

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT result FROM {self.TABLE} WHERE id = %s", (row_id,))
                existing = cur.fetchone()
                if existing and _s(existing[0]) in {"win", "loss"}:
                    return "kept_settled"

                params = (
                    row_id,
                    _s(row.get("date")),
                    _s(row.get("sportradarSportEventId")),
                    _s(row.get("source")) or "sportradar",
                    _s(row.get("sourcePlayerA")),
                    _s(row.get("sourcePlayerB")),
                    _s(row.get("predictedWinner")),
                    _s(row.get("opponent")),
                    _s(row.get("surface")),
                    _f(row.get("premiumPct")),
                    _s(row.get("status")) or "PREMIUM",
                    _s(row.get("veto")),
                    _s(row.get("decision")),
                    _s(row.get("oddPredicted")),
                    _s(row.get("oddOpponent")),
                    _s(row.get("oddsSource")),
                    _s(row.get("result")) or "pending",
                    _s(row.get("realWinner")),
                    _s(row.get("settledAt")),
                    _s(row.get("settleSource")),
                    _s(row.get("tournament")),
                    _s(row.get("seasonName")),
                    _s(row.get("round")),
                    _s(row.get("startTime")),
                    _s(row.get("score")),
                    _s(row.get("winnerId")),
                    _s(row.get("sportradarPlayerAId")),
                    _s(row.get("sportradarPlayerBId")),
                    json.dumps(row.get("raw") or row, ensure_ascii=False),
                )
                cur.execute(
                    f"""
                    INSERT INTO {self.TABLE} (
                        id, date, sport_event_id, source, source_player_a, source_player_b,
                        predicted_winner, opponent, surface, premium_pct, status, veto,
                        decision, odd_predicted, odd_opponent, odds_source, result, real_winner,
                        settled_at, settle_source, tournament, season_name, round, start_time,
                        score, winner_id, sportradar_player_a_id, sportradar_player_b_id, raw_json
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        source = EXCLUDED.source,
                        source_player_a = EXCLUDED.source_player_a,
                        source_player_b = EXCLUDED.source_player_b,
                        predicted_winner = EXCLUDED.predicted_winner,
                        opponent = EXCLUDED.opponent,
                        surface = EXCLUDED.surface,
                        premium_pct = EXCLUDED.premium_pct,
                        status = EXCLUDED.status,
                        veto = EXCLUDED.veto,
                        decision = EXCLUDED.decision,
                        odd_predicted = EXCLUDED.odd_predicted,
                        odd_opponent = EXCLUDED.odd_opponent,
                        odds_source = EXCLUDED.odds_source,
                        tournament = EXCLUDED.tournament,
                        season_name = EXCLUDED.season_name,
                        round = EXCLUDED.round,
                        start_time = EXCLUDED.start_time,
                        score = EXCLUDED.score,
                        winner_id = EXCLUDED.winner_id,
                        sportradar_player_a_id = EXCLUDED.sportradar_player_a_id,
                        sportradar_player_b_id = EXCLUDED.sportradar_player_b_id,
                        raw_json = EXCLUDED.raw_json,
                        updated_at = NOW()
                    """,
                    params,
                )
            conn.commit()
        return "updated" if existing else "inserted"

    def settle_pending_by_event(self, sport_event_id: str, real_winner: str, score: str, winner_id: str) -> bool:
        if not sport_event_id or not real_winner:
            return False
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, predicted_winner FROM {self.TABLE}
                    WHERE sport_event_id = %s AND result = 'pending'
                    """,
                    (sport_event_id,),
                )
                rows = cur.fetchall()
                changed = 0
                for row_id, predicted in rows:
                    result = "win" if same_player(_s(predicted), real_winner) else "loss"
                    cur.execute(
                        f"""
                        UPDATE {self.TABLE}
                        SET result = %s,
                            real_winner = %s,
                            settled_at = %s,
                            settle_source = 'sportradar',
                            score = COALESCE(NULLIF(%s, ''), score),
                            winner_id = COALESCE(NULLIF(%s, ''), winner_id),
                            updated_at = NOW()
                        WHERE id = %s AND result = 'pending'
                        """,
                        (result, real_winner, today_paris().isoformat(), score, winner_id, row_id),
                    )
                    changed += cur.rowcount
            conn.commit()
        return changed > 0


    def reset_all(self) -> Dict[str, Any]:
        """Delete all Premium history rows from PostgreSQL.

        This is intentionally separate from tennis_results_2026 and only affects
        the tennis_premium_history table.
        """
        if not self.enabled:
            return {
                "status": "error",
                "databaseConfigured": False,
                "databaseStatus": "not_configured",
                "table": self.TABLE,
                "error": "DATABASE_URL absente dans le service web.",
            }
        self.ensure_schema()
        before = self.counts()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self.TABLE}")
                deleted = int(cur.rowcount or 0)
            conn.commit()
        after = self.counts()
        return {
            "status": "ok",
            "databaseConfigured": True,
            "databaseStatus": "ok",
            "table": self.TABLE,
            "deletedRows": deleted,
            "countsBefore": before,
            "countsAfter": after,
            "policy": "Reset Premium uniquement : tennis_results_2026 / Elo ne sont pas touchés.",
        }

    def summary(self) -> Dict[str, Any]:
        rows = self.fetch_rows(limit=None)
        return build_premium_summary(rows)


def stats_for_period(rows: List[Dict[str, Any]], period_days: Optional[int]) -> Dict[str, Any]:
    today = today_paris()
    selected: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "PREMIUM":
            continue
        d = parse_date(row.get("date"))
        if d is None:
            continue
        if period_days is not None and d < today - timedelta(days=period_days - 1):
            continue
        selected.append(row)

    settled = [r for r in selected if r.get("result") in {"win", "loss"}]
    wins = sum(1 for r in settled if r.get("result") == "win")
    losses = sum(1 for r in settled if r.get("result") == "loss")
    pending = sum(1 for r in selected if r.get("result") == "pending")
    total = wins + losses
    win_rate = round((wins / total) * 100.0, 2) if total else 0.0

    profit_units = 0.0
    odds_used = 0
    for row in settled:
        odd = _f(row.get("oddPredicted"), 0.0)
        if odd <= 1.0:
            continue
        odds_used += 1
        profit_units += (odd - 1.0) if row.get("result") == "win" else -1.0

    roi = round((profit_units / odds_used) * 100.0, 2) if odds_used else 0.0
    return {
        "trackedPremium": len(selected),
        "settled": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "winRate": win_rate,
        "profitUnits": round(profit_units, 3),
        "roiPct": roi,
        "oddsUsed": odds_used,
    }


def build_premium_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_day: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row.get("status") != "PREMIUM":
            continue
        d = _s(row.get("date"))
        if not d:
            continue
        bucket = by_day.setdefault(d, {
            "date": d,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "winRate": 0.0,
            "profitUnits": 0.0,
            "profitEur": 0.0,
            "hadPremiumToday": True,
            "hadPremiumSettledToday": False,
        })
        odd = _f(row.get("oddPredicted"), 0.0)
        result = row.get("result")
        if result == "win":
            bucket["wins"] += 1
            bucket["hadPremiumSettledToday"] = True
            if odd > 1.0:
                bucket["profitUnits"] += odd - 1.0
                bucket["profitEur"] += STAKE_EUR * (odd - 1.0)
        elif result == "loss":
            bucket["losses"] += 1
            bucket["hadPremiumSettledToday"] = True
            if odd > 1.0:
                bucket["profitUnits"] -= 1.0
            bucket["profitEur"] -= STAKE_EUR
        else:
            bucket["pending"] += 1

    days = []
    for bucket in by_day.values():
        settled = bucket["wins"] + bucket["losses"]
        bucket["winRate"] = round((bucket["wins"] / settled) * 100.0, 2) if settled else 0.0
        bucket["profitUnits"] = round(bucket["profitUnits"], 3)
        bucket["profitEur"] = round(bucket["profitEur"], 2)
        days.append(bucket)
    days.sort(key=lambda x: x["date"])

    cumulative_days: List[Dict[str, Any]] = []
    cw = cl = 0
    cpu = cpe = 0.0
    for bucket in days:
        cw += int(bucket.get("wins") or 0)
        cl += int(bucket.get("losses") or 0)
        cpu += float(bucket.get("profitUnits") or 0.0)
        cpe += float(bucket.get("profitEur") or 0.0)
        settled = cw + cl
        cumulative_days.append({
            "date": bucket["date"],
            "cumulativeWins": cw,
            "cumulativeLosses": cl,
            "cumulativeSettled": settled,
            "cumulativeWinRate": round((cw / settled) * 100.0, 2) if settled else 0.0,
            "cumulativeProfitUnits": round(cpu, 3),
            "cumulativeProfitEur": round(cpe, 2),
            "pendingThatDay": int(bucket.get("pending") or 0),
            "hadPremiumToday": bool(bucket.get("hadPremiumToday", False)),
            "hadPremiumSettledToday": bool(bucket.get("hadPremiumSettledToday", False)),
        })

    return {
        "status": "ok",
        "storage": {"mode": "postgres", "table": PostgresPremiumStore.TABLE},
        "summary": {
            "day": stats_for_period(rows, 1),
            "week": stats_for_period(rows, 7),
            "month": stats_for_period(rows, 30),
            "year": stats_for_period(rows, 365),
            "all": stats_for_period(rows, None),
        },
        "chart": {
            "days": days,
            "cumulativeDays": cumulative_days,
            "description": "Historique Premium séparé, stocké dans PostgreSQL.",
            "stakeEur": STAKE_EUR,
            "euroAxisMin": -2000.0,
            "euroAxisMax": 2000.0,
            "winRateAxisMin": 0.0,
            "winRateAxisMax": 100.0,
        },
        "rows": rows,
    }
