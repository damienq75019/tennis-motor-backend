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


def _b(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "oui", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "non", "n", "off", ""}:
        return False
    return default


def _raw(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("raw")
    return raw if isinstance(raw, dict) else {}


def _field(row: Dict[str, Any], key: str, default: Any = "") -> Any:
    if key in row and row.get(key) is not None:
        return row.get(key)
    raw = _raw(row)
    if key in raw and raw.get(key) is not None:
        return raw.get(key)
    return default


def _refuse_value_status_from_fields(category: str, pct: float, odd: float) -> str:
    cat = normalize_category(category, default="REFUSE")
    if cat != "REFUSE":
        return "NOT_REFUSE"
    if odd <= 1.0:
        return "NO_ODDS"
    if odd > 1.80:
        return "DANGER_ODD_GT_180"
    if 68.0 <= pct <= 72.0:
        return "VALUE_STRICT"
    if 60.0 <= pct <= 72.0:
        return "VALUE_LARGE"
    return "COTE_180_ONLY"


def _refuse_value_label(status: str) -> str:
    labels = {
        "NOT_REFUSE": "—",
        "NO_ODDS": "❌ REFUSÉ — cote manquante",
        "DANGER_ODD_GT_180": "❌ REFUSÉ DANGER",
        "VALUE_STRICT": "✅ REFUSÉ VALUE STRICT",
        "VALUE_LARGE": "✅ REFUSÉ VALUE LARGE",
        "COTE_180_ONLY": "⚖ REFUSÉ COTE ≤ 1.80",
    }
    return labels.get(status or "", status or "")


def _refuse_value_reason(status: str) -> str:
    reasons = {
        "NOT_REFUSE": "Règle Refusé Value non appliquée : le match n'est pas en catégorie REFUSE.",
        "NO_ODDS": "Refusé sans cote exploitable : pas de classement value.",
        "DANGER_ODD_GT_180": "Refusé avec cote > 1.80 : zone négative dans l'historique actuel.",
        "VALUE_STRICT": "Refusé 68-72% avec cote <= 1.80 : meilleure zone actuelle de l'audit.",
        "VALUE_LARGE": "Refusé 60-72% avec cote <= 1.80 : zone value large actuelle.",
        "COTE_180_ONLY": "Refusé avec cote <= 1.80 mais hors zone 60-72%. À surveiller, pas strict.",
    }
    return reasons.get(status or "", "")


def compute_refuse_value_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    category = normalize_category(_field(row, "refuseValueCategoryBase", _field(row, "status", _field(row, "category", "REFUSE"))), default="REFUSE")
    pct = _f(_field(row, "premiumPct", _field(row, "premium_pct", 0.0)))
    if 0.0 <= pct <= 1.0:
        pct *= 100.0
    odd = _f(_field(row, "refuseValueOdd", _field(row, "oddPredicted", _field(row, "odd_predicted", 0.0))))
    implied_pct = (100.0 / odd) if odd > 1.0 else 0.0
    ev_pct = ((pct / 100.0) * odd - 1.0) * 100.0 if odd > 1.0 else 0.0

    status = _s(_field(row, "refuseValueStatus", ""))
    if not status:
        status = _refuse_value_status_from_fields(category, pct, odd)

    cote180 = _b(_field(row, "refuseValueCote180", None), default=False) or (category == "REFUSE" and odd > 1.0 and odd <= 1.80)
    large = _b(_field(row, "refuseValueLarge", None), default=False) or (category == "REFUSE" and odd > 1.0 and odd <= 1.80 and 60.0 <= pct <= 72.0)
    strict = _b(_field(row, "refuseValueStrict", None), default=False) or (category == "REFUSE" and odd > 1.0 and odd <= 1.80 and 68.0 <= pct <= 72.0)
    danger = _b(_field(row, "refuseDanger", _field(row, "refuseValueDanger", None)), default=False) or (category == "REFUSE" and (odd <= 1.0 or odd > 1.80))

    return {
        "refuseValueEngineVersion": _s(_field(row, "refuseValueEngineVersion", "step62-refuse-value-persistent-history")),
        "refuseValueApplies": category == "REFUSE",
        "refuseValueCategoryBase": category,
        "refuseValueOdd": round(odd, 3) if odd > 0 else 0.0,
        "refuseValueImpliedPct": round(implied_pct, 2),
        "refuseValueEvPct": round(ev_pct, 2),
        "refuseValueCote180": bool(cote180),
        "refuseValueLarge": bool(large),
        "refuseValueStrict": bool(strict),
        "refuseValueDanger": bool(danger),
        "refuseDanger": bool(danger),
        "refuseValueStatus": status,
        "refuseValueDecision": _s(_field(row, "refuseValueDecision", status)) or status,
        "refuseValueLabel": _s(_field(row, "refuseValueLabel", _refuse_value_label(status))) or _refuse_value_label(status),
        "refuseValueReason": _s(_field(row, "refuseValueReason", _refuse_value_reason(status))) or _refuse_value_reason(status),
        "vetoAudit": _s(_field(row, "vetoAudit", "non")),
        "vetoAuditActive": _b(_field(row, "vetoAuditActive", False)),
        "vetoAuditPolicy": _s(_field(row, "vetoAuditPolicy", "")),
    }


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
    - With provider event id: one row per date + event, regardless of pick/orientation.
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
                        source TEXT NOT NULL DEFAULT 'api_tennis',
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
                # STEP62 : colonnes persistantes Refusés Value.
                # Les données restent aussi dans raw_json pour compatibilité, mais ces
                # colonnes permettent un vrai endpoint historique durable et filtrable.
                for ddl in [
                    "ADD COLUMN IF NOT EXISTS refuse_value_engine_version TEXT",
                    "ADD COLUMN IF NOT EXISTS refuse_value_applies BOOLEAN NOT NULL DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS refuse_value_category_base TEXT",
                    "ADD COLUMN IF NOT EXISTS refuse_value_odd DOUBLE PRECISION NOT NULL DEFAULT 0",
                    "ADD COLUMN IF NOT EXISTS refuse_value_implied_pct DOUBLE PRECISION NOT NULL DEFAULT 0",
                    "ADD COLUMN IF NOT EXISTS refuse_value_ev_pct DOUBLE PRECISION NOT NULL DEFAULT 0",
                    "ADD COLUMN IF NOT EXISTS refuse_value_cote_180 BOOLEAN NOT NULL DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS refuse_value_large BOOLEAN NOT NULL DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS refuse_value_strict BOOLEAN NOT NULL DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS refuse_value_danger BOOLEAN NOT NULL DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS refuse_value_status TEXT",
                    "ADD COLUMN IF NOT EXISTS refuse_value_decision TEXT",
                    "ADD COLUMN IF NOT EXISTS refuse_value_label TEXT",
                    "ADD COLUMN IF NOT EXISTS refuse_value_reason TEXT",
                    "ADD COLUMN IF NOT EXISTS veto_audit TEXT",
                    "ADD COLUMN IF NOT EXISTS veto_audit_active BOOLEAN NOT NULL DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS veto_audit_policy TEXT",
                ]:
                    cur.execute(f"ALTER TABLE {self.TABLE} {ddl}")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_refuse_value_status ON {self.TABLE}(refuse_value_status)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_refuse_value_large ON {self.TABLE}(refuse_value_large)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_refuse_value_strict ON {self.TABLE}(refuse_value_strict)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_refuse_value_cote_180 ON {self.TABLE}(refuse_value_cote_180)")
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
                   sportradar_player_b_id, refuse_value_engine_version,
                   refuse_value_applies, refuse_value_category_base, refuse_value_odd,
                   refuse_value_implied_pct, refuse_value_ev_pct, refuse_value_cote_180,
                   refuse_value_large, refuse_value_strict, refuse_value_danger,
                   refuse_value_status, refuse_value_decision, refuse_value_label,
                   refuse_value_reason, veto_audit, veto_audit_active, veto_audit_policy,
                   raw_json
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
                    row["refuseValueEngineVersion"] = row.pop("refuse_value_engine_version", "")
                    row["refuseValueApplies"] = bool(row.pop("refuse_value_applies", False))
                    row["refuseValueCategoryBase"] = row.pop("refuse_value_category_base", "")
                    row["refuseValueOdd"] = row.pop("refuse_value_odd", 0.0)
                    row["refuseValueImpliedPct"] = row.pop("refuse_value_implied_pct", 0.0)
                    row["refuseValueEvPct"] = row.pop("refuse_value_ev_pct", 0.0)
                    row["refuseValueCote180"] = bool(row.pop("refuse_value_cote_180", False))
                    row["refuseValueLarge"] = bool(row.pop("refuse_value_large", False))
                    row["refuseValueStrict"] = bool(row.pop("refuse_value_strict", False))
                    row["refuseValueDanger"] = bool(row.pop("refuse_value_danger", False))
                    row["refuseDanger"] = row["refuseValueDanger"]
                    row["refuseValueStatus"] = row.pop("refuse_value_status", "")
                    row["refuseValueDecision"] = row.pop("refuse_value_decision", "")
                    row["refuseValueLabel"] = row.pop("refuse_value_label", "")
                    row["refuseValueReason"] = row.pop("refuse_value_reason", "")
                    row["vetoAudit"] = row.pop("veto_audit", "")
                    row["vetoAuditActive"] = bool(row.pop("veto_audit_active", False))
                    row["vetoAuditPolicy"] = row.pop("veto_audit_policy", "")
                    rv = compute_refuse_value_fields(row)
                    for key, value in rv.items():
                        if key not in row or row.get(key) in (None, "", 0, 0.0, False):
                            row[key] = value
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

                rv_fields = compute_refuse_value_fields(row)

                params = (
                    row_id,
                    _s(row.get("date")),
                    _s(row.get("sportradarSportEventId")),
                    _s(row.get("source")) or "api_tennis",
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
                    _s(rv_fields.get("refuseValueEngineVersion")),
                    bool(rv_fields.get("refuseValueApplies")),
                    _s(rv_fields.get("refuseValueCategoryBase")),
                    _f(rv_fields.get("refuseValueOdd")),
                    _f(rv_fields.get("refuseValueImpliedPct")),
                    _f(rv_fields.get("refuseValueEvPct")),
                    bool(rv_fields.get("refuseValueCote180")),
                    bool(rv_fields.get("refuseValueLarge")),
                    bool(rv_fields.get("refuseValueStrict")),
                    bool(rv_fields.get("refuseValueDanger")),
                    _s(rv_fields.get("refuseValueStatus")),
                    _s(rv_fields.get("refuseValueDecision")),
                    _s(rv_fields.get("refuseValueLabel")),
                    _s(rv_fields.get("refuseValueReason")),
                    _s(rv_fields.get("vetoAudit")),
                    bool(rv_fields.get("vetoAuditActive")),
                    _s(rv_fields.get("vetoAuditPolicy")),
                    json.dumps(row.get("raw") or row, ensure_ascii=False),
                )
                cur.execute(
                    f"""
                    INSERT INTO {self.TABLE} (
                        id, date, sport_event_id, source, source_player_a, source_player_b,
                        predicted_winner, opponent, surface, premium_pct, status, veto,
                        decision, odd_predicted, odd_opponent, odds_source, result, real_winner,
                        settled_at, settle_source, tournament, season_name, round, start_time,
                        score, winner_id, sportradar_player_a_id, sportradar_player_b_id,
                        refuse_value_engine_version, refuse_value_applies, refuse_value_category_base,
                        refuse_value_odd, refuse_value_implied_pct, refuse_value_ev_pct,
                        refuse_value_cote_180, refuse_value_large, refuse_value_strict, refuse_value_danger,
                        refuse_value_status, refuse_value_decision, refuse_value_label, refuse_value_reason,
                        veto_audit, veto_audit_active, veto_audit_policy, raw_json
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
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
                        refuse_value_engine_version = EXCLUDED.refuse_value_engine_version,
                        refuse_value_applies = EXCLUDED.refuse_value_applies,
                        refuse_value_category_base = EXCLUDED.refuse_value_category_base,
                        refuse_value_odd = EXCLUDED.refuse_value_odd,
                        refuse_value_implied_pct = EXCLUDED.refuse_value_implied_pct,
                        refuse_value_ev_pct = EXCLUDED.refuse_value_ev_pct,
                        refuse_value_cote_180 = EXCLUDED.refuse_value_cote_180,
                        refuse_value_large = EXCLUDED.refuse_value_large,
                        refuse_value_strict = EXCLUDED.refuse_value_strict,
                        refuse_value_danger = EXCLUDED.refuse_value_danger,
                        refuse_value_status = EXCLUDED.refuse_value_status,
                        refuse_value_decision = EXCLUDED.refuse_value_decision,
                        refuse_value_label = EXCLUDED.refuse_value_label,
                        refuse_value_reason = EXCLUDED.refuse_value_reason,
                        veto_audit = EXCLUDED.veto_audit,
                        veto_audit_active = EXCLUDED.veto_audit_active,
                        veto_audit_policy = EXCLUDED.veto_audit_policy,
                        raw_json = EXCLUDED.raw_json,
                        updated_at = NOW()
                    """,
                    params,
                )
            conn.commit()
        return "updated" if existing else "inserted"


    def fetch_refuse_value_rows(self, limit: Optional[int] = None, value_filter: str = "all") -> List[Dict[str, Any]]:
        """Durable STEP62 history for Refusés Value.

        value_filter: all | cote180 | large | strict | danger | no_odds
        """
        rows = self.fetch_rows(limit=limit, category="REFUSE")
        flt = _s(value_filter).lower().replace("-", "_").replace(" ", "_") or "all"
        out: List[Dict[str, Any]] = []
        for row in rows:
            rv = compute_refuse_value_fields(row)
            row.update(rv)
            status = _s(row.get("refuseValueStatus"))
            keep = False
            if flt in {"all", "tout", "*"}:
                keep = True
            elif flt in {"cote180", "cote_180", "cote", "180"}:
                keep = bool(row.get("refuseValueCote180"))
            elif flt in {"large", "value_large"}:
                keep = bool(row.get("refuseValueLarge"))
            elif flt in {"strict", "value_strict"}:
                keep = bool(row.get("refuseValueStrict"))
            elif flt in {"danger", "danger_odd_gt_180"}:
                keep = bool(row.get("refuseValueDanger")) or status.startswith("DANGER")
            elif flt in {"no_odds", "noodds"}:
                keep = status == "NO_ODDS"
            if keep:
                out.append(row)
        return out

    def refuse_value_summary(self, limit: Optional[int] = None, value_filter: str = "all") -> Dict[str, Any]:
        rows = self.fetch_refuse_value_rows(limit=limit, value_filter=value_filter)
        wins = losses = voids = pending = 0
        cote180 = large = strict = danger = no_odds = 0
        profit = 0.0
        odds_used = 0
        odd_sum = 0.0
        by_status: Dict[str, int] = {}
        by_day: Dict[str, Dict[str, Any]] = {}

        for row in rows:
            result = normalize_result(row.get("result"))
            odd = _f(row.get("refuseValueOdd") or row.get("oddPredicted"), 0.0)
            status = _s(row.get("refuseValueStatus")) or "UNKNOWN"
            by_status[status] = by_status.get(status, 0) + 1
            if row.get("refuseValueCote180"):
                cote180 += 1
            if row.get("refuseValueLarge"):
                large += 1
            if row.get("refuseValueStrict"):
                strict += 1
            if row.get("refuseValueDanger") or status.startswith("DANGER"):
                danger += 1
            if status == "NO_ODDS":
                no_odds += 1
            if odd > 1.0:
                odds_used += 1
                odd_sum += odd

            day = _s(row.get("date")) or "unknown"
            d = by_day.setdefault(day, {"date": day, "total": 0, "wins": 0, "losses": 0, "voids": 0, "pending": 0, "profitEur": 0.0, "settled": 0})
            d["total"] += 1

            if result == "win":
                wins += 1
                d["wins"] += 1
                d["settled"] += 1
                gain = (odd - 1.0) * STAKE_EUR if odd > 1.0 else 0.0
                profit += gain
                d["profitEur"] += gain
            elif result == "loss":
                losses += 1
                d["losses"] += 1
                d["settled"] += 1
                profit -= STAKE_EUR
                d["profitEur"] -= STAKE_EUR
            elif result == "void":
                voids += 1
                d["voids"] += 1
            else:
                pending += 1
                d["pending"] += 1

        settled = wins + losses
        total = len(rows)
        win_rate = (wins * 100.0 / settled) if settled > 0 else 0.0
        roi = (profit * 100.0 / (settled * STAKE_EUR)) if settled > 0 else 0.0
        avg_odd = (odd_sum / odds_used) if odds_used > 0 else 0.0
        break_even = (100.0 / avg_odd) if avg_odd > 1.0 else 0.0

        chart_days: List[Dict[str, Any]] = []
        for day in sorted(by_day.keys()):
            d = by_day[day]
            settled_day = int(d.get("settled") or 0)
            d["winRate"] = round((d.get("wins", 0) * 100.0 / settled_day) if settled_day > 0 else 0.0, 2)
            d["roiPct"] = round((d.get("profitEur", 0.0) * 100.0 / (settled_day * STAKE_EUR)) if settled_day > 0 else 0.0, 2)
            d["profitEur"] = round(float(d.get("profitEur") or 0.0), 2)
            chart_days.append(d)

        return {
            "status": "ok",
            "filter": value_filter,
            "total": total,
            "cote180": cote180,
            "large": large,
            "strict": strict,
            "danger": danger,
            "noOdds": no_odds,
            "pending": pending,
            "wins": wins,
            "losses": losses,
            "void": voids,
            "voids": voids,
            "refunded": voids,
            "settled": settled,
            "winRate": round(win_rate, 2),
            "winRatePct": round(win_rate, 2),
            "profitEur": round(profit, 2),
            "profitUnits": round(profit / STAKE_EUR, 3),
            "roiPct": round(roi, 2),
            "oddsUsed": odds_used,
            "avgOdd": round(avg_odd, 3),
            "breakEvenPct": round(break_even, 2),
            "byStatus": by_status,
            "chartDays": chart_days,
        }

    def backfill_refuse_value_columns(self, limit: int = 50000, dry_run: bool = False) -> Dict[str, Any]:
        """Populate STEP62 physical columns for existing REFUSE rows.

        Existing older rows keep their result. Only the Refuse Value metadata columns
        are updated from raw_json / odds / premium_pct.
        """
        self.ensure_schema()
        rows = self.fetch_rows(limit=limit, category="REFUSE")
        updated = 0
        sample: List[Dict[str, Any]] = []
        if dry_run:
            for row in rows[:10]:
                rv = compute_refuse_value_fields(row)
                sample.append({"id": row.get("id"), "date": row.get("date"), "pick": row.get("predictedWinner"), "status": rv.get("refuseValueStatus")})
            return {"status": "ok", "dryRun": True, "scanned": len(rows), "wouldUpdate": len(rows), "sample": sample}

        with self._connect() as conn:
            with conn.cursor() as cur:
                for row in rows:
                    rv = compute_refuse_value_fields(row)
                    cur.execute(
                        f"""
                        UPDATE {self.TABLE}
                        SET refuse_value_engine_version = %s,
                            refuse_value_applies = %s,
                            refuse_value_category_base = %s,
                            refuse_value_odd = %s,
                            refuse_value_implied_pct = %s,
                            refuse_value_ev_pct = %s,
                            refuse_value_cote_180 = %s,
                            refuse_value_large = %s,
                            refuse_value_strict = %s,
                            refuse_value_danger = %s,
                            refuse_value_status = %s,
                            refuse_value_decision = %s,
                            refuse_value_label = %s,
                            refuse_value_reason = %s,
                            veto_audit = %s,
                            veto_audit_active = %s,
                            veto_audit_policy = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            _s(rv.get("refuseValueEngineVersion")),
                            bool(rv.get("refuseValueApplies")),
                            _s(rv.get("refuseValueCategoryBase")),
                            _f(rv.get("refuseValueOdd")),
                            _f(rv.get("refuseValueImpliedPct")),
                            _f(rv.get("refuseValueEvPct")),
                            bool(rv.get("refuseValueCote180")),
                            bool(rv.get("refuseValueLarge")),
                            bool(rv.get("refuseValueStrict")),
                            bool(rv.get("refuseValueDanger")),
                            _s(rv.get("refuseValueStatus")),
                            _s(rv.get("refuseValueDecision")),
                            _s(rv.get("refuseValueLabel")),
                            _s(rv.get("refuseValueReason")),
                            _s(rv.get("vetoAudit")),
                            bool(rv.get("vetoAuditActive")),
                            _s(rv.get("vetoAuditPolicy")),
                            row.get("id"),
                        ),
                    )
                    updated += int(cur.rowcount or 0)
                    if len(sample) < 10:
                        sample.append({"id": row.get("id"), "date": row.get("date"), "pick": row.get("predictedWinner"), "status": rv.get("refuseValueStatus")})
            conn.commit()
        return {"status": "ok", "dryRun": False, "scanned": len(rows), "updated": updated, "sample": sample}

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
        when legacy provider later reports retired/walkover/cancelled.
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
        """Return all rows for a legacy provider event id, all categories included."""
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


    def void_row_by_id(
        self,
        row_id: str,
        score: str = "",
        winner_id: str = "",
        *,
        reason: str = "api_tennis_legacy_name_void",
        real_winner: str = "",
    ) -> int:
        """Mark one row as void/refunded by row id.

        Used by API-Tennis legacy-name fallback when old legacy provider rows
        cannot be matched by event id after the provider migration.
        """
        if not row_id:
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
                    WHERE id = %s
                      AND result <> 'void'
                    """,
                    (_s(real_winner), today_paris().isoformat(), _s(reason) or "api_tennis_legacy_name_void", _s(score), _s(winner_id), _s(row_id)),
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


    def repair_shelton_merida_20260525(self) -> Dict[str, Any]:
        """One-time safe repair for Daniel Merida / Ben Shelton, 2026-05-25.

        STEP40 could not resolve this legacy legacy provider row through API-Tennis
        name fallback. The official result was Ben Shelton defeating Daniel
        Merida in straight sets. This method only touches the exact pending
        row id and leaves every other historical line unchanged.
        """
        row_id = "2026-05-25__sr:sport_event:71642350"
        real_winner = "Ben Shelton"
        score = "6-3 6-3 6-4"
        winner_id = "api_tennis:player:2837"
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, date, status, predicted_winner, opponent, result, real_winner, score
                    FROM {self.TABLE}
                    WHERE id = %s
                    """,
                    (row_id,),
                )
                before_row = cur.fetchone()
                if not before_row:
                    return {
                        "status": "not_found",
                        "repair": "shelton_merida_20260525",
                        "rowId": row_id,
                        "updatedRows": 0,
                        "policy": "Exact row not found; no change applied.",
                    }

                before = {
                    "id": _s(before_row[0]),
                    "date": _s(before_row[1]),
                    "status": _s(before_row[2]),
                    "category": _s(before_row[2]),
                    "predictedWinner": _s(before_row[3]),
                    "opponent": _s(before_row[4]),
                    "result": _s(before_row[5]),
                    "realWinner": _s(before_row[6]),
                    "score": _s(before_row[7]),
                }

                result = "win" if same_player(before["predictedWinner"], real_winner) else "loss"
                cur.execute(
                    f"""
                    UPDATE {self.TABLE}
                    SET result = %s,
                        real_winner = %s,
                        settled_at = %s,
                        settle_source = %s,
                        score = %s,
                        winner_id = %s,
                        updated_at = NOW()
                    WHERE id = %s
                      AND result = 'pending'
                    """,
                    (result, real_winner, today_paris().isoformat(), "manual_step41_shelton_merida", score, winner_id, row_id),
                )
                updated = int(cur.rowcount or 0)
                cur.execute(
                    f"""
                    SELECT id, date, status, predicted_winner, opponent, result, real_winner, score
                    FROM {self.TABLE}
                    WHERE id = %s
                    """,
                    (row_id,),
                )
                after_row = cur.fetchone()
                after = {
                    "id": _s(after_row[0]),
                    "date": _s(after_row[1]),
                    "status": _s(after_row[2]),
                    "category": _s(after_row[2]),
                    "predictedWinner": _s(after_row[3]),
                    "opponent": _s(after_row[4]),
                    "result": _s(after_row[5]),
                    "realWinner": _s(after_row[6]),
                    "score": _s(after_row[7]),
                } if after_row else {}
            conn.commit()
        return {
            "status": "ok",
            "repair": "shelton_merida_20260525",
            "rowId": row_id,
            "updatedRows": updated,
            "before": before,
            "after": after,
            "policy": "Correctif manuel ciblé : Merida/Shelton a été joué; Shelton gagnant 6-3 6-3 6-4; aucune autre ligne modifiée.",
        }


    def repair_wawrinka_fils_dejong_20260525(self) -> Dict[str, Any]:
        """One-time safe repair for Wawrinka/Fils replaced by De Jong, 2026-05-25.

        The original betting/engine line was understood as Wawrinka vs Arthur Fils.
        Arthur Fils withdrew before the match and Jesper De Jong replaced him as
        lucky loser. The stored legacy line later settled Wawrinka vs De Jong as
        a loss, but financially the original Wawrinka/Fils market must be void /
        refunded. This method only touches the exact historical row.
        """
        row_id = "2026-05-25__sr:sport_event:71684570"
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, date, status, source_player_a, source_player_b,
                           predicted_winner, opponent, result, real_winner, score, odd_predicted, odd_opponent
                    FROM {self.TABLE}
                    WHERE id = %s
                    """,
                    (row_id,),
                )
                before_row = cur.fetchone()
                if not before_row:
                    return {
                        "status": "not_found",
                        "repair": "wawrinka_fils_dejong_20260525",
                        "rowId": row_id,
                        "updatedRows": 0,
                        "policy": "Exact row not found; no change applied.",
                    }

                before = {
                    "id": _s(before_row[0]),
                    "date": _s(before_row[1]),
                    "status": _s(before_row[2]),
                    "category": _s(before_row[2]),
                    "sourcePlayerA": _s(before_row[3]),
                    "sourcePlayerB": _s(before_row[4]),
                    "predictedWinner": _s(before_row[5]),
                    "opponent": _s(before_row[6]),
                    "result": _s(before_row[7]),
                    "realWinner": _s(before_row[8]),
                    "score": _s(before_row[9]),
                    "oddPredicted": _s(before_row[10]),
                    "oddOpponent": _s(before_row[11]),
                }

                cur.execute(
                    f"""
                    UPDATE {self.TABLE}
                    SET result = %s,
                        real_winner = %s,
                        settled_at = %s,
                        settle_source = %s,
                        score = %s,
                        winner_id = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        "void",
                        "Jesper De Jong",
                        today_paris().isoformat(),
                        "manual_step42_replaced_opponent_fils_with_dejong_void",
                        "6-3 3-6 6-3 6-4",
                        "api_tennis:player:412",
                        row_id,
                    ),
                )
                updated = int(cur.rowcount or 0)

                cur.execute(
                    f"""
                    SELECT id, date, status, source_player_a, source_player_b,
                           predicted_winner, opponent, result, real_winner, score, odd_predicted, odd_opponent
                    FROM {self.TABLE}
                    WHERE id = %s
                    """,
                    (row_id,),
                )
                after_row = cur.fetchone()
                after = {
                    "id": _s(after_row[0]),
                    "date": _s(after_row[1]),
                    "status": _s(after_row[2]),
                    "category": _s(after_row[2]),
                    "sourcePlayerA": _s(after_row[3]),
                    "sourcePlayerB": _s(after_row[4]),
                    "predictedWinner": _s(after_row[5]),
                    "opponent": _s(after_row[6]),
                    "result": _s(after_row[7]),
                    "realWinner": _s(after_row[8]),
                    "score": _s(after_row[9]),
                    "oddPredicted": _s(after_row[10]),
                    "oddOpponent": _s(after_row[11]),
                } if after_row else {}
            conn.commit()

        return {
            "status": "ok",
            "repair": "wawrinka_fils_dejong_20260525",
            "rowId": row_id,
            "updatedRows": updated,
            "before": before,
            "after": after,
            "policy": "Correctif manuel ciblé : le marché initial Wawrinka/Fils est remboursé car Fils a été remplacé par De Jong; aucune transformation automatique du pick vers le nouveau match.",
        }


    def repair_van_assche_kypson_gaubas_20260525(self) -> Dict[str, Any]:
        """One-time safe repair for Van Assche/Kypson replaced by Gaubas, 2026-05-25.

        The original betting/engine line was understood as Luca Van Assche vs
        Patrick Kypson. Kypson withdrew before the match and Vilius Gaubas
        replaced him as lucky loser. The stored legacy line Van Assche/Gaubas
        later settled as a loss because Gaubas was the stored pick, but
        financially the initial Van Assche/Kypson market must be void/refunded.
        This method only touches the exact historical Van Assche/Gaubas row.
        """
        row_id = "2026-05-25__sr:sport_event:71719174"
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, date, status, source_player_a, source_player_b,
                           predicted_winner, opponent, result, real_winner, score, odd_predicted, odd_opponent
                    FROM {self.TABLE}
                    WHERE id = %s
                    """,
                    (row_id,),
                )
                before_row = cur.fetchone()
                if not before_row:
                    return {
                        "status": "not_found",
                        "repair": "van_assche_kypson_gaubas_20260525",
                        "rowId": row_id,
                        "updatedRows": 0,
                        "policy": "Exact row not found; no change applied.",
                    }

                before = {
                    "id": _s(before_row[0]),
                    "date": _s(before_row[1]),
                    "status": _s(before_row[2]),
                    "category": _s(before_row[2]),
                    "sourcePlayerA": _s(before_row[3]),
                    "sourcePlayerB": _s(before_row[4]),
                    "predictedWinner": _s(before_row[5]),
                    "opponent": _s(before_row[6]),
                    "result": _s(before_row[7]),
                    "realWinner": _s(before_row[8]),
                    "score": _s(before_row[9]),
                    "oddPredicted": _s(before_row[10]),
                    "oddOpponent": _s(before_row[11]),
                }

                cur.execute(
                    f"""
                    UPDATE {self.TABLE}
                    SET result = %s,
                        real_winner = %s,
                        settled_at = %s,
                        settle_source = %s,
                        score = %s,
                        winner_id = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        "void",
                        "Luca van Assche",
                        today_paris().isoformat(),
                        "manual_step43_replaced_opponent_kypson_with_gaubas_void",
                        "6-4 6-2 2-6 7-5",
                        "api_tennis:player:1066",
                        row_id,
                    ),
                )
                updated = int(cur.rowcount or 0)

                cur.execute(
                    f"""
                    SELECT id, date, status, source_player_a, source_player_b,
                           predicted_winner, opponent, result, real_winner, score, odd_predicted, odd_opponent
                    FROM {self.TABLE}
                    WHERE id = %s
                    """,
                    (row_id,),
                )
                after_row = cur.fetchone()
                after = {
                    "id": _s(after_row[0]),
                    "date": _s(after_row[1]),
                    "status": _s(after_row[2]),
                    "category": _s(after_row[2]),
                    "sourcePlayerA": _s(after_row[3]),
                    "sourcePlayerB": _s(after_row[4]),
                    "predictedWinner": _s(after_row[5]),
                    "opponent": _s(after_row[6]),
                    "result": _s(after_row[7]),
                    "realWinner": _s(after_row[8]),
                    "score": _s(after_row[9]),
                    "oddPredicted": _s(after_row[10]),
                    "oddOpponent": _s(after_row[11]),
                } if after_row else {}
            conn.commit()

        return {
            "status": "ok",
            "repair": "van_assche_kypson_gaubas_20260525",
            "rowId": row_id,
            "updatedRows": updated,
            "before": before,
            "after": after,
            "policy": "Correctif manuel ciblé : le marché initial Van Assche/Kypson est remboursé car Kypson a été remplacé par Gaubas; aucune transformation automatique du pick vers le nouveau match.",
        }


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


    def form_value_report(self, category: Optional[str] = "ALL", limit: Optional[int] = None) -> Dict[str, Any]:
        """STEP34: active Form/Value layer from persistent history.

        Uses the already settled historical categories (PREMIUM / PROCHE / VETO / REFUSE)
        and compares win/loss/void/pending, odds, ROI and engine signals.
        This is intentionally multi-year: no day-window limit by default.
        """
        cat = normalize_category(category, default="ALL")
        rows = self.fetch_rows(limit=limit, category=cat)
        return build_form_value_report(rows, category=cat)

    def summary(self, category: Optional[str] = "PREMIUM") -> Dict[str, Any]:
        cat = normalize_category(category)
        rows = self.fetch_rows(limit=None, category=cat)
        return build_premium_summary(rows, category=cat)



FORM_VALUE_SIGNAL_KEYS = [
    "premiumPct",
    "pSwe",
    "pAtp",
    "pForm5",
    "pForm10",
    "pSurfaceForm5",
    "pDominance",
]


def _row_signal_value(row: Dict[str, Any], key: str) -> Optional[float]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    aliases = {
        "premiumPct": ["premiumPct", "premium_pct", "premium"],
        "pSwe": ["pSwe", "p_swe"],
        "pAtp": ["pAtp", "p_atp"],
        "pForm5": ["pForm5", "p_form5"],
        "pForm10": ["pForm10", "p_form10"],
        "pSurfaceForm5": ["pSurfaceForm5", "p_surface_form5"],
        "pDominance": ["pDominance", "p_dominance"],
    }
    for source in (row, raw):
        for name in aliases.get(key, [key]):
            if name in source and source.get(name) not in (None, ""):
                try:
                    val = float(str(source.get(name)).replace(",", "."))
                    if key == "premiumPct" and 0.0 <= val <= 1.0:
                        val *= 100.0
                    return val
                except Exception:
                    continue
    return None


def _row_odd(row: Dict[str, Any]) -> float:
    return _f(row.get("oddPredicted"), 0.0)


def _new_form_bucket(label: str) -> Dict[str, Any]:
    return {
        "label": label,
        "total": 0,
        "settled": 0,
        "wins": 0,
        "losses": 0,
        "void": 0,
        "voids": 0,
        "refunded": 0,
        "pending": 0,
        "oddsUsed": 0,
        "oddSum": 0.0,
        "profitUnits": 0.0,
        "profitEur": 0.0,
        "signalsSum": {k: 0.0 for k in FORM_VALUE_SIGNAL_KEYS},
        "signalsCount": {k: 0 for k in FORM_VALUE_SIGNAL_KEYS},
        "signalsAvg": {},
    }


def _add_row_to_form_bucket(bucket: Dict[str, Any], row: Dict[str, Any]) -> None:
    result = normalize_result(row.get("result"))
    odd = _row_odd(row)
    bucket["total"] += 1

    if result == "win":
        bucket["wins"] += 1
        bucket["settled"] += 1
        if odd > 1.0:
            bucket["oddsUsed"] += 1
            bucket["oddSum"] += odd
            bucket["profitUnits"] += odd - 1.0
            bucket["profitEur"] += STAKE_EUR * (odd - 1.0)
    elif result == "loss":
        bucket["losses"] += 1
        bucket["settled"] += 1
        if odd > 1.0:
            bucket["oddsUsed"] += 1
            bucket["oddSum"] += odd
            bucket["profitUnits"] -= 1.0
        bucket["profitEur"] -= STAKE_EUR
    elif result == "void":
        bucket["void"] += 1
        bucket["voids"] += 1
        bucket["refunded"] += 1
    else:
        bucket["pending"] += 1

    for key in FORM_VALUE_SIGNAL_KEYS:
        val = _row_signal_value(row, key)
        if val is None:
            continue
        bucket["signalsSum"][key] += float(val)
        bucket["signalsCount"][key] += 1


def _finalize_form_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    settled = int(bucket.get("settled") or 0)
    wins = int(bucket.get("wins") or 0)
    odds_used = int(bucket.get("oddsUsed") or 0)
    avg_odd = round(float(bucket.get("oddSum") or 0.0) / odds_used, 3) if odds_used else 0.0
    break_even = round(100.0 / avg_odd, 2) if avg_odd > 1.0 else 0.0
    win_rate = round((wins / settled) * 100.0, 2) if settled else 0.0
    roi = round((float(bucket.get("profitUnits") or 0.0) / odds_used) * 100.0, 2) if odds_used else 0.0
    edge = round(win_rate - break_even, 2) if break_even else 0.0

    avgs: Dict[str, float] = {}
    for key in FORM_VALUE_SIGNAL_KEYS:
        cnt = int(bucket["signalsCount"].get(key) or 0)
        avgs[key] = round(float(bucket["signalsSum"].get(key) or 0.0) / cnt, 4) if cnt else 0.0

    bucket["signalsAvg"] = avgs
    bucket["avgOdd"] = avg_odd
    bucket["breakEvenPct"] = break_even
    bucket["winRatePct"] = win_rate
    bucket["historicalEdgePct"] = edge
    bucket["profitUnits"] = round(float(bucket.get("profitUnits") or 0.0), 3)
    bucket["profitEur"] = round(float(bucket.get("profitEur") or 0.0), 2)
    bucket["roiPct"] = roi
    bucket["activeSample"] = settled >= 3 and odds_used >= 3

    if not bucket["activeSample"]:
        bucket["valueGrade"] = "INSUFFICIENT_SAMPLE"
        bucket["recommendation"] = "WAIT"
    elif roi > 0 and edge > 0:
        bucket["valueGrade"] = "POSITIVE_VALUE"
        bucket["recommendation"] = "PROMOTE"
    elif roi < 0 and edge < 0:
        bucket["valueGrade"] = "NEGATIVE_VALUE"
        bucket["recommendation"] = "DOWNGRADE"
    else:
        bucket["valueGrade"] = "MIXED_VALUE"
        bucket["recommendation"] = "KEEP"

    # Internal sums are not useful in API responses.
    bucket.pop("signalsSum", None)
    bucket.pop("signalsCount", None)
    bucket.pop("oddSum", None)
    return bucket


def _form_signal_fit(match_signals: Dict[str, float], reference: Dict[str, Any]) -> float:
    avgs = reference.get("signalsAvg") if isinstance(reference, dict) else {}
    if not isinstance(avgs, dict):
        return 0.0
    total = 0.0
    count = 0
    for key in ("pSwe", "pAtp", "pForm5", "pForm10", "pSurfaceForm5", "pDominance"):
        mv = match_signals.get(key)
        rv = avgs.get(key)
        if mv is None or rv in (None, ""):
            continue
        try:
            mvf = float(mv)
            rvf = float(rv)
        except Exception:
            continue
        # Signals are mostly 0..1. Similarity 100 = same, 0 = far.
        total += max(0.0, 1.0 - min(1.0, abs(mvf - rvf))) * 100.0
        count += 1
    return round(total / count, 2) if count else 0.0


def build_form_value_report(rows: List[Dict[str, Any]], category: Optional[str] = "ALL") -> Dict[str, Any]:
    cat_filter = normalize_category(category, default="ALL")
    categories: Dict[str, Dict[str, Any]] = {}
    for cat in ("PREMIUM", "PROCHE", "VETO", "REFUSE"):
        categories[cat] = {
            "all": _new_form_bucket("all"),
            "wins": _new_form_bucket("wins"),
            "losses": _new_form_bucket("losses"),
            "voids": _new_form_bucket("voids"),
            "pending": _new_form_bucket("pending"),
        }

    selected_rows = []
    for row in rows or []:
        cat = row_category(row)
        if cat_filter != "ALL" and cat != cat_filter:
            continue
        selected_rows.append(row)
        result = normalize_result(row.get("result"))
        _add_row_to_form_bucket(categories[cat]["all"], row)
        if result == "win":
            _add_row_to_form_bucket(categories[cat]["wins"], row)
        elif result == "loss":
            _add_row_to_form_bucket(categories[cat]["losses"], row)
        elif result == "void":
            _add_row_to_form_bucket(categories[cat]["voids"], row)
        else:
            _add_row_to_form_bucket(categories[cat]["pending"], row)

    for cat in categories:
        for group in list(categories[cat].keys()):
            categories[cat][group] = _finalize_form_bucket(categories[cat][group])

    ranking = []
    for cat, groups in categories.items():
        all_bucket = groups["all"]
        ranking.append({
            "category": cat,
            "settled": all_bucket.get("settled"),
            "winRatePct": all_bucket.get("winRatePct"),
            "roiPct": all_bucket.get("roiPct"),
            "historicalEdgePct": all_bucket.get("historicalEdgePct"),
            "valueGrade": all_bucket.get("valueGrade"),
            "recommendation": all_bucket.get("recommendation"),
        })
    ranking.sort(key=lambda x: (float(x.get("roiPct") or 0.0), float(x.get("historicalEdgePct") or 0.0)), reverse=True)

    return {
        "status": "ok",
        "version": "step34-form-value-engine",
        "category": cat_filter,
        "rowsUsed": len(selected_rows),
        "signals": FORM_VALUE_SIGNAL_KEYS,
        "categories": categories,
        "ranking": ranking,
        "policy": "STEP34 actif : Form5/Form10/SurfaceForm5/Dominance/pSWE/pATP/premiumPct/cote sont reliés aux historiques multi-années pour détecter value positive/négative par catégorie.",
    }


def score_match_with_form_value(match: Dict[str, Any], category: str, report: Dict[str, Any]) -> Dict[str, Any]:
    cat = normalize_category(category, default="REFUSE")
    categories = report.get("categories") if isinstance(report, dict) else {}
    groups = categories.get(cat) if isinstance(categories, dict) else None
    if not isinstance(groups, dict):
        return {"formValueActive": False, "formValueCategory": cat, "formValueAction": "NO_HISTORY", "formValueLabel": "Historique indisponible"}

    base = groups.get("all") or {}
    wins = groups.get("wins") or {}
    losses = groups.get("losses") or {}
    odd = _f(match.get("oddA") or match.get("playerAOdd") or match.get("coteA"), 0.0)
    has_valid_odd = odd > 1.0
    implied = round(100.0 / odd, 2) if has_valid_odd else 0.0

    match_signals = {key: (_row_signal_value(match, key) if isinstance(match, dict) else None) for key in FORM_VALUE_SIGNAL_KEYS}
    # _row_signal_value expects raw-compatible row; direct match values still work.
    match_signals = {k: (0.0 if v is None else float(v)) for k, v in match_signals.items()}

    win_fit = _form_signal_fit(match_signals, wins)
    loss_fit = _form_signal_fit(match_signals, losses)
    form_delta = round(win_fit - loss_fit, 2)
    hist_win_rate = float(base.get("winRatePct") or 0.0)
    hist_roi = float(base.get("roiPct") or 0.0)
    hist_edge = round(hist_win_rate - implied, 2) if implied else float(base.get("historicalEdgePct") or 0.0)
    settled = int(base.get("settled") or 0)
    active_sample = bool(base.get("activeSample"))

    value_score = round(hist_edge + (hist_roi * 0.25) + (form_delta * 0.15), 2)

    if not active_sample:
        action = "WAIT"
        label = "⏳ FORM — échantillon court"
        playable = False
    elif not has_valid_odd:
        # No market price = no value decision.  A Refusé with a strong historical
        # category must not be promoted if the implied probability cannot be
        # calculated from a real odd.
        action = "WAIT"
        label = "⏳ FORM — cote manquante"
        playable = False
    elif value_score >= 5.0 and hist_roi > 0:
        action = "PROMOTE"
        label = "✅ VALUE FORM"
        playable = True
    elif value_score <= -5.0 and hist_roi < 0:
        action = "DOWNGRADE"
        label = "❌ DANGER FORM"
        playable = False
    else:
        action = "KEEP"
        label = "⚖ FORM NEUTRE"
        playable = False

    return {
        "formValueActive": True,
        "formValueCategory": cat,
        "formValueAction": action,
        "formValuePlayable": playable,
        "formValueLabel": label,
        "formValueScore": value_score,
        "formValueWinRatePct": round(hist_win_rate, 2),
        "formValueRoiPct": round(hist_roi, 2),
        "formValueEdgePct": hist_edge,
        "formValueImpliedPct": implied,
        "formValueSettled": settled,
        "formValueWinFitPct": win_fit,
        "formValueLossFitPct": loss_fit,
        "formValueFormDeltaPct": form_delta,
        "formValueOddAvailable": bool(has_valid_odd),
        "formValueReason": (
            f"Catégorie {cat}: WR {hist_win_rate:.1f}% vs seuil cote {implied:.1f}%, ROI {hist_roi:.1f}%, fit win {win_fit:.1f} / loss {loss_fit:.1f}."
            if has_valid_odd
            else f"Catégorie {cat}: cote manquante, aucune promotion value autorisée; ROI hist {hist_roi:.1f}%, fit win {win_fit:.1f} / loss {loss_fit:.1f}."
        ),
    }

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
