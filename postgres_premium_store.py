from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

STAKE_EUR = 100.0

VOID_RESULTS = {"void", "refunded", "refund", "cancelled", "canceled", "abandoned", "retired", "walkover", "withdrawn", "forfeit"}
SETTLED_RESULTS = {"win", "loss"}
FINAL_RESULTS = SETTLED_RESULTS | VOID_RESULTS


def normalize_result(value: Any) -> str:
    raw = _s(value).lower()
    if raw in {"won", "winner", "w"}:
        return "win"
    if raw in {"lost", "loser", "l"}:
        return "loss"
    if raw in VOID_RESULTS:
        return "void"
    if raw in {"pending", ""}:
        return "pending"
    return raw


def is_void_result(value: Any) -> bool:
    return normalize_result(value) == "void"


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


def premium_history_key(day: str, sport_event_id: str, source_a: str, source_b: str, predicted: str = "") -> str:
    """Stable one-row-per-match history key.

    STEP30:
    - With Sportradar event id: one row per date + event, regardless of pick/orientation.
    - Without event id: one row per date + normalized pair, regardless of pick/orientation.
    This prevents duplicated rows such as A-pick and B-pick for the same match.
    """
    if sport_event_id:
        return f"{day}__{sport_event_id}"
    pair = sorted([_canon(source_a), _canon(source_b)])
    return f"{day}__{pair[0]}__{pair[1]}"


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


VALID_HISTORY_CATEGORIES = {"PREMIUM", "PROCHE", "VETO", "REFUSE"}


def normalize_category(value: Any, default: str = "PREMIUM") -> str:
    raw = _s(value).upper()
    raw = raw.replace("É", "E").replace("È", "E").replace("Ê", "E")
    raw = raw.replace(" ", "_").replace("-", "_")
    if raw in {"PREMIUM", "PREM"}:
        return "PREMIUM"
    if raw in {"PROCHE", "PROCHES"}:
        return "PROCHE"
    if raw in {"VETO", "VETOES"}:
        return "VETO"
    if raw in {"REFUSE", "REFUS", "REFUSES", "REFUSED", "REFUSE_SANS_VETO", "REFUSES_SANS_VETO"}:
        return "REFUSE"
    if raw in {"ALL", "TOUT", "*"}:
        return "ALL"
    return default


def category_where(category: Any, column: str = "status") -> Tuple[str, List[Any]]:
    cat = normalize_category(category, default="ALL")
    if cat == "ALL":
        return "", []
    return f" AND UPPER(COALESCE({column}, 'PREMIUM')) = %s", [cat]


def row_category(row: Dict[str, Any]) -> str:
    return normalize_category(row.get("status") or row.get("category"), default="PREMIUM")


def category_label(category: Any) -> str:
    cat = normalize_category(category)
    labels = {
        "PREMIUM": "Premium",
        "PROCHE": "Proches",
        "VETO": "Veto",
        "REFUSE": "Refusés",
        "ALL": "Toutes catégories",
    }
    return labels.get(cat, cat)


class PostgresPremiumStore:
    """Persistent categorized pick history for Tennis Motor.

    Physical table kept as tennis_premium_history for zero data loss.
    Logical STEP25 model: PREMIUM / PROCHE / VETO / REFUSE categories.
    """

    TABLE = os.environ.get("TENNIS_MOTOR_HISTORY_TABLE", "tennis_premium_history")

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
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_status ON {self.TABLE}(status)
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

    def counts(self, category: Optional[str] = None) -> Dict[str, Any]:
        self.ensure_schema()
        where, params = category_where(category)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE} WHERE 1=1{where}", tuple(params))
                total = int(cur.fetchone()[0] or 0)
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE} WHERE result = 'pending'{where}", tuple(params))
                pending = int(cur.fetchone()[0] or 0)
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE} WHERE result = 'win'{where}", tuple(params))
                wins = int(cur.fetchone()[0] or 0)
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE} WHERE result = 'loss'{where}", tuple(params))
                losses = int(cur.fetchone()[0] or 0)
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE} WHERE result IN ('void','refunded','refund','cancelled','canceled','abandoned','retired','walkover','withdrawn','forfeit'){where}", tuple(params))
                voids = int(cur.fetchone()[0] or 0)

                cur.execute(f"SELECT UPPER(COALESCE(status, 'PREMIUM')), COUNT(*) FROM {self.TABLE} GROUP BY UPPER(COALESCE(status, 'PREMIUM'))")
                by_status_all = {str(k or "PREMIUM").upper(): int(v or 0) for k, v in cur.fetchall()}

                cur.execute(f"SELECT UPPER(COALESCE(status, 'PREMIUM')), COUNT(*) FROM {self.TABLE} WHERE 1=1{where} GROUP BY UPPER(COALESCE(status, 'PREMIUM'))", tuple(params))
                by_status_selected = {str(k or "PREMIUM").upper(): int(v or 0) for k, v in cur.fetchall()}

        selected_cat = normalize_category(category, default="ALL")
        return {
            "category": selected_cat,
            "total": total,
            "premium": int(by_status_selected.get("PREMIUM", 0)) if selected_cat != "ALL" else int(by_status_all.get("PREMIUM", 0)),
            "proches": int(by_status_selected.get("PROCHE", 0)) if selected_cat != "ALL" else int(by_status_all.get("PROCHE", 0)),
            "veto": int(by_status_selected.get("VETO", 0)) if selected_cat != "ALL" else int(by_status_all.get("VETO", 0)),
            "refuse": int(by_status_selected.get("REFUSE", 0)) if selected_cat != "ALL" else int(by_status_all.get("REFUSE", 0)),
            "byStatus": by_status_selected,
            "byStatusAll": by_status_all,
            "pending": pending,
            "wins": wins,
            "losses": losses,
            "void": voids,
            "voids": voids,
            "refunded": voids,
            "settled": wins + losses,
        }


    def fetch_rows(self, limit: Optional[int] = None, category: Optional[str] = None) -> List[Dict[str, Any]]:
        self.ensure_schema()
        sql = f"""
            SELECT id, date, sport_event_id, source_player_a, source_player_b,
                   predicted_winner, opponent, surface, premium_pct, status, veto,
                   decision, odd_predicted, odd_opponent, odds_source, result,
                   real_winner, settled_at, settle_source, tournament, season_name,
                   round, start_time, score, winner_id, sportradar_player_a_id,
                   sportradar_player_b_id, raw_json
            FROM {self.TABLE}
            WHERE 1=1
        """
        where, params_list = category_where(category)
        sql += where
        sql += " ORDER BY date DESC, created_at DESC"
        if limit is not None:
            sql += " LIMIT %s"
            params_list.append(int(limit))
        params: Tuple[Any, ...] = tuple(params_list)
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
                cur.execute(f"SELECT result, predicted_winner FROM {self.TABLE} WHERE id = %s", (row_id,))
                existing = cur.fetchone()
                incoming_result = normalize_result(row.get("result"))

                # Do not let a fresh pending row overwrite a final row.
                # Exception: incoming void must be allowed to correct a retired/cancelled match
                # that was previously settled as win/loss.
                if existing and normalize_result(existing[0]) in FINAL_RESULTS and incoming_result == "pending":
                    return "kept_settled"

                # STEP30: first pick wins. If the same sport_event_id comes back later
                # with reversed orientation and a different predicted winner, do not flip
                # the historical pick. This is what caused Dellien/Royer to keep Dellien
                # instead of the first Royer pick.
                if existing and normalize_result(existing[0]) == "pending" and incoming_result == "pending":
                    existing_predicted = _s(existing[1] if len(existing) > 1 else "")
                    incoming_predicted = _s(row.get("predictedWinner"))
                    if existing_predicted and incoming_predicted and not same_player(existing_predicted, incoming_predicted):
                        return "kept_existing_pick"

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
                    normalize_result(row.get("result")) or "pending",
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
                        result = CASE
                            WHEN EXCLUDED.result <> 'pending' THEN EXCLUDED.result
                            ELSE {self.TABLE}.result
                        END,
                        real_winner = CASE
                            WHEN EXCLUDED.result <> 'pending' THEN EXCLUDED.real_winner
                            ELSE {self.TABLE}.real_winner
                        END,
                        settled_at = CASE
                            WHEN EXCLUDED.result <> 'pending' THEN EXCLUDED.settled_at
                            ELSE {self.TABLE}.settled_at
                        END,
                        settle_source = CASE
                            WHEN EXCLUDED.result <> 'pending' THEN EXCLUDED.settle_source
                            ELSE {self.TABLE}.settle_source
                        END,
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

    def fetch_pending_rows(self, day: Optional[str] = None, limit: Optional[int] = None, category: Optional[str] = None) -> List[Dict[str, Any]]:
        self.ensure_schema()
        sql = f"""
            SELECT id, date, sport_event_id, source_player_a, source_player_b,
                   predicted_winner, opponent, surface, premium_pct, status, result,
                   odd_predicted, odd_opponent, tournament, round, start_time,
                   score, winner_id, sportradar_player_a_id, sportradar_player_b_id
            FROM {self.TABLE}
            WHERE result = 'pending'
        """
        params: List[Any] = []
        if day:
            sql += " AND date = %s"
            params.append(_s(day))
        where, cat_params = category_where(category)
        sql += where
        params.extend(cat_params)
        sql += " ORDER BY date ASC, created_at ASC"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(int(limit))

        out: List[Dict[str, Any]] = []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                cols = [desc[0] for desc in cur.description]
                for db_row in cur.fetchall():
                    row = dict(zip(cols, db_row))
                    row["sourcePlayerA"] = row.pop("source_player_a")
                    row["sourcePlayerB"] = row.pop("source_player_b")
                    row["predictedWinner"] = row.pop("predicted_winner")
                    row["premiumPct"] = row.pop("premium_pct")
                    row["oddPredicted"] = row.pop("odd_predicted")
                    row["oddOpponent"] = row.pop("odd_opponent")
                    row["sportradarSportEventId"] = row.pop("sport_event_id")
                    row["sportradarPlayerAId"] = row.pop("sportradar_player_a_id")
                    row["sportradarPlayerBId"] = row.pop("sportradar_player_b_id")
                    out.append(row)
        return out


    def history_dates(self, days_back: int = 7, limit: int = 60, category: Optional[str] = None) -> List[str]:
        """Dates with any history rows in the recent window.

        Used by STEP30 auto-settle so old win/loss rows can still be corrected to void
        when Sportradar later reports retired/walkover/cancelled.
        """
        self.ensure_schema()
        days_raw = int(days_back or 0)
        where, cat_params = category_where(category)
        params: List[Any] = []
        date_filter = ""
        if days_raw > 0:
            days_raw = min(days_raw, 36500)
            since = today_paris() - timedelta(days=days_raw - 1)
            date_filter = " AND date >= %s"
            params.append(since.isoformat())
        params.extend(cat_params)
        params.append(int(limit))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT date
                    FROM {self.TABLE}
                    WHERE 1=1{date_filter}{where}
                    ORDER BY date ASC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                return [_s(row[0]) for row in cur.fetchall() if _s(row[0])]


    def pending_dates(self, days_back: int = 7, limit: int = 30, category: Optional[str] = None) -> List[str]:
        self.ensure_schema()
        days_raw = int(days_back or 0)
        where, cat_params = category_where(category)
        params: List[Any] = []
        date_filter = ""
        if days_raw > 0:
            days_raw = min(days_raw, 36500)
            since = today_paris() - timedelta(days=days_raw - 1)
            date_filter = " AND date >= %s"
            params.append(since.isoformat())
        params.extend(cat_params)
        params.append(int(limit))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT date
                    FROM {self.TABLE}
                    WHERE result = 'pending'{date_filter}{where}
                    ORDER BY date ASC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                return [_s(row[0]) for row in cur.fetchall() if _s(row[0])]


    def fetch_rows_by_event(self, sport_event_id: str) -> List[Dict[str, Any]]:
        """Return all rows for a Sportradar event id, all categories included."""
        if not sport_event_id:
            return []
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, date, sport_event_id, source_player_a, source_player_b,
                           predicted_winner, opponent, surface, premium_pct, status, result,
                           odd_predicted, odd_opponent, tournament, round, start_time,
                           score, winner_id, sportradar_player_a_id, sportradar_player_b_id
                    FROM {self.TABLE}
                    WHERE sport_event_id = %s
                    ORDER BY date DESC, updated_at DESC, premium_pct DESC
                    """,
                    (sport_event_id,),
                )
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    def void_rows_by_event(
        self,
        sport_event_id: str,
        score: str = "",
        winner_id: str = "",
        *,
        reason: str = "sportradar_void",
        real_winner: str = "",
    ) -> int:
        """Mark every row for an event as void/refunded.

        Used for retired, walkover, abandoned and cancelled matches.
        This intentionally overrides win/loss because betting settlement is refund/void.
        """
        if not sport_event_id:
            return 0
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.TABLE}
                    SET result = 'void',
                        real_winner = COALESCE(NULLIF(%s, ''), real_winner),
                        settled_at = %s,
                        settle_source = %s,
                        score = COALESCE(NULLIF(%s, ''), score),
                        winner_id = COALESCE(NULLIF(%s, ''), winner_id),
                        updated_at = NOW()
                    WHERE sport_event_id = %s
                      AND result <> 'void'
                    """,
                    (_s(real_winner), today_paris().isoformat(), _s(reason) or "sportradar_void", _s(score), _s(winner_id), _s(sport_event_id)),
                )
                changed = int(cur.rowcount or 0)
            conn.commit()
        return changed

    def cleanup_duplicate_events(self, category: Optional[str] = None) -> Dict[str, Any]:
        """Collapse legacy duplicate rows to one row per date + sport_event_id.

        Older STEP25 ids included __pick_<player>, which allowed one match to appear twice.
        STEP30 keeps one canonical id: YYYY-MM-DD__sr:sport_event:...
        """
        self.ensure_schema()
        where, params = category_where(category)
        groups: List[Tuple[str, str, int]] = []
        deleted = 0
        canonicalized = 0

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT date, sport_event_id, COUNT(*)
                    FROM {self.TABLE}
                    WHERE COALESCE(sport_event_id, '') <> ''{where}
                    GROUP BY date, sport_event_id
                    HAVING COUNT(*) > 1
                    """,
                    tuple(params),
                )
                groups = [(str(d), str(e), int(c or 0)) for d, e, c in cur.fetchall()]

                for day, event_id, _count in groups:
                    canonical_id = premium_history_key(day, event_id, "", "", "")
                    cur.execute(
                        f"""
                        SELECT id, result, premium_pct, updated_at
                        FROM {self.TABLE}
                        WHERE date = %s AND sport_event_id = %s
                        ORDER BY
                            CASE WHEN id = %s THEN 0 ELSE 1 END,
                            CASE WHEN result IN ('win','loss','void') THEN 0 ELSE 1 END,
                            premium_pct DESC,
                            updated_at DESC
                        """,
                        (day, event_id, canonical_id),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        continue

                    keep_id = str(rows[0][0])
                    drop_ids = [str(r[0]) for r in rows[1:]]
                    if drop_ids:
                        cur.execute(
                            f"DELETE FROM {self.TABLE} WHERE id = ANY(%s)",
                            (drop_ids,),
                        )
                        deleted += int(cur.rowcount or 0)

                    if keep_id != canonical_id:
                        # If the canonical id already exists, keep it and delete the old keeper.
                        cur.execute(f"SELECT 1 FROM {self.TABLE} WHERE id = %s", (canonical_id,))
                        canonical_exists = cur.fetchone() is not None
                        if canonical_exists:
                            cur.execute(f"DELETE FROM {self.TABLE} WHERE id = %s", (keep_id,))
                            deleted += int(cur.rowcount or 0)
                        else:
                            cur.execute(f"UPDATE {self.TABLE} SET id = %s, updated_at = NOW() WHERE id = %s", (canonical_id, keep_id))
                            canonicalized += int(cur.rowcount or 0)
            conn.commit()

        return {
            "status": "ok",
            "category": normalize_category(category, default="ALL"),
            "duplicateGroups": len(groups),
            "deletedRows": deleted,
            "canonicalizedRows": canonicalized,
            "policy": "STEP30 one row per date + sport_event_id.",
        }

    def settle_pending_by_event(self, sport_event_id: str, real_winner: str, score: str, winner_id: str, *, source: str = "sportradar") -> int:
        if not sport_event_id or not real_winner:
            return 0
        self.ensure_schema()
        changed = 0
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
                for row_id, predicted in rows:
                    result = "win" if same_player(_s(predicted), real_winner) else "loss"
                    cur.execute(
                        f"""
                        UPDATE {self.TABLE}
                        SET result = %s,
                            real_winner = %s,
                            settled_at = %s,
                            settle_source = %s,
                            score = COALESCE(NULLIF(%s, ''), score),
                            winner_id = COALESCE(NULLIF(%s, ''), winner_id),
                            updated_at = NOW()
                        WHERE id = %s AND result = 'pending'
                        """,
                        (result, real_winner, today_paris().isoformat(), _s(source) or "sportradar", score, winner_id, row_id),
                    )
                    changed += int(cur.rowcount or 0)
            conn.commit()
        return changed

    def settle_pending_by_id(self, row_id: str, real_winner: str, score: str, winner_id: str, *, source: str = "sportradar-name-fallback") -> int:
        if not row_id or not real_winner:
            return 0
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT predicted_winner FROM {self.TABLE} WHERE id = %s AND result = 'pending'", (row_id,))
                row = cur.fetchone()
                if not row:
                    return 0
                predicted = _s(row[0])
                result = "win" if same_player(predicted, real_winner) else "loss"
                cur.execute(
                    f"""
                    UPDATE {self.TABLE}
                    SET result = %s,
                        real_winner = %s,
                        settled_at = %s,
                        settle_source = %s,
                        score = COALESCE(NULLIF(%s, ''), score),
                        winner_id = COALESCE(NULLIF(%s, ''), winner_id),
                        updated_at = NOW()
                    WHERE id = %s AND result = 'pending'
                    """,
                    (result, real_winner, today_paris().isoformat(), _s(source) or "sportradar-name-fallback", score, winner_id, row_id),
                )
                changed = int(cur.rowcount or 0)
            conn.commit()
        return changed


    def repair_dellien_royer_refuse(self) -> Dict[str, Any]:
        """One-time safe repair for the 2026-05-24 Dellien/Royer duplicate.

        STEP30: the duplicate cleanup previously kept the later/wrong Dellien pick
        because it sorted by premium_pct. The correct first pick was Royer.
        This repair updates the canonical REFUSE row to the Royer pick and win.
        """
        self.ensure_schema()
        day = "2026-05-24"
        event_id = "sr:sport_event:71664880"
        cat = "REFUSE"
        canonical_id = premium_history_key(day, event_id, "Dellien, Hugo", "Royer, Valentin", "Royer, Valentin")

        before: List[Dict[str, Any]] = []
        after: List[Dict[str, Any]] = []
        deleted = 0
        updated = 0

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, predicted_winner, opponent, result, real_winner, premium_pct,
                           odd_predicted, odd_opponent, score
                    FROM {self.TABLE}
                    WHERE date = %s AND sport_event_id = %s AND UPPER(COALESCE(status, '')) = %s
                    ORDER BY created_at ASC, updated_at ASC
                    """,
                    (day, event_id, cat),
                )
                cols = [desc[0] for desc in cur.description]
                before = [dict(zip(cols, row)) for row in cur.fetchall()]

                if not before:
                    return {
                        "status": "not_found",
                        "message": "Aucune ligne Dellien/Royer trouvée dans REFUSE.",
                        "date": day,
                        "sportEventId": event_id,
                        "category": cat,
                    }

                # If the canonical id is missing, canonicalize the oldest row first.
                ids = [str(r.get("id")) for r in before]
                if canonical_id not in ids:
                    keep_id = ids[0]
                    cur.execute(f"UPDATE {self.TABLE} SET id = %s WHERE id = %s", (canonical_id, keep_id))
                    updated += int(cur.rowcount or 0)

                # Remove any remaining legacy duplicate rows for this same event/category.
                cur.execute(
                    f"""
                    DELETE FROM {self.TABLE}
                    WHERE date = %s AND sport_event_id = %s AND UPPER(COALESCE(status, '')) = %s
                      AND id <> %s
                    """,
                    (day, event_id, cat, canonical_id),
                )
                deleted += int(cur.rowcount or 0)

                cur.execute(
                    f"""
                    UPDATE {self.TABLE}
                    SET source_player_a = %s,
                        source_player_b = %s,
                        predicted_winner = %s,
                        opponent = %s,
                        premium_pct = %s,
                        result = %s,
                        real_winner = %s,
                        settled_at = %s,
                        settle_source = %s,
                        score = %s,
                        winner_id = %s,
                        odd_predicted = %s,
                        odd_opponent = %s,
                        sportradar_player_a_id = %s,
                        sportradar_player_b_id = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        "Dellien, Hugo",
                        "Royer, Valentin",
                        "Royer, Valentin",
                        "Dellien, Hugo",
                        55.7,
                        "win",
                        "Royer, Valentin",
                        "2026-05-24",
                        "manual_step30_correct_first_pick",
                        "4-6 2-6 2-6",
                        "sr:competitor:341220",
                        "2.3",
                        "1.65",
                        "sr:competitor:341220",
                        "sr:competitor:57289",
                        canonical_id,
                    ),
                )
                updated += int(cur.rowcount or 0)

                cur.execute(
                    f"""
                    SELECT id, predicted_winner, opponent, result, real_winner, premium_pct,
                           odd_predicted, odd_opponent, score
                    FROM {self.TABLE}
                    WHERE id = %s
                    """,
                    (canonical_id,),
                )
                cols = [desc[0] for desc in cur.description]
                after = [dict(zip(cols, row)) for row in cur.fetchall()]
            conn.commit()

        return {
            "status": "ok",
            "repair": "dellien_royer_refuse_first_pick",
            "date": day,
            "sportEventId": event_id,
            "category": cat,
            "deletedDuplicateRows": deleted,
            "updatedRows": updated,
            "before": before,
            "after": after,
            "policy": "Correctif manuel STEP30 : le premier pick historique était Royer, pas Dellien.",
        }


    def reset_category(self, category: str) -> Dict[str, Any]:
        """Delete history rows for one category only."""
        if not self.enabled:
            return {
                "status": "error",
                "databaseConfigured": False,
                "databaseStatus": "not_configured",
                "table": self.TABLE,
                "error": "DATABASE_URL absente dans le service web.",
            }
        cat = normalize_category(category)
        if cat == "ALL":
            return self.reset_all()
        self.ensure_schema()
        before = self.counts(category=cat)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self.TABLE} WHERE UPPER(COALESCE(status, 'PREMIUM')) = %s", (cat,))
                deleted = int(cur.rowcount or 0)
            conn.commit()
        after = self.counts(category=cat)
        return {
            "status": "ok",
            "databaseConfigured": True,
            "databaseStatus": "ok",
            "table": self.TABLE,
            "category": cat,
            "deletedRows": deleted,
            "countsBefore": before,
            "countsAfter": after,
            "policy": "Reset par catégorie uniquement : tennis_results_2026 / Elo ne sont pas touchés.",
        }


    def reset_all(self) -> Dict[str, Any]:
        """Delete all Premium history rows from PostgreSQL.

        This is intentionally separate from tennis_results_2026 and only affects
        the historique moteur.
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
            "policy": "Reset global historique moteur : tennis_results_2026 / Elo ne sont pas touchés.",
        }

    def summary(self, category: Optional[str] = "PREMIUM") -> Dict[str, Any]:
        cat = normalize_category(category)
        rows = self.fetch_rows(limit=None, category=cat)
        return build_premium_summary(rows, category=cat)


def stats_for_period(rows: List[Dict[str, Any]], period_days: Optional[int], category: Optional[str] = "PREMIUM") -> Dict[str, Any]:
    today = today_paris()
    selected: List[Dict[str, Any]] = []
    cat = normalize_category(category)
    for row in rows:
        if cat != "ALL" and row_category(row) != cat:
            continue
        d = parse_date(row.get("date"))
        if d is None:
            continue
        if period_days is not None and d < today - timedelta(days=period_days - 1):
            continue
        selected.append(row)

    settled = [r for r in selected if normalize_result(r.get("result")) in {"win", "loss"}]
    wins = sum(1 for r in settled if normalize_result(r.get("result")) == "win")
    losses = sum(1 for r in settled if normalize_result(r.get("result")) == "loss")
    pending = sum(1 for r in selected if normalize_result(r.get("result")) == "pending")
    voids = sum(1 for r in selected if is_void_result(r.get("result")))
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
        "trackedCategory": len(selected),
        "category": normalize_category(category),
        "settled": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "void": voids,
        "voids": voids,
        "refunded": voids,
        "winRate": win_rate,
        "profitUnits": round(profit_units, 3),
        "roiPct": roi,
        "oddsUsed": odds_used,
    }


def build_premium_summary(rows: List[Dict[str, Any]], category: Optional[str] = "PREMIUM") -> Dict[str, Any]:
    cat = normalize_category(category)
    by_day: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if cat != "ALL" and row_category(row) != cat:
            continue
        d = _s(row.get("date"))
        if not d:
            continue
        bucket = by_day.setdefault(d, {
            "date": d,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "void": 0,
            "voids": 0,
            "refunded": 0,
            "winRate": 0.0,
            "profitUnits": 0.0,
            "profitEur": 0.0,
            "hadPremiumToday": True,
            "hadPremiumSettledToday": False,
            "category": cat,
        })
        odd = _f(row.get("oddPredicted"), 0.0)
        result = normalize_result(row.get("result"))
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
        elif result == "void":
            bucket["void"] += 1
            bucket["voids"] += 1
            bucket["refunded"] += 1
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
            "voidThatDay": int(bucket.get("void") or 0),
            "refundedThatDay": int(bucket.get("refunded") or 0),
            "hadPremiumToday": bool(bucket.get("hadPremiumToday", False)),
            "hadPremiumSettledToday": bool(bucket.get("hadPremiumSettledToday", False)),
            "category": cat,
        })

    return {
        "status": "ok",
        "storage": {"mode": "postgres", "table": PostgresPremiumStore.TABLE},
        "summary": {
            "category": cat,
            "label": category_label(cat),
            "day": stats_for_period(rows, 1, cat),
            "week": stats_for_period(rows, 7, cat),
            "month": stats_for_period(rows, 30, cat),
            "year": stats_for_period(rows, 365, cat),
            "all": stats_for_period(rows, None, cat),
        },
        "chart": {
            "days": days,
            "cumulativeDays": cumulative_days,
            "category": cat,
            "label": category_label(cat),
            "description": f"Historique {category_label(cat)} séparé, stocké dans PostgreSQL.",
            "stakeEur": STAKE_EUR,
            "euroAxisMin": -2000.0,
            "euroAxisMax": 2000.0,
            "winRateAxisMin": 0.0,
            "winRateAxisMax": 100.0,
        },
        "rows": rows,
    }
