#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor — V3.1.1 Learning Engine

Non-destructive intelligent layer above STEP56/STEP62.

V3.1.1 fixes the first V3.1 shadow audit:
- persistent learning memory table from PostgreSQL history;
- automatic shadow-rule generation from historical segments;
- shadow evaluation for daily/live candidates;
- zero replacement of the official motor until out-of-sample validation.

Policy:
- V2/STEP56 remains the official prediction engine.
- V3.1 learns, writes memory, creates shadow rules, and tests them.
- V3.1 does not change a bet decision automatically.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

STAKE_EUR = 100.0
DEFAULT_ODDS_CUTOFF = 1.90
FINAL_WIN = "win"
FINAL_LOSS = "loss"
FINAL_VOID = "void"
FINAL_PENDING = "pending"
V3_VERSION = "v3.1.1-qualification-priority-shadow-rules"


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm(value: Any) -> str:
    text = _s(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _f(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        try:
            x = float(value)
            return default if math.isnan(x) else x
        except Exception:
            return default
    text = _s(value).replace(",", ".")
    if text in {"", "-", "None", "null"}:
        return default
    try:
        return float(text)
    except Exception:
        m = re.search(r"(\d+(?:[\.,]\d+)?)", text)
        if not m:
            return default
        try:
            return float(m.group(1).replace(",", "."))
        except Exception:
            return default


def _b(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _norm(value)
    return text in {"1", "true", "yes", "oui", "ok", "y"}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _stable_hash(*parts: Any, length: int = 16) -> str:
    raw = "||".join(_s(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:length]


def normalize_result(value: Any) -> str:
    text = _norm(value)
    if not text:
        return FINAL_PENDING
    if any(tok in text for tok in ["void", "refund", "refunded", "remb", "rembours", "retired", "abandon", "walkover", "withdrawn", "forfeit", "cancel"]):
        return FINAL_VOID
    if any(tok in text for tok in ["pending", "attente", "scheduled", "not started", "open", "live", "inplay", "en cours"]):
        return FINAL_PENDING
    if any(tok in text for tok in ["loss", "lost", "lose", "perdu", "perdus", "defaite", "ko"]):
        return FINAL_LOSS
    if any(tok in text for tok in ["win", "won", "gagne", "gagnes", "victoire", "ok"]):
        return FINAL_WIN
    return FINAL_PENDING


def row_result(row: Dict[str, Any]) -> str:
    for key in [
        "result", "statusResult", "settledResult", "historyResult", "betResult",
        "matchResult", "pickResult", "premiumResult", "valueResult", "outcome",
        "finalResult",
    ]:
        if key in row:
            r = normalize_result(row.get(key))
            if r != FINAL_PENDING:
                return r
    return normalize_result(row.get("result"))


def row_category(row: Dict[str, Any]) -> str:
    candidates = [
        row.get("status"),
        row.get("category"),
        row.get("refuseValueCategoryBase"),
        row.get("step56OfficialCategory"),
        row.get("officialCategory"),
        row.get("decision"),
    ]
    text = " ".join(_s(x) for x in candidates if _s(x)).upper()
    if "PREMIUM" in text:
        return "PREMIUM"
    if "PROCHE" in text:
        return "PROCHE"
    if "VETO" in text:
        return "VETO"
    if "REFUSE" in text or "REFUS" in text or "STEP56_REFUSE" in text:
        return "REFUSE"
    raw = _s(row.get("status") or row.get("category") or "").upper()
    return raw if raw in {"PREMIUM", "PROCHE", "VETO", "REFUSE"} else "UNKNOWN"


def row_odd(row: Dict[str, Any]) -> float:
    for key in [
        "refuseValueOdd", "oddPredicted", "oddA", "playerAOdd", "coteA", "cote",
        "odds", "liveEntryOdd", "live_entry_odd", "odd", "bookmakerOdd",
    ]:
        odd = _f(row.get(key), 0.0)
        if odd > 1.0:
            return odd
    raw = row.get("raw") or {}
    if isinstance(raw, dict):
        for key in ["refuseValueOdd", "oddPredicted", "oddA", "playerAOdd", "coteA", "odd"]:
            odd = _f(raw.get(key), 0.0)
            if odd > 1.0:
                return odd
    return 0.0


def row_profit(row: Dict[str, Any], stake: float = STAKE_EUR) -> Tuple[float, float]:
    result = row_result(row)
    odd = row_odd(row)
    if result == FINAL_WIN:
        return ((odd - 1.0) * stake if odd > 1.0 else 0.0, stake)
    if result == FINAL_LOSS:
        return (-stake, stake)
    return (0.0, 0.0)


def _get_nested(row: Dict[str, Any], key: str) -> Any:
    if key in row:
        return row.get(key)
    raw = row.get("raw") or {}
    if isinstance(raw, dict):
        return raw.get(key)
    return None


def collect_text_fields(row: Dict[str, Any]) -> str:
    parts: List[str] = []
    direct_keys = [
        "round", "roundName", "round_name", "tournament", "seasonName", "source",
        "oddsSource", "eventName", "competitionName", "stage", "phase", "type",
        "league", "leagueName", "draw", "tourney_name", "tourney_level",
        "qualifierDetectorPolicy", "tournamentWinsPolicy",
    ]
    for key in direct_keys:
        value = row.get(key)
        if value:
            parts.append(_s(value))
    raw = row.get("raw") or {}
    if isinstance(raw, dict):
        for key in direct_keys:
            value = raw.get(key)
            if value:
                parts.append(_s(value))
    return " | ".join(parts)


def qualification_signals(row: Dict[str, Any]) -> List[str]:
    """Return explicit signals proving or strongly indicating a qualification draw.

    STEP V3.1 was too conservative and missed API-Tennis cases where the
    event is globally a qualification event but the round string is rendered as
    e.g. "ATP Wimbledon - Quarter-finals".  V3.1.1 treats those audit-candidate
    qualification flags as draw-level qualification signals while keeping them
    separate from the old clay veto policy.
    """
    signals: List[str] = []
    text = _norm(collect_text_fields(row))

    text_patterns = [
        (r"\bqualification\b", "text:qualification"),
        (r"\bqualifications\b", "text:qualifications"),
        (r"\bqualifying\b", "text:qualifying"),
        (r"\bqualifier\b", "text:qualifier"),
        (r"\bqualif\b", "text:qualif"),
        (r"\bqualifs\b", "text:qualifs"),
        (r"\bgentlemen s qualifying singles\b", "text:wimbledon_qualifying"),
        (r"\bmen s qualification\b", "text:mens_qualification"),
        (r"\bmens qualification\b", "text:mens_qualification"),
        (r"\bq[123]\b", "text:q_round"),
    ]
    for pattern, label in text_patterns:
        if re.search(pattern, text):
            signals.append(label)

    # Direct round codes when provider gives clean Q1/Q2/Q3.
    for key in ["round", "roundName", "round_name", "stage"]:
        round_text = _s(_get_nested(row, key))
        if re.fullmatch(r"\s*Q[123]\s*", round_text, flags=re.IGNORECASE):
            signals.append(f"round_code:{round_text.strip().upper()}")

    # API/engine qualification source.  IMPORTANT: a player can be a qualifier
    # inside a main draw; that alone is not enough to tag the match as a
    # qualification-round match.  We require a provider/source signal about the
    # event or phase, not merely player_a_is_qualifier=true.
    for side in ["a", "b"]:
        conf = _norm(_get_nested(row, f"player_{side}_qualifier_detection_confidence"))
        source = _norm(_get_nested(row, f"player_{side}_qualifier_source"))
        negative_source = (
            "no qualification flag" in source
            or "not detected" in source
            or source in {"", "api tennis no qualification flag"}
        )
        strong_source = (
            "event qualification global" in source
            or "qualification global" in source
            or "qualifying draw" in source
            or "qualification draw" in source
            or "qualification round" in source
        )
        if not negative_source and strong_source:
            signals.append(f"source:player_{side}_event_qualification")
            if "audit candidate" in conf or conf == "audit candidate" or conf == "audit_candidate":
                signals.append(f"audit_candidate:player_{side}")

    # API-Tennis sometimes marks an event as qualification globally, without a
    # player-level flag; the source string below is exactly the case seen in the
    # user's 22/06 logs.
    if "api tennis event qualification global" in text:
        signals.append("api_tennis:event_qualification_global")

    # De-duplicate while preserving order.
    return list(dict.fromkeys(signals))


def is_qualification(row: Dict[str, Any]) -> bool:
    return bool(qualification_signals(row))


def draw_type(row: Dict[str, Any]) -> str:
    return "QUALIFICATION" if is_qualification(row) else "MAIN_OR_UNKNOWN"

def parse_score_sets(score: Any) -> List[Tuple[int, int]]:
    score_text = _s(score)
    if not score_text:
        return []
    score_text = re.sub(r"\([^)]*\)", "", score_text)
    score_text = score_text.replace("–", "-").replace("—", "-")
    out: List[Tuple[int, int]] = []
    for a, b in re.findall(r"(\d{1,2})\s*[-/]\s*(\d{1,2})", score_text):
        ia, ib = int(a), int(b)
        if 0 <= ia <= 20 and 0 <= ib <= 20:
            out.append((ia, ib))
    return out


def score_features(row: Dict[str, Any]) -> Dict[str, Any]:
    score_value = row.get("score")
    raw = row.get("raw") or {}
    if not score_value and isinstance(raw, dict):
        score_value = raw.get("score") or raw.get("finalScore") or raw.get("matchScore")
    sets = parse_score_sets(score_value)
    tb_any = any(max(a, b) >= 7 and abs(a - b) <= 2 for a, b in sets)
    first = sets[0] if sets else None
    first_tb = bool(first and max(first) >= 7 and abs(first[0] - first[1]) <= 2)
    best_of_5_like = len(sets) >= 4
    return {
        "setsCount": len(sets),
        "hasScore": bool(sets),
        "hasTiebreakSet": tb_any,
        "firstSet": f"{first[0]}-{first[1]}" if first else "",
        "firstSetTiebreak": first_tb,
        "bestOf5Like": best_of_5_like,
        "sets": sets,
    }


def segment_grade(settled: int, roi_pct: float, profit: float, min_settled: int) -> str:
    if settled < min_settled:
        return "INSUFFICIENT_SAMPLE"
    if roi_pct >= 10.0 and profit > 0:
        return "PROMOTE_TEST"
    if roi_pct >= 3.0 and profit > 0:
        return "WATCH_POSITIVE"
    if roi_pct < 0.0:
        return "DOWNGRADE_OR_REJECT"
    return "NEUTRAL"


@dataclass
class SegmentStats:
    segment: str
    total: int = 0
    settled: int = 0
    wins: int = 0
    losses: int = 0
    voids: int = 0
    pending: int = 0
    staked: float = 0.0
    profitEur: float = 0.0
    oddsCount: int = 0
    oddsSum: float = 0.0
    minPremiumPct: float = 0.0
    maxPremiumPct: float = 0.0
    _premiumSum: float = 0.0

    def add(self, row: Dict[str, Any]) -> None:
        self.total += 1
        result = row_result(row)
        odd = row_odd(row)
        premium_pct = _f(row.get("premiumPct"), 0.0)
        if premium_pct > 0:
            self._premiumSum += premium_pct
            self.minPremiumPct = premium_pct if self.minPremiumPct <= 0 else min(self.minPremiumPct, premium_pct)
            self.maxPremiumPct = max(self.maxPremiumPct, premium_pct)
        if odd > 1.0:
            self.oddsCount += 1
            self.oddsSum += odd
        profit, staked = row_profit(row)
        self.profitEur += profit
        self.staked += staked
        if result == FINAL_WIN:
            self.wins += 1
            self.settled += 1
        elif result == FINAL_LOSS:
            self.losses += 1
            self.settled += 1
        elif result == FINAL_VOID:
            self.voids += 1
        else:
            self.pending += 1

    def finish(self, min_settled: int = 10) -> Dict[str, Any]:
        wr = (self.wins / self.settled * 100.0) if self.settled else 0.0
        roi = (self.profitEur / self.staked * 100.0) if self.staked > 0 else 0.0
        avg_odd = (self.oddsSum / self.oddsCount) if self.oddsCount else 0.0
        avg_pct = (self._premiumSum / self.total) if self.total else 0.0
        return {
            "segment": self.segment,
            "total": self.total,
            "settled": self.settled,
            "wins": self.wins,
            "losses": self.losses,
            "voids": self.voids,
            "pending": self.pending,
            "winRatePct": round(wr, 2),
            "profitEur": round(self.profitEur, 2),
            "stakedEur": round(self.staked, 2),
            "roiPct": round(roi, 2),
            "avgOdd": round(avg_odd, 3),
            "avgPremiumPct": round(avg_pct, 3),
            "minPremiumPct": round(self.minPremiumPct, 3),
            "maxPremiumPct": round(self.maxPremiumPct, 3),
            "grade": segment_grade(self.settled, roi, self.profitEur, min_settled),
        }


def row_segments(row: Dict[str, Any], odds_cutoff: float = DEFAULT_ODDS_CUTOFF) -> List[str]:
    cat = row_category(row)
    surface = _s(row.get("surface") or "UNKNOWN").upper().replace(" ", "_") or "UNKNOWN"
    draw = draw_type(row)
    odd = row_odd(row)
    score = score_features(row)
    pct = _f(row.get("premiumPct"), 0.0)

    segments = [
        "ALL",
        f"CATEGORY_{cat}",
        f"DRAW_{draw}",
        f"{cat}_{draw}",
        f"SURFACE_{surface}",
        f"{cat}_SURFACE_{surface}",
    ]

    if odd > 1.0:
        segments.append("ODDS_AVAILABLE")
        segments.append("ODDS_GT_1_90" if odd > odds_cutoff else "ODDS_LE_1_90")
        segments.append(f"{cat}_ODDS_GT_1_90" if odd > odds_cutoff else f"{cat}_ODDS_LE_1_90")
    else:
        segments.append("ODDS_MISSING")

    if score.get("hasTiebreakSet"):
        segments.append("TIEBREAK_ANY")
        segments.append(f"{cat}_TIEBREAK_ANY")
    if score.get("firstSetTiebreak"):
        segments.append("FIRST_SET_TIEBREAK")
        segments.append(f"{cat}_FIRST_SET_TIEBREAK")
    if score.get("bestOf5Like"):
        segments.append("BEST_OF_5_LIKE")
        segments.append(f"{cat}_BEST_OF_5_LIKE")

    if cat == "REFUSE":
        if _b(row.get("refuseValueStrict")):
            segments.append("REFUSE_VALUE_STRICT")
            segments.append(f"REFUSE_VALUE_STRICT_{draw}")
        if _b(row.get("refuseValueLarge")):
            segments.append("REFUSE_VALUE_LARGE")
            segments.append(f"REFUSE_VALUE_LARGE_{draw}")
        if _b(row.get("refuseValueCote180")):
            segments.append("REFUSE_COTE_180")
            segments.append(f"REFUSE_COTE_180_{draw}")
        if _b(row.get("refuseValueDanger") or row.get("refuseDanger")):
            segments.append("REFUSE_DANGER")

    if pct > 0:
        bucket_low = int(pct // 5) * 5
        bucket_high = bucket_low + 5
        segments.append(f"PREMIUM_PCT_{bucket_low}_{bucket_high}")
        segments.append(f"{cat}_PCT_{bucket_low}_{bucket_high}")

    return list(dict.fromkeys(segments))


def build_segment_stats(rows: Iterable[Dict[str, Any]], min_settled: int = 10, odds_cutoff: float = DEFAULT_ODDS_CUTOFF) -> List[Dict[str, Any]]:
    segs: Dict[str, SegmentStats] = {}
    for row in rows:
        for segment in row_segments(row, odds_cutoff=odds_cutoff):
            segs.setdefault(segment, SegmentStats(segment=segment)).add(row)
    return [s.finish(min_settled=min_settled) for s in segs.values()]


def dedupe_rows(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    duplicates = 0
    for row in rows:
        event_id = _s(row.get("sportradarSportEventId") or row.get("sport_event_id"))
        if event_id:
            key = "event:" + event_id + ":" + row_category(row)
        else:
            names = sorted([_norm(row.get("sourcePlayerA") or row.get("predictedWinner")), _norm(row.get("sourcePlayerB") or row.get("opponent"))])
            key = "fallback:" + _s(row.get("date")) + ":" + "|".join(names) + ":" + _norm(row.get("tournament") or row.get("seasonName")) + ":" + row_category(row)
        if key in seen:
            duplicates += 1
            old = seen[key]
            if row_result(old) == FINAL_PENDING and row_result(row) != FINAL_PENDING:
                seen[key] = row
        else:
            seen[key] = row
    return list(seen.values()), {"inputRows": len(rows), "dedupedRows": len(seen), "duplicatesRemoved": duplicates}


def build_v3_learning_report(
    rows: List[Dict[str, Any]],
    *,
    category: str = "ALL",
    min_settled: int = 10,
    odds_cutoff: float = DEFAULT_ODDS_CUTOFF,
    dedupe: bool = True,
    include_rows: bool = False,
) -> Dict[str, Any]:
    raw_rows = rows or []
    wanted = _s(category).upper()
    selected = list(raw_rows) if wanted in {"", "ALL", "TOUT", "*"} else [r for r in raw_rows if row_category(r) == wanted]
    working, dedupe_info = dedupe_rows(selected) if dedupe else (selected, {"inputRows": len(selected), "dedupedRows": len(selected), "duplicatesRemoved": 0})
    segments = build_segment_stats(working, min_settled=min_settled, odds_cutoff=odds_cutoff)
    segments.sort(key=lambda x: (x["settled"], x["profitEur"]), reverse=True)

    top_profit = sorted([s for s in segments if s["settled"] >= min_settled], key=lambda x: x["profitEur"], reverse=True)[:12]
    top_roi = sorted([s for s in segments if s["settled"] >= min_settled], key=lambda x: x["roiPct"], reverse=True)[:12]
    worst = sorted([s for s in segments if s["settled"] >= min_settled], key=lambda x: x["profitEur"])[:12]
    all_stats = next((s for s in segments if s["segment"] == "ALL"), None) or SegmentStats(segment="ALL").finish(min_settled=min_settled)
    hypotheses = make_learning_hypotheses(segments, min_settled=min_settled)

    result = {
        "status": "ok",
        "version": V3_VERSION,
        "policy": "V3 apprend en mémoire et teste en shadow mode; elle ne remplace pas la V2 automatiquement.",
        "category": wanted or "ALL",
        "minSettled": min_settled,
        "oddsCutoff": odds_cutoff,
        "dedupe": dedupe_info,
        "summary": all_stats,
        "segments": segments,
        "topProfitSegments": top_profit,
        "topRoiSegments": top_roi,
        "worstSegments": worst,
        "hypotheses": hypotheses,
    }
    if include_rows:
        result["rows"] = [compact_learning_row(r) for r in working]
    return result


def make_learning_hypotheses(segments: List[Dict[str, Any]], min_settled: int) -> List[Dict[str, Any]]:
    interesting = {
        "PREMIUM_QUALIFICATION", "PREMIUM_MAIN_OR_UNKNOWN", "REFUSE_QUALIFICATION", "REFUSE_MAIN_OR_UNKNOWN",
        "REFUSE_VALUE_STRICT", "REFUSE_VALUE_LARGE", "REFUSE_VALUE_STRICT_QUALIFICATION", "REFUSE_VALUE_LARGE_QUALIFICATION",
        "TIEBREAK_ANY", "CATEGORY_PREMIUM", "CATEGORY_REFUSE", "PREMIUM_TIEBREAK_ANY", "REFUSE_TIEBREAK_ANY",
        "ODDS_LE_1_90", "ODDS_GT_1_90", "PREMIUM_ODDS_LE_1_90", "PREMIUM_ODDS_GT_1_90",
        "REFUSE_ODDS_LE_1_90", "REFUSE_ODDS_GT_1_90",
    }
    by_name = {s["segment"]: s for s in segments}
    out: List[Dict[str, Any]] = []
    for name in sorted(interesting):
        s = by_name.get(name)
        if not s:
            continue
        if s["settled"] < min_settled:
            decision = "ATTENDRE"
            reason = f"échantillon insuffisant ({s['settled']} réglés)"
        elif s["roiPct"] > 5.0 and s["profitEur"] > 0:
            decision = "TESTER_EN_SHADOW"
            reason = "segment positif; à tester sur nouveaux matchs sans remplacer V2"
        elif s["roiPct"] < 0.0:
            decision = "DOWNGRADE"
            reason = "segment négatif dans l'historique propre"
        else:
            decision = "SURVEILLER"
            reason = "segment neutre ou faiblement positif"
        out.append({"segment": name, "decision": decision, "reason": reason, "stats": s})
    return out


def compact_learning_row(row: Dict[str, Any]) -> Dict[str, Any]:
    score = score_features(row)
    return {
        "historyId": row.get("id"),
        "date": row.get("date"),
        "category": row_category(row),
        "drawType": draw_type(row),
        "isQualification": is_qualification(row),
        "qualificationSignals": qualification_signals(row),
        "playerA": row.get("predictedWinner") or row.get("sourcePlayerA") or row.get("playerA"),
        "playerB": row.get("opponent") or row.get("sourcePlayerB") or row.get("playerB"),
        "surface": row.get("surface"),
        "premiumPct": _f(row.get("premiumPct"), 0.0),
        "odd": row_odd(row),
        "result": row_result(row),
        "score": row.get("score"),
        "scoreFeatures": score,
        "segments": row_segments(row),
    }


class V3LearningMemoryStore:
    MEMORY_TABLE = os.environ.get("TENNIS_MOTOR_V3_MEMORY_TABLE", "tennis_v3_learning_memory")
    RULES_TABLE = os.environ.get("TENNIS_MOTOR_V3_RULES_TABLE", "tennis_v3_shadow_rules")
    DECISIONS_TABLE = os.environ.get("TENNIS_MOTOR_V3_DECISIONS_TABLE", "tennis_v3_shadow_decisions")

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
                    CREATE TABLE IF NOT EXISTS {self.MEMORY_TABLE} (
                        history_id TEXT PRIMARY KEY,
                        history_date TEXT,
                        category TEXT,
                        draw_type TEXT,
                        is_qualification BOOLEAN NOT NULL DEFAULT FALSE,
                        surface TEXT,
                        player_a TEXT,
                        player_b TEXT,
                        predicted_winner TEXT,
                        opponent TEXT,
                        premium_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
                        odd DOUBLE PRECISION NOT NULL DEFAULT 0,
                        result TEXT NOT NULL DEFAULT 'pending',
                        profit_eur DOUBLE PRECISION NOT NULL DEFAULT 0,
                        staked_eur DOUBLE PRECISION NOT NULL DEFAULT 0,
                        tournament TEXT,
                        round TEXT,
                        score TEXT,
                        has_tiebreak_set BOOLEAN NOT NULL DEFAULT FALSE,
                        first_set TEXT,
                        first_set_tiebreak BOOLEAN NOT NULL DEFAULT FALSE,
                        best_of_5_like BOOLEAN NOT NULL DEFAULT FALSE,
                        segments_json TEXT NOT NULL DEFAULT '[]',
                        source_row_json TEXT NOT NULL DEFAULT '{{}}',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.MEMORY_TABLE}_category ON {self.MEMORY_TABLE}(category)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.MEMORY_TABLE}_draw_type ON {self.MEMORY_TABLE}(draw_type)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.MEMORY_TABLE}_result ON {self.MEMORY_TABLE}(result)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.MEMORY_TABLE}_history_date ON {self.MEMORY_TABLE}(history_date)")
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.RULES_TABLE} (
                        rule_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'shadow',
                        action TEXT NOT NULL,
                        category TEXT,
                        include_segments_json TEXT NOT NULL DEFAULT '[]',
                        exclude_segments_json TEXT NOT NULL DEFAULT '[]',
                        min_premium_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
                        max_premium_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
                        min_odd DOUBLE PRECISION NOT NULL DEFAULT 0,
                        max_odd DOUBLE PRECISION NOT NULL DEFAULT 0,
                        min_settled INTEGER NOT NULL DEFAULT 0,
                        source_segment TEXT NOT NULL,
                        train_settled INTEGER NOT NULL DEFAULT 0,
                        train_wins INTEGER NOT NULL DEFAULT 0,
                        train_losses INTEGER NOT NULL DEFAULT 0,
                        train_win_rate_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
                        train_roi_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
                        train_profit_eur DOUBLE PRECISION NOT NULL DEFAULT 0,
                        confidence TEXT,
                        reason TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_tested_at TIMESTAMPTZ,
                        promoted_at TIMESTAMPTZ
                    )
                    """
                )
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.RULES_TABLE}_status ON {self.RULES_TABLE}(status)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.RULES_TABLE}_source_segment ON {self.RULES_TABLE}(source_segment)")
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.DECISIONS_TABLE} (
                        decision_id TEXT PRIMARY KEY,
                        rule_id TEXT,
                        match_key TEXT NOT NULL,
                        day TEXT,
                        player_a TEXT,
                        player_b TEXT,
                        category TEXT,
                        shadow_decision TEXT NOT NULL,
                        reason TEXT,
                        result TEXT NOT NULL DEFAULT 'pending',
                        source_payload_json TEXT NOT NULL DEFAULT '{{}}',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.DECISIONS_TABLE}_day ON {self.DECISIONS_TABLE}(day)")
                cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.DECISIONS_TABLE}_rule_id ON {self.DECISIONS_TABLE}(rule_id)")
            conn.commit()

    def status(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"databaseConfigured": False, "databaseStatus": "not_configured"}
        try:
            self.ensure_schema()
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {self.MEMORY_TABLE}")
                    memory_count = int(cur.fetchone()[0] or 0)
                    cur.execute(f"SELECT COUNT(*) FROM {self.RULES_TABLE}")
                    rules_count = int(cur.fetchone()[0] or 0)
                    cur.execute(f"SELECT COUNT(*) FROM {self.DECISIONS_TABLE}")
                    decisions_count = int(cur.fetchone()[0] or 0)
            return {
                "status": "ok",
                "databaseConfigured": True,
                "databaseStatus": "ok",
                "memoryTable": self.MEMORY_TABLE,
                "rulesTable": self.RULES_TABLE,
                "decisionsTable": self.DECISIONS_TABLE,
                "memoryRows": memory_count,
                "shadowRules": rules_count,
                "shadowDecisions": decisions_count,
            }
        except Exception as exc:
            return {"status": "error", "databaseConfigured": self.enabled, "databaseStatus": "error", "error": f"{type(exc).__name__}: {exc}"}

    def upsert_memory_rows(self, rows: Sequence[Dict[str, Any]], odds_cutoff: float = DEFAULT_ODDS_CUTOFF) -> Dict[str, Any]:
        self.ensure_schema()
        inserted_or_updated = 0
        skipped = 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                for row in rows:
                    history_id = _s(row.get("id") or row.get("historyId"))
                    if not history_id:
                        skipped += 1
                        continue
                    score = score_features(row)
                    profit, staked = row_profit(row)
                    segments = row_segments(row, odds_cutoff=odds_cutoff)
                    cur.execute(
                        f"""
                        INSERT INTO {self.MEMORY_TABLE} (
                            history_id, history_date, category, draw_type, is_qualification, surface,
                            player_a, player_b, predicted_winner, opponent, premium_pct, odd, result,
                            profit_eur, staked_eur, tournament, round, score, has_tiebreak_set,
                            first_set, first_set_tiebreak, best_of_5_like, segments_json, source_row_json, updated_at
                        ) VALUES (
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()
                        )
                        ON CONFLICT (history_id) DO UPDATE SET
                            history_date = EXCLUDED.history_date,
                            category = EXCLUDED.category,
                            draw_type = EXCLUDED.draw_type,
                            is_qualification = EXCLUDED.is_qualification,
                            surface = EXCLUDED.surface,
                            player_a = EXCLUDED.player_a,
                            player_b = EXCLUDED.player_b,
                            predicted_winner = EXCLUDED.predicted_winner,
                            opponent = EXCLUDED.opponent,
                            premium_pct = EXCLUDED.premium_pct,
                            odd = EXCLUDED.odd,
                            result = EXCLUDED.result,
                            profit_eur = EXCLUDED.profit_eur,
                            staked_eur = EXCLUDED.staked_eur,
                            tournament = EXCLUDED.tournament,
                            round = EXCLUDED.round,
                            score = EXCLUDED.score,
                            has_tiebreak_set = EXCLUDED.has_tiebreak_set,
                            first_set = EXCLUDED.first_set,
                            first_set_tiebreak = EXCLUDED.first_set_tiebreak,
                            best_of_5_like = EXCLUDED.best_of_5_like,
                            segments_json = EXCLUDED.segments_json,
                            source_row_json = EXCLUDED.source_row_json,
                            updated_at = NOW()
                        """,
                        (
                            history_id,
                            _s(row.get("date")),
                            row_category(row),
                            draw_type(row),
                            is_qualification(row),
                            _s(row.get("surface")),
                            _s(row.get("sourcePlayerA") or row.get("playerA") or row.get("predictedWinner")),
                            _s(row.get("sourcePlayerB") or row.get("playerB") or row.get("opponent")),
                            _s(row.get("predictedWinner") or row.get("playerA")),
                            _s(row.get("opponent") or row.get("playerB")),
                            _f(row.get("premiumPct"), 0.0),
                            row_odd(row),
                            row_result(row),
                            profit,
                            staked,
                            _s(row.get("tournament") or row.get("seasonName")),
                            _s(row.get("round")),
                            _s(row.get("score")),
                            bool(score.get("hasTiebreakSet")),
                            _s(score.get("firstSet")),
                            bool(score.get("firstSetTiebreak")),
                            bool(score.get("bestOf5Like")),
                            _json_dumps(segments),
                            _json_dumps(row),
                        ),
                    )
                    inserted_or_updated += 1
            conn.commit()
        return {"status": "ok", "memoryRowsWritten": inserted_or_updated, "skippedRows": skipped}

    def fetch_memory_rows(self, limit: int = 50000, category: Optional[str] = None) -> List[Dict[str, Any]]:
        self.ensure_schema()
        params: List[Any] = []
        where = ""
        if category and _s(category).upper() not in {"ALL", "TOUT", "*"}:
            where = " WHERE category = %s"
            params.append(_s(category).upper())
        sql = f"""
            SELECT history_id, history_date, category, draw_type, is_qualification, surface,
                   player_a, player_b, predicted_winner, opponent, premium_pct, odd, result,
                   profit_eur, staked_eur, tournament, round, score, has_tiebreak_set,
                   first_set, first_set_tiebreak, best_of_5_like, segments_json, source_row_json
            FROM {self.MEMORY_TABLE}{where}
            ORDER BY history_date DESC NULLS LAST, updated_at DESC
            LIMIT %s
        """
        params.append(int(limit))
        rows: List[Dict[str, Any]] = []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                cols = [d[0] for d in cur.description]
                for db_row in cur.fetchall():
                    r = dict(zip(cols, db_row))
                    source = _json_loads(r.get("source_row_json"), {})
                    if isinstance(source, dict) and source:
                        rows.append(source)
                    else:
                        rows.append({
                            "id": r.get("history_id"),
                            "date": r.get("history_date"),
                            "status": r.get("category"),
                            "surface": r.get("surface"),
                            "sourcePlayerA": r.get("player_a"),
                            "sourcePlayerB": r.get("player_b"),
                            "predictedWinner": r.get("predicted_winner"),
                            "opponent": r.get("opponent"),
                            "premiumPct": r.get("premium_pct"),
                            "oddPredicted": r.get("odd"),
                            "result": r.get("result"),
                            "tournament": r.get("tournament"),
                            "round": r.get("round"),
                            "score": r.get("score"),
                        })
        return rows

    def upsert_shadow_rules(self, rules: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        self.ensure_schema()
        written = 0
        deprecated = 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                # V3.1.1 cleanup: old automatic rules such as DRAW_MAIN_OR_UNKNOWN
                # must not stay active forever.  Deactivate all v3_auto_* rules,
                # then reactivate only the freshly generated rule set below.
                cur.execute(
                    f"""
                    UPDATE {self.RULES_TABLE}
                    SET status = 'deprecated', updated_at = NOW()
                    WHERE rule_id LIKE 'v3_auto_%' AND status = 'shadow'
                    """
                )
                deprecated = int(cur.rowcount or 0)
                for rule in rules:
                    cur.execute(
                        f"""
                        INSERT INTO {self.RULES_TABLE} (
                            rule_id, name, status, action, category, include_segments_json, exclude_segments_json,
                            min_premium_pct, max_premium_pct, min_odd, max_odd, min_settled, source_segment,
                            train_settled, train_wins, train_losses, train_win_rate_pct, train_roi_pct,
                            train_profit_eur, confidence, reason, updated_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                        ON CONFLICT (rule_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            status = EXCLUDED.status,
                            action = EXCLUDED.action,
                            category = EXCLUDED.category,
                            include_segments_json = EXCLUDED.include_segments_json,
                            exclude_segments_json = EXCLUDED.exclude_segments_json,
                            min_premium_pct = EXCLUDED.min_premium_pct,
                            max_premium_pct = EXCLUDED.max_premium_pct,
                            min_odd = EXCLUDED.min_odd,
                            max_odd = EXCLUDED.max_odd,
                            min_settled = EXCLUDED.min_settled,
                            source_segment = EXCLUDED.source_segment,
                            train_settled = EXCLUDED.train_settled,
                            train_wins = EXCLUDED.train_wins,
                            train_losses = EXCLUDED.train_losses,
                            train_win_rate_pct = EXCLUDED.train_win_rate_pct,
                            train_roi_pct = EXCLUDED.train_roi_pct,
                            train_profit_eur = EXCLUDED.train_profit_eur,
                            confidence = EXCLUDED.confidence,
                            reason = EXCLUDED.reason,
                            updated_at = NOW()
                        """,
                        (
                            rule["ruleId"], rule["name"], rule.get("status", "shadow"), rule["action"], rule.get("category", ""),
                            _json_dumps(rule.get("includeSegments", [])), _json_dumps(rule.get("excludeSegments", [])),
                            _f(rule.get("minPremiumPct"), 0.0), _f(rule.get("maxPremiumPct"), 0.0),
                            _f(rule.get("minOdd"), 0.0), _f(rule.get("maxOdd"), 0.0), int(rule.get("minSettled", 0) or 0),
                            rule.get("sourceSegment", ""), int(rule.get("trainSettled", 0) or 0), int(rule.get("trainWins", 0) or 0),
                            int(rule.get("trainLosses", 0) or 0), _f(rule.get("trainWinRatePct"), 0.0), _f(rule.get("trainRoiPct"), 0.0),
                            _f(rule.get("trainProfitEur"), 0.0), rule.get("confidence", ""), rule.get("reason", ""),
                        ),
                    )
                    written += 1
            conn.commit()
        return {"status": "ok", "rulesWritten": written, "autoRulesDeprecatedBeforeRefresh": deprecated}

    def list_shadow_rules(self, status: str = "shadow", limit: int = 200) -> List[Dict[str, Any]]:
        self.ensure_schema()
        params: List[Any] = []
        where = ""
        if status and _s(status).lower() not in {"all", "*"}:
            where = " WHERE status = %s"
            params.append(_s(status).lower())
        sql = f"""
            SELECT rule_id, name, status, action, category, include_segments_json, exclude_segments_json,
                   min_premium_pct, max_premium_pct, min_odd, max_odd, min_settled, source_segment,
                   train_settled, train_wins, train_losses, train_win_rate_pct, train_roi_pct,
                   train_profit_eur, confidence, reason, created_at, updated_at
            FROM {self.RULES_TABLE}{where}
            ORDER BY train_profit_eur DESC, train_roi_pct DESC, train_settled DESC
            LIMIT %s
        """
        params.append(int(limit))
        out: List[Dict[str, Any]] = []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    r = dict(zip(cols, row))
                    out.append({
                        "ruleId": r["rule_id"],
                        "name": r["name"],
                        "status": r["status"],
                        "action": r["action"],
                        "category": r["category"],
                        "includeSegments": _json_loads(r["include_segments_json"], []),
                        "excludeSegments": _json_loads(r["exclude_segments_json"], []),
                        "minPremiumPct": r["min_premium_pct"],
                        "maxPremiumPct": r["max_premium_pct"],
                        "minOdd": r["min_odd"],
                        "maxOdd": r["max_odd"],
                        "minSettled": r["min_settled"],
                        "sourceSegment": r["source_segment"],
                        "trainSettled": r["train_settled"],
                        "trainWins": r["train_wins"],
                        "trainLosses": r["train_losses"],
                        "trainWinRatePct": r["train_win_rate_pct"],
                        "trainRoiPct": r["train_roi_pct"],
                        "trainProfitEur": r["train_profit_eur"],
                        "confidence": r["confidence"],
                        "reason": r["reason"],
                    })
        return out

    def persist_shadow_decisions(self, day: str, decisions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        self.ensure_schema()
        written = 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                for d in decisions:
                    match = d.get("match") or {}
                    for rule in d.get("matchedRules", []) or []:
                        decision_id = "v3d_" + _stable_hash(day, d.get("matchKey"), rule.get("ruleId"), length=18)
                        cur.execute(
                            f"""
                            INSERT INTO {self.DECISIONS_TABLE} (
                                decision_id, rule_id, match_key, day, player_a, player_b, category,
                                shadow_decision, reason, result, source_payload_json, updated_at
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                            ON CONFLICT (decision_id) DO UPDATE SET
                                shadow_decision = EXCLUDED.shadow_decision,
                                reason = EXCLUDED.reason,
                                result = EXCLUDED.result,
                                source_payload_json = EXCLUDED.source_payload_json,
                                updated_at = NOW()
                            """,
                            (
                                decision_id, rule.get("ruleId"), d.get("matchKey"), day,
                                _s(match.get("playerA") or match.get("sourcePlayerA") or match.get("predictedWinner")),
                                _s(match.get("playerB") or match.get("sourcePlayerB") or match.get("opponent")),
                                row_category(match), d.get("shadowDecision"), d.get("reason", ""), "pending", _json_dumps(d),
                            ),
                        )
                        written += 1
            conn.commit()
        return {"status": "ok", "shadowDecisionsWritten": written}


def confidence_from_stats(stats: Dict[str, Any]) -> str:
    settled = int(stats.get("settled", 0) or 0)
    roi = _f(stats.get("roiPct"), 0.0)
    if settled >= 100 and abs(roi) >= 8:
        return "HIGH"
    if settled >= 40 and abs(roi) >= 5:
        return "MEDIUM"
    return "LOW"


def category_from_segment(segment: str) -> str:
    if "PREMIUM" in segment:
        return "PREMIUM"
    if "REFUSE" in segment:
        return "REFUSE"
    if "PROCHE" in segment:
        return "PROCHE"
    if "VETO" in segment:
        return "VETO"
    return ""


def is_rule_candidate_segment(segment: str) -> bool:
    # Avoid rules that are too broad or purely descriptive. Keep actionable segments.
    # V3.1 generated DRAW_MAIN_OR_UNKNOWN rules; they were too generic and created
    # confusing explanations.  V3.1.1 keeps qualification as a useful segment but
    # no longer creates standalone rules for MAIN_OR_UNKNOWN.
    if segment in {
        "ALL", "ODDS_AVAILABLE", "ODDS_MISSING", "DRAW_MAIN_OR_UNKNOWN",
        "TIEBREAK_ANY", "FIRST_SET_TIEBREAK", "BEST_OF_5_LIKE",
    }:
        return False
    allowed_prefixes = (
        "PREMIUM_", "REFUSE_", "PROCHE_", "VETO_",
        "CATEGORY_PREMIUM", "CATEGORY_REFUSE",
        "DRAW_QUALIFICATION",
    )
    return segment.startswith(allowed_prefixes)


def make_shadow_rules_from_report(report: Dict[str, Any], *, min_settled: int = 10, max_rules: int = 50) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    for stats in report.get("segments", []) or []:
        segment = _s(stats.get("segment"))
        if not segment or not is_rule_candidate_segment(segment):
            continue
        settled = int(stats.get("settled", 0) or 0)
        roi = _f(stats.get("roiPct"), 0.0)
        profit = _f(stats.get("profitEur"), 0.0)
        if settled < min_settled:
            continue

        if roi >= 10.0 and profit > 0:
            action = "PROMOTE_SHADOW"
            name = "Tester positif — " + segment
            reason = "Segment historiquement positif; V3 le teste en shadow sur les prochains matchs."
        elif roi < 0.0:
            action = "DOWNGRADE_SHADOW"
            name = "Downgrade test — " + segment
            reason = "Segment historiquement négatif; V3 teste le downgrade sans bloquer officiellement la V2."
        else:
            continue

        include = [segment]
        # For category-specific odds rules, keep the rule in the same category.
        cat = category_from_segment(segment)
        rule_id = "v3_auto_" + _stable_hash(segment, action, min_settled, length=14)
        rules.append({
            "ruleId": rule_id,
            "name": name,
            "status": "shadow",
            "action": action,
            "category": cat,
            "includeSegments": include,
            "excludeSegments": [],
            "minPremiumPct": 0.0,
            "maxPremiumPct": 0.0,
            "minOdd": 0.0,
            "maxOdd": 0.0,
            "minSettled": min_settled,
            "sourceSegment": segment,
            "trainSettled": settled,
            "trainWins": int(stats.get("wins", 0) or 0),
            "trainLosses": int(stats.get("losses", 0) or 0),
            "trainWinRatePct": _f(stats.get("winRatePct"), 0.0),
            "trainRoiPct": roi,
            "trainProfitEur": profit,
            "confidence": confidence_from_stats(stats),
            "reason": reason,
        })

    rules.sort(key=lambda r: (r["trainProfitEur"], abs(r["trainRoiPct"]), r["trainSettled"]), reverse=True)
    return rules[:max_rules]


def build_v3_rules_from_history(
    rows: List[Dict[str, Any]],
    *,
    category: str = "ALL",
    min_settled: int = 10,
    odds_cutoff: float = DEFAULT_ODDS_CUTOFF,
    max_rules: int = 50,
) -> Dict[str, Any]:
    report = build_v3_learning_report(rows, category=category, min_settled=min_settled, odds_cutoff=odds_cutoff, dedupe=True, include_rows=False)
    rules = make_shadow_rules_from_report(report, min_settled=min_settled, max_rules=max_rules)
    return {"status": "ok", "version": V3_VERSION, "rules": rules, "rulesCount": len(rules), "learningReport": report}


def rule_matches_row(rule: Dict[str, Any], row: Dict[str, Any], odds_cutoff: float = DEFAULT_ODDS_CUTOFF) -> bool:
    if _s(rule.get("status")).lower() == "deprecated":
        return False
    source_segment = _s(rule.get("sourceSegment")).upper()
    if source_segment in {"DRAW_MAIN_OR_UNKNOWN", "TIEBREAK_ANY", "FIRST_SET_TIEBREAK", "BEST_OF_5_LIKE"}:
        return False
    cat = _s(rule.get("category")).upper()
    if cat and row_category(row) != cat:
        return False
    pct = _f(row.get("premiumPct"), 0.0)
    odd = row_odd(row)
    min_pct = _f(rule.get("minPremiumPct"), 0.0)
    max_pct = _f(rule.get("maxPremiumPct"), 0.0)
    min_odd = _f(rule.get("minOdd"), 0.0)
    max_odd = _f(rule.get("maxOdd"), 0.0)
    if min_pct > 0 and pct < min_pct:
        return False
    if max_pct > 0 and pct > max_pct:
        return False
    if min_odd > 0 and odd < min_odd:
        return False
    if max_odd > 0 and odd > max_odd:
        return False
    tags = set(row_segments(row, odds_cutoff=odds_cutoff))
    include = set(rule.get("includeSegments") or [])
    exclude = set(rule.get("excludeSegments") or [])
    if include and not include.issubset(tags):
        return False
    if exclude and exclude.intersection(tags):
        return False
    return True


def match_key_for_daily(row: Dict[str, Any], day: str = "") -> str:
    event_id = _s(row.get("sportEventId") or row.get("sport_event_id") or row.get("sportradarSportEventId") or row.get("id"))
    if event_id:
        return "event:" + event_id
    a = _norm(row.get("playerA") or row.get("sourcePlayerA") or row.get("predictedWinner"))
    b = _norm(row.get("playerB") or row.get("sourcePlayerB") or row.get("opponent"))
    names = "|".join(sorted([a, b]))
    return "fallback:" + _s(day or row.get("date")) + ":" + names + ":" + _norm(row.get("tournament") or row.get("seasonName"))


def _confidence_weight(rule: Dict[str, Any]) -> int:
    conf = _s(rule.get("confidence")).upper()
    if conf == "HIGH":
        return 3
    if conf == "MEDIUM":
        return 2
    if conf == "LOW":
        return 1
    return 0


def _segment_specificity(segment: str) -> int:
    seg = _s(segment).upper()
    if not seg:
        return 0
    score = 0
    # Category-specific rules are more actionable than global rules.
    if seg.startswith(("PREMIUM_", "REFUSE_", "PROCHE_", "VETO_")):
        score += 30
    if "ODDS" in seg:
        score += 25
    if "QUALIFICATION" in seg:
        score += 20
    if "MAIN_OR_UNKNOWN" in seg:
        score += 10
    if "PCT_" in seg:
        score += 18
    if "VALUE" in seg or "COTE_180" in seg:
        score += 18
    if "SURFACE" in seg:
        score += 12
    if "TIEBREAK" in seg:
        score += 8
    if seg.startswith("CATEGORY_"):
        score -= 12
    if seg in {"DRAW_MAIN_OR_UNKNOWN", "TIEBREAK_ANY"}:
        score -= 25
    score += min(seg.count("_"), 8)
    return score


def rule_specificity(rule: Dict[str, Any]) -> int:
    include = rule.get("includeSegments") or []
    if not include:
        return 0
    return max(_segment_specificity(_s(seg)) for seg in include)


def sort_shadow_rules_for_decision(rules: Sequence[Dict[str, Any]], *, prefer_action: str = "") -> List[Dict[str, Any]]:
    prefer = _s(prefer_action).upper()

    def key(rule: Dict[str, Any]) -> Tuple[int, int, int, float, int]:
        action = _s(rule.get("action")).upper()
        action_boost = 1 if prefer and action == prefer else 0
        return (
            action_boost,
            rule_specificity(rule),
            _confidence_weight(rule),
            abs(_f(rule.get("trainRoiPct"), 0.0)),
            int(rule.get("trainSettled", 0) or 0),
        )

    return sorted(list(rules or []), key=key, reverse=True)


def split_shadow_rules(rules: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    positives: List[Dict[str, Any]] = []
    negatives: List[Dict[str, Any]] = []
    neutral: List[Dict[str, Any]] = []
    for r in rules or []:
        action = _s(r.get("action")).upper()
        if action == "DOWNGRADE_SHADOW":
            negatives.append(r)
        elif action == "PROMOTE_SHADOW":
            positives.append(r)
        else:
            neutral.append(r)
    return (
        sort_shadow_rules_for_decision(positives, prefer_action="PROMOTE_SHADOW"),
        sort_shadow_rules_for_decision(negatives, prefer_action="DOWNGRADE_SHADOW"),
        sort_shadow_rules_for_decision(neutral),
    )


def decide_shadow_from_rules(matched: Sequence[Dict[str, Any]]) -> Tuple[str, str, Dict[str, Any]]:
    positives, negatives, neutral = split_shadow_rules(matched)
    if negatives:
        top_neg = negatives[0]
        top_pos = positives[0] if positives else None
        # Negative rules are protective.  If a specific negative segment exists,
        # it wins over broad positive segments.  This fixes the V3.1 problem where
        # the decision was DOWNGRADE but the reason displayed DRAW_MAIN_OR_UNKNOWN.
        if top_pos and rule_specificity(top_pos) > rule_specificity(top_neg) + 20:
            return "V3_SHADOW_WATCH_CONFLICT", shadow_reason("V3_SHADOW_WATCH_CONFLICT", [top_neg, top_pos]), top_neg
        return "V3_SHADOW_DOWNGRADE", shadow_reason("V3_SHADOW_DOWNGRADE", [top_neg]), top_neg
    if positives:
        top_pos = positives[0]
        return "V3_SHADOW_PROMOTE", shadow_reason("V3_SHADOW_PROMOTE", [top_pos]), top_pos
    if neutral:
        top = neutral[0]
        return "V3_SHADOW_WATCH", shadow_reason("V3_SHADOW_WATCH", [top]), top
    return "V3_NO_SIGNAL", shadow_reason("V3_NO_SIGNAL", []), {}


def evaluate_shadow_matches(
    matches: Sequence[Dict[str, Any]],
    rules: Sequence[Dict[str, Any]],
    *,
    day: str = "",
    odds_cutoff: float = DEFAULT_ODDS_CUTOFF,
) -> Dict[str, Any]:
    decisions: List[Dict[str, Any]] = []
    counts = {"promote": 0, "downgrade": 0, "watch": 0, "noSignal": 0, "conflict": 0}
    for m in matches or []:
        matched_raw = [r for r in rules if rule_matches_row(r, m, odds_cutoff=odds_cutoff)]
        positives, negatives, neutral = split_shadow_rules(matched_raw)
        matched = negatives + positives + neutral
        decision, reason, primary_rule = decide_shadow_from_rules(matched)
        if decision == "V3_SHADOW_DOWNGRADE":
            counts["downgrade"] += 1
        elif decision == "V3_SHADOW_PROMOTE":
            counts["promote"] += 1
        elif decision == "V3_SHADOW_WATCH_CONFLICT":
            counts["watch"] += 1
            counts["conflict"] += 1
        elif decision == "V3_SHADOW_WATCH":
            counts["watch"] += 1
        else:
            counts["noSignal"] += 1
        decisions.append({
            "matchKey": match_key_for_daily(m, day=day),
            "shadowDecision": decision,
            "reason": reason,
            "primaryRule": primary_rule,
            "match": m,
            "category": row_category(m),
            "drawType": draw_type(m),
            "isQualification": is_qualification(m),
            "qualificationSignals": qualification_signals(m),
            "segments": row_segments(m, odds_cutoff=odds_cutoff),
            "matchedRules": matched,
            "positiveRules": positives,
            "negativeRules": negatives,
        })
    return {"status": "ok", "version": V3_VERSION, "day": day, "counts": counts, "decisions": decisions}


def shadow_reason(decision: str, rules: Sequence[Dict[str, Any]]) -> str:
    if not rules:
        return "Aucune règle V3 shadow ne correspond à ce match."
    top = rules[0]
    segment = _s(top.get("sourceSegment"))
    roi = _f(top.get("trainRoiPct"), 0.0)
    settled = int(top.get("trainSettled", 0) or 0)
    if decision == "V3_SHADOW_DOWNGRADE":
        return f"Downgrade V3 : segment négatif prioritaire {segment} | ROI entraînement {roi:.2f}% sur {settled} matchs réglés."
    if decision == "V3_SHADOW_PROMOTE":
        return f"Promote V3 : segment positif prioritaire {segment} | ROI entraînement {roi:.2f}% sur {settled} matchs réglés."
    if decision == "V3_SHADOW_WATCH_CONFLICT":
        second = rules[1] if len(rules) > 1 else {}
        return "Conflit V3 : négatif " + segment + " contre positif " + _s(second.get("sourceSegment")) + ". À surveiller, pas de validation automatique."
    return "Règle V3 en observation : " + segment
