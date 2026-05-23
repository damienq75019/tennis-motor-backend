from __future__ import annotations

import csv
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

CSV_HEADER = [
    "tourney_id", "tourney_name", "surface", "draw_size", "tourney_level", "indoor", "tourney_date",
    "match_num", "winner_id", "winner_seed", "winner_entry", "winner_name", "winner_hand", "winner_ht",
    "winner_ioc", "winner_age", "winner_rank", "winner_rank_points", "loser_id", "loser_seed",
    "loser_entry", "loser_name", "loser_hand", "loser_ht", "loser_ioc", "loser_age", "loser_rank",
    "loser_rank_points", "score", "best_of", "round", "minutes", "w_ace", "w_df", "w_svpt",
    "w_1stIn", "w_1stWon", "w_2ndWon", "w_SvGms", "w_bpSaved", "w_bpFaced", "l_ace",
    "l_df", "l_svpt", "l_1stIn", "l_1stWon", "l_2ndWon", "l_SvGms", "l_bpSaved", "l_bpFaced",
]


def _s(value: Any) -> str:
    return str(value or "").strip()


def _display_name_for_history(value: Any) -> str:
    text = re.sub(r"\s+", " ", _s(value))
    if "," in text:
        left, right = text.split(",", 1)
        left = re.sub(r"\s+", " ", left.strip())
        right = re.sub(r"\s+", " ", right.strip())
        if left and right:
            return f"{right} {left}"
    return text


def _canon(value: Any) -> str:
    text = _display_name_for_history(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def row_unique_key(row: Dict[str, Any]) -> str:
    """Semantic key used for idempotency across CSV and PostgreSQL.

    We intentionally do not depend only on Sportradar event_id because older CSV rows do not have it.
    """
    parts = [
        _s(row.get("tourney_date")),
        _canon(row.get("tourney_name")),
        _canon(row.get("winner_name")),
        _canon(row.get("loser_name")),
        _s(row.get("score")),
    ]
    return "|".join(parts)


def _safe_int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return None


class PostgresResultsStore:
    """Persistent storage for Tennis Motor 2026 ATP results on Railway PostgreSQL.

    Table strategy:
    - one row per ATP singles result;
    - unique semantic key prevents duplicates even when the CSV seed has no sport_event_id;
    - raw_row_json preserves the exact Jeff-Sackmann-compatible CSV row;
    - export_csv rebuilds data/2026.csv from PostgreSQL, so the engine can keep loading CSVs.
    """

    TABLE = "tennis_results_2026"

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
        except Exception as exc:  # pragma: no cover - depends on Railway dependency install
            raise RuntimeError(
                "Dépendance PostgreSQL manquante. Ajoute psycopg[binary] dans requirements.txt."
            ) from exc
        return psycopg.connect(self.database_url, connect_timeout=10)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE} (
                        unique_key TEXT PRIMARY KEY,
                        sport_event_id TEXT,
                        source TEXT NOT NULL DEFAULT 'sportradar',
                        tourney_date TEXT NOT NULL,
                        tourney_name TEXT NOT NULL,
                        match_num INTEGER,
                        winner_name TEXT,
                        loser_name TEXT,
                        score TEXT,
                        raw_row_json TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_{self.TABLE}_sport_event_id
                    ON {self.TABLE}(sport_event_id)
                    WHERE sport_event_id IS NOT NULL AND sport_event_id <> ''
                    """
                )
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_date ON {self.TABLE}(tourney_date)")
            conn.commit()

    def status(self) -> Dict[str, Any]:
        if not self.enabled:
            return {
                "databaseConfigured": False,
                "databaseStatus": "not_configured",
                "table": self.TABLE,
                "rows": None,
                "error": "DATABASE_URL absente dans le service web.",
            }
        try:
            self.ensure_schema()
            return {
                "databaseConfigured": True,
                "databaseStatus": "ok",
                "table": self.TABLE,
                "rows": self.count_rows(),
                "policy": "PostgreSQL persistent store + CSV rebuild for motor compatibility",
            }
        except Exception as exc:
            return {
                "databaseConfigured": True,
                "databaseStatus": "error",
                "table": self.TABLE,
                "rows": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def count_rows(self) -> int:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {self.TABLE}")
                value = cur.fetchone()[0]
        return int(value or 0)

    def existing_keys(self) -> Set[str]:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT unique_key FROM {self.TABLE}")
                return {str(row[0]) for row in cur.fetchall()}

    def import_csv_if_empty(self, csv_path: Path) -> Dict[str, int]:
        self.ensure_schema()
        if self.count_rows() > 0:
            return {"imported": 0, "skipped": 0, "tableWasEmpty": 0}
        if not csv_path.exists():
            return {"imported": 0, "skipped": 0, "tableWasEmpty": 1}

        rows: List[Dict[str, str]] = []
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for raw in reader:
                    row = {k: _s(raw.get(k)) for k in CSV_HEADER}
                    if row.get("winner_name") and row.get("loser_name") and row.get("score"):
                        rows.append(row)
        except Exception:
            return {"imported": 0, "skipped": 0, "tableWasEmpty": 1}

        inserted, skipped = self.insert_rows(rows, source="csv_seed")
        return {"imported": inserted, "skipped": skipped, "tableWasEmpty": 1}

    def insert_rows(self, rows: Iterable[Dict[str, Any]], *, source: str = "sportradar") -> Tuple[int, int]:
        self.ensure_schema()
        inserted = 0
        skipped = 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                for raw in rows:
                    row = {k: _s(raw.get(k)) for k in CSV_HEADER}
                    unique_key = row_unique_key(row)
                    if not unique_key or unique_key.count("|") < 4:
                        skipped += 1
                        continue
                    sport_event_id = _s(raw.get("sport_event_id") or raw.get("eventId") or raw.get("event_id"))
                    match_num = _safe_int_or_none(row.get("match_num"))
                    cur.execute(
                        f"""
                        INSERT INTO {self.TABLE} (
                            unique_key, sport_event_id, source, tourney_date, tourney_name,
                            match_num, winner_name, loser_name, score, raw_row_json, updated_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            unique_key,
                            sport_event_id,
                            source,
                            row.get("tourney_date", ""),
                            row.get("tourney_name", ""),
                            match_num,
                            row.get("winner_name", ""),
                            row.get("loser_name", ""),
                            row.get("score", ""),
                            json.dumps(row, ensure_ascii=False),
                        ),
                    )
                    if cur.rowcount == 1:
                        inserted += 1
                    else:
                        skipped += 1
            conn.commit()
        return inserted, skipped

    def export_rows(self) -> List[Dict[str, str]]:
        self.ensure_schema()
        out: List[Dict[str, str]] = []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT raw_row_json
                    FROM {self.TABLE}
                    ORDER BY tourney_date ASC, tourney_name ASC, COALESCE(match_num, 999999) ASC, winner_name ASC, loser_name ASC
                    """
                )
                for (raw_json,) in cur.fetchall():
                    try:
                        row = json.loads(raw_json)
                    except Exception:
                        continue
                    out.append({k: _s(row.get(k)) for k in CSV_HEADER})
        return out

    def export_csv(self, csv_path: Path, *, backup: bool = True) -> Dict[str, Any]:
        rows = self.export_rows()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        if backup and csv_path.exists():
            backup_path = csv_path.parent.parent / "output" / "results2026" / "2026_backup_before_postgres_export.csv"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if not backup_path.exists():
                backup_path.write_bytes(csv_path.read_bytes())
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADER, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return {"csvPath": str(csv_path), "rowsExported": len(rows)}
