#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP58 — Live feature parity builder for STEP56 R&D model.

Purpose:
- Produce the exact STEP56 model feature vector for a playerA/playerB tennis match.
- Use NO odds and NO player names/ids as model features.
- Player identity is used only to look up rolling historical state, exactly like Elo/form state.
- Support strict parity against the STEP56 R&D feature matrix.

Deployment status: R&D / parity only, not production motor replacement.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

INVALID_TOKENS = ("RET", "W/O", "WO", "WALKOVER", "DEF", "ABD", "ABN", "CANCEL", "DEFAULT")
ROUND_ORDER = {"RR": 0, "BR": 1, "R128": 2, "R64": 3, "R32": 4, "R16": 5, "QF": 6, "SF": 7, "F": 8}
SURFACES = ["Hard", "Clay", "Grass", "Carpet"]
LEVELS = ["G", "M", "A", "D", "F", "C"]


def stable_orientation(*parts: Any) -> bool:
    """Stable replacement for Python hash(). Returns True when A should be winner side."""
    raw = "|".join(str(x) for x in parts).encode("utf-8", errors="ignore")
    digest = hashlib.sha256(raw).hexdigest()
    return int(digest[:16], 16) % 2 == 0


def canonical_name(name: str) -> str:
    import unicodedata
    value = str(name or "").strip().lower()
    if "," in value:
        left, right = value.split(",", 1)
        if left.strip() and right.strip():
            value = right.strip() + " " + left.strip()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9\s\-']", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        if pd.isna(x):
            return default
        return int(float(x))
    except Exception:
        return default


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-min(x, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(x, -60.0))
    return z / (1.0 + z)


def logit(p: float) -> float:
    p = max(0.001, min(0.999, float(p)))
    return math.log(p / (1.0 - p))


def shrink_prob(p: float, strength: float) -> float:
    return 0.5 + (float(p) - 0.5) * (1.0 - strength)


def atp_points_prob(a: float, b: float, scale: float = 1850.0) -> float:
    return 1.0 / (1.0 + 10.0 ** (-(float(a) - float(b)) / scale))


def rank_prob(ra: float, rb: float, scale: float = 34.0) -> float:
    if ra <= 0 or rb <= 0:
        return 0.5
    return 1.0 / (1.0 + math.exp(-((float(rb) - float(ra)) / scale)))


def form_prob(a: float, b: float, scale: float = 0.16) -> float:
    return 1.0 / (1.0 + math.exp(-((float(a) - float(b)) / scale)))


def elo_prob(diff: float) -> float:
    return 1.0 / (1.0 + 10.0 ** (-float(diff) / 400.0))


def old_premium_from_signals(p_swe: float, p_atp: float, p_rank: float, p_f5: float, p_f10: float, p_sf5: float, p_dom: float) -> float:
    score = (
        0.28 * logit(p_swe)
        + 0.22 * logit(p_atp)
        + 0.14 * logit(p_rank)
        + 0.14 * logit(p_f5)
        + 0.09 * logit(p_f10)
        + 0.07 * logit(p_sf5)
        + 0.06 * logit(p_dom)
        + 0.18
    )
    return sigmoid(score / 0.96)


def parse_score_sets(score: Any) -> List[Tuple[int, int]]:
    sets: List[Tuple[int, int]] = []
    for tok in re.split(r"\s+", str(score or "").strip()):
        tok = re.sub(r"\([^)]*\)", "", tok.strip())
        m = re.match(r"^(\d{1,2})-(\d{1,2})$", tok)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if max(a, b) > 20:
            continue
        sets.append((a, b))
    return sets


def score_meta(score: Any) -> Tuple[int, int, int, int]:
    sets = parse_score_sets(score)
    if not sets:
        return (0, 0, 0, 0)
    games = sum(a + b for a, b in sets)
    tbs = sum(1 for a, b in sets if max(a, b) >= 7 and abs(a - b) <= 2)
    return (len(sets), games, tbs, 1 if len(sets) >= 5 else 0)


def valid_score(score: Any) -> bool:
    s = str(score or "").upper()
    return bool(s.strip()) and not any(tok in s for tok in INVALID_TOKENS)


def canonical_surface(x: Any) -> str:
    s = str(x or "").strip().title()
    if "Hard" in s:
        return "Hard"
    if "Clay" in s:
        return "Clay"
    if "Grass" in s:
        return "Grass"
    if "Carpet" in s:
        return "Carpet"
    return s if s in SURFACES else "Hard"


def round_idx(r: Any) -> int:
    return ROUND_ORDER.get(str(r or "").strip(), 9)


def is_left(hand: Any) -> int:
    return 1 if str(hand or "").upper().startswith("L") else 0


def is_right(hand: Any) -> int:
    return 1 if str(hand or "").upper().startswith("R") else 0


def mean_deque(dq: Iterable[float], default: float = 0.5) -> float:
    vals = list(dq)
    return float(np.mean(vals)) if vals else default


@dataclass
class PlayerState:
    g_elo: float = 1500.0
    g_count: int = 0
    s_elo: Dict[str, float] = field(default_factory=lambda: defaultdict(lambda: 1500.0))
    s_count: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    res5: deque = field(default_factory=lambda: deque(maxlen=5))
    res10: deque = field(default_factory=lambda: deque(maxlen=10))
    surf_res5: Dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=5)))
    dom5: deque = field(default_factory=lambda: deque(maxlen=5))
    metrics10: Dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=10)))
    surf_metrics10: Dict[str, Dict[str, deque]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(lambda: deque(maxlen=10))))
    last_minutes: float = 0.0
    last_sets: float = 0.0
    last_games: float = 0.0
    last_tbs: float = 0.0
    last_five: float = 0.0
    current_tourney: str = ""
    current_tourney_matches: int = 0
    current_tourney_minutes: float = 0.0
    current_tourney_games: float = 0.0
    current_tourney_sets: float = 0.0
    current_tourney_tbs: float = 0.0
    recent_retired: deque = field(default_factory=lambda: deque(maxlen=8))


def player_stats_from_row(row: pd.Series, prefix: str, oppprefix: str) -> Dict[str, float]:
    svpt = safe_float(row.get(f"{prefix}_svpt"), 0)
    ace = safe_float(row.get(f"{prefix}_ace"), 0)
    df = safe_float(row.get(f"{prefix}_df"), 0)
    firstin = safe_float(row.get(f"{prefix}_1stIn"), 0)
    firstwon = safe_float(row.get(f"{prefix}_1stWon"), 0)
    secondwon = safe_float(row.get(f"{prefix}_2ndWon"), 0)
    svgms = safe_float(row.get(f"{prefix}_SvGms"), 0)
    bpsaved = safe_float(row.get(f"{prefix}_bpSaved"), 0)
    bpfaced = safe_float(row.get(f"{prefix}_bpFaced"), 0)
    opp_svpt = safe_float(row.get(f"{oppprefix}_svpt"), 0)
    opp_firstwon = safe_float(row.get(f"{oppprefix}_1stWon"), 0)
    opp_secondwon = safe_float(row.get(f"{oppprefix}_2ndWon"), 0)
    opp_svgms = safe_float(row.get(f"{oppprefix}_SvGms"), 0)
    opp_bpsaved = safe_float(row.get(f"{oppprefix}_bpSaved"), 0)
    opp_bpfaced = safe_float(row.get(f"{oppprefix}_bpFaced"), 0)
    service_pts_won = firstwon + secondwon
    opp_service_pts_won = opp_firstwon + opp_secondwon
    breaks_conceded = max(0.0, bpfaced - bpsaved)
    breaks_won = max(0.0, opp_bpfaced - opp_bpsaved)
    metrics: Dict[str, float] = {}
    metrics["spw"] = service_pts_won / svpt if svpt > 0 else 0.5
    metrics["rpw"] = (opp_svpt - opp_service_pts_won) / opp_svpt if opp_svpt > 0 else 0.5
    metrics["ace_rate"] = ace / svpt if svpt > 0 else 0.0
    metrics["df_rate"] = df / svpt if svpt > 0 else 0.0
    metrics["first_in"] = firstin / svpt if svpt > 0 else 0.6
    metrics["first_won"] = firstwon / firstin if firstin > 0 else 0.65
    metrics["second_won"] = secondwon / max(1.0, (svpt - firstin)) if svpt - firstin > 0 else 0.50
    metrics["bp_save"] = bpsaved / bpfaced if bpfaced > 0 else 0.65
    metrics["bp_convert"] = breaks_won / opp_bpfaced if opp_bpfaced > 0 else 0.35
    metrics["hold_rate"] = max(0.0, min(1.0, (svgms - breaks_conceded) / svgms)) if svgms > 0 else 0.75
    metrics["break_rate"] = max(0.0, min(1.0, breaks_won / opp_svgms)) if opp_svgms > 0 else 0.25
    metrics["stats_available"] = 1.0 if (svpt > 0 and opp_svpt > 0) else 0.0
    metrics["service_games"] = svgms
    metrics["return_games"] = opp_svgms
    return metrics


def dominance_from_metrics(m1: Dict[str, float], m2: Dict[str, float]) -> float:
    return 0.34 * (m1["spw"] - m2["spw"]) + 0.34 * (m1["rpw"] - m2["rpw"]) + 0.16 * (m1["hold_rate"] - m2["hold_rate"]) + 0.16 * (m1["break_rate"] - m2["break_rate"])


def get_surface_weighted_elo(st: PlayerState, surface: str, shrink: float = 26.0) -> float:
    n = st.s_count[surface]
    w = n / (n + shrink)
    return w * st.s_elo[surface] + (1.0 - w) * st.g_elo


def get_metric(st: PlayerState, metric: str, surface: Optional[str] = None, default: float = 0.5) -> float:
    if surface is None:
        return mean_deque(st.metrics10[metric], default)
    if len(st.surf_metrics10[surface][metric]) > 0:
        return mean_deque(st.surf_metrics10[surface][metric], default)
    return get_metric(st, metric, None, default)


def reset_tourney_if_needed(st: PlayerState, tourney_id: str) -> None:
    if st.current_tourney != tourney_id:
        st.current_tourney = tourney_id
        st.current_tourney_matches = 0
        st.current_tourney_minutes = 0.0
        st.current_tourney_games = 0.0
        st.current_tourney_sets = 0.0
        st.current_tourney_tbs = 0.0


def row_key(row: pd.Series, prefix: str, key_mode: str) -> str:
    if key_mode == "id":
        return str(row.get(f"{prefix}_id"))
    return canonical_name(str(row.get(f"{prefix}_name") or ""))


def build_features_from_context(
    *,
    states: Dict[str, PlayerState],
    A_key: str,
    B_key: str,
    surface: str,
    tourney_id: str,
    level: str,
    indoor: Any,
    best_of: int,
    draw_size: float,
    rnd: str,
    A_points: float,
    B_points: float,
    A_rank: float,
    B_rank: float,
    A_height: float = 0.0,
    B_height: float = 0.0,
    A_age: float = 0.0,
    B_age: float = 0.0,
    A_hand: Any = "",
    B_hand: Any = "",
) -> Dict[str, float]:
    surface = canonical_surface(surface)
    A = states[A_key]
    B = states[B_key]
    reset_tourney_if_needed(A, str(tourney_id))
    reset_tourney_if_needed(B, str(tourney_id))

    A_left = is_left(A_hand)
    B_left = is_left(B_hand)
    A_right = is_right(A_hand)
    B_right = is_right(B_hand)

    sweA = get_surface_weighted_elo(A, surface)
    sweB = get_surface_weighted_elo(B, surface)
    p_swe = shrink_prob(elo_prob(sweA - sweB), 0.08)
    p_atp = shrink_prob(atp_points_prob(A_points, B_points, 1850.0), 0.10) if (A_points > 0 and B_points > 0) else 0.5
    p_rank = shrink_prob(rank_prob(A_rank, B_rank, 34.0), 0.12) if (A_rank > 0 and B_rank > 0) else 0.5

    form5A = mean_deque(A.res5, 0.5)
    form5B = mean_deque(B.res5, 0.5)
    form10A = mean_deque(A.res10, 0.5)
    form10B = mean_deque(B.res10, 0.5)
    sformA = mean_deque(A.surf_res5[surface], form5A)
    sformB = mean_deque(B.surf_res5[surface], form5B)
    domA = mean_deque(A.dom5, 0.0)
    domB = mean_deque(B.dom5, 0.0)

    p_f5 = shrink_prob(form_prob(form5A, form5B, 0.17), 0.20)
    p_f10 = shrink_prob(form_prob(form10A, form10B, 0.16), 0.22)
    p_sf5 = shrink_prob(form_prob(sformA, sformB, 0.18), 0.18)
    p_dom = shrink_prob(sigmoid((domA - domB) / 0.075), 0.25)
    old_p = old_premium_from_signals(p_swe, p_atp, p_rank, p_f5, p_f10, p_sf5, p_dom)

    feats: Dict[str, float] = {}

    def add(k: str, v: Any) -> None:
        try:
            vf = float(v)
        except Exception:
            vf = 0.0
        if math.isnan(vf) or math.isinf(vf):
            vf = 0.0
        feats[k] = vf

    for k, p in [
        ("p_swe", p_swe),
        ("p_atp", p_atp),
        ("p_rank", p_rank),
        ("p_form5", p_f5),
        ("p_form10", p_f10),
        ("p_surface_form5", p_sf5),
        ("p_dominance", p_dom),
        ("old_premium", old_p),
    ]:
        add(k, p)
        add(k + "_logit", logit(p))

    add("swe_diff", sweA - sweB)
    add("global_elo_diff", A.g_elo - B.g_elo)
    add("surface_elo_diff", A.s_elo[surface] - B.s_elo[surface])
    add("log_points_diff", math.log1p(A_points) - math.log1p(B_points))
    add("rank_diff_inv", (B_rank - A_rank) if A_rank > 0 and B_rank > 0 else 0)
    add("form5_diff", form5A - form5B)
    add("form10_diff", form10A - form10B)
    add("surface_form5_diff", sformA - sformB)
    add("dom_diff", domA - domB)
    add("global_count_diff_log", math.log1p(A.g_count) - math.log1p(B.g_count))
    add("surface_count_diff_log", math.log1p(A.s_count[surface]) - math.log1p(B.s_count[surface]))
    add("min_global_count_log", min(math.log1p(A.g_count), math.log1p(B.g_count)))
    add("min_surface_count_log", min(math.log1p(A.s_count[surface]), math.log1p(B.s_count[surface])))

    metric_defaults = [
        ("spw", 0.62),
        ("rpw", 0.38),
        ("ace_rate", 0.04),
        ("df_rate", 0.03),
        ("first_in", 0.62),
        ("first_won", 0.70),
        ("second_won", 0.50),
        ("bp_save", 0.62),
        ("bp_convert", 0.38),
        ("hold_rate", 0.78),
        ("break_rate", 0.22),
        ("stats_available", 0.0),
    ]
    for metric, default in metric_defaults:
        a = get_metric(A, metric, None, default)
        b = get_metric(B, metric, None, default)
        sa = get_metric(A, metric, surface, default)
        sb = get_metric(B, metric, surface, default)
        add(f"{metric}_diff10", a - b)
        add(f"surf_{metric}_diff10", sa - sb)

    add("serve_vs_return_edge", get_metric(A, "spw", None, 0.62) - get_metric(B, "rpw", None, 0.38))
    add("return_vs_serve_edge", get_metric(A, "rpw", None, 0.38) - get_metric(B, "spw", None, 0.62))
    add("hold_break_edge", get_metric(A, "hold_rate", None, 0.78) + get_metric(A, "break_rate", None, 0.22) - get_metric(B, "hold_rate", None, 0.78) - get_metric(B, "break_rate", None, 0.22))
    add("weak_second_serve_edge", get_metric(A, "second_won", None, 0.50) - get_metric(B, "second_won", None, 0.50))
    add("big_server_clay_risk_A", (get_metric(A, "ace_rate", None, 0.04) - get_metric(A, "rpw", None, 0.38)) * (1 if surface == "Clay" else 0))
    add("big_server_clay_risk_B", (get_metric(B, "ace_rate", None, 0.04) - get_metric(B, "rpw", None, 0.38)) * (1 if surface == "Clay" else 0))
    add("big_server_clay_risk_diff", feats["big_server_clay_risk_A"] - feats["big_server_clay_risk_B"])

    for attr in ["last_minutes", "last_sets", "last_games", "last_tbs", "last_five", "current_tourney_matches", "current_tourney_minutes", "current_tourney_games", "current_tourney_sets", "current_tourney_tbs"]:
        add(attr + "_diff", getattr(A, attr) - getattr(B, attr))
        add(attr + "_A", getattr(A, attr))
        add(attr + "_B", getattr(B, attr))

    add("played_prev_round_A", 1 if A.current_tourney_matches > 0 else 0)
    add("played_prev_round_B", 1 if B.current_tourney_matches > 0 else 0)
    add("recent_retired_diff", mean_deque(A.recent_retired, 0.0) - mean_deque(B.recent_retired, 0.0))

    add("height_diff", A_height - B_height if A_height > 0 and B_height > 0 else 0)
    add("age_diff", A_age - B_age if A_age > 0 and B_age > 0 else 0)
    add("lefty_A", A_left)
    add("lefty_B", B_left)
    add("lefty_edge", A_left - B_left)
    add("same_hand", 1 if (A_left == B_left and (A_left or A_right or B_left or B_right)) else 0)
    add("best_of5", 1 if int(best_of) == 5 else 0)
    add("draw_log", math.log1p(draw_size))
    add("round_idx", round_idx(rnd))
    add("indoor", 1 if str(indoor or "") == "I" else 0)
    for s in SURFACES:
        add("surface_" + s, 1 if surface == s else 0)
    for lev in LEVELS:
        add("level_" + lev, 1 if str(level or "") == lev else 0)

    add("old_high_but_weak_dom", old_p * max(0.0, 0.5 - p_dom))
    add("old_high_but_weak_return", old_p * max(0.0, 0.0 - feats["rpw_diff10"]))
    add("old_high_fatigue_penalty", old_p * max(0.0, feats["last_minutes_A"] - feats["last_minutes_B"]) / 300.0)
    return feats


def build_features_from_historical_row(row: pd.Series, A_is_winner: bool, states: Dict[str, PlayerState], key_mode: str = "id") -> Tuple[Dict[str, float], int, Dict[str, Any]]:
    if A_is_winner:
        A_pref, B_pref = "winner", "loser"
        y = 1
    else:
        A_pref, B_pref = "loser", "winner"
        y = 0
    A_key = row_key(row, A_pref, key_mode)
    B_key = row_key(row, B_pref, key_mode)
    feats = build_features_from_context(
        states=states,
        A_key=A_key,
        B_key=B_key,
        surface=canonical_surface(row.get("surface")),
        tourney_id=str(row.get("tourney_id")),
        level=str(row.get("tourney_level") or ""),
        indoor=row.get("indoor"),
        best_of=safe_int(row.get("best_of"), 3),
        draw_size=safe_float(row.get("draw_size"), 0),
        rnd=str(row.get("round") or ""),
        A_points=safe_float(row.get(f"{A_pref}_rank_points"), 0),
        B_points=safe_float(row.get(f"{B_pref}_rank_points"), 0),
        A_rank=safe_float(row.get(f"{A_pref}_rank"), 0),
        B_rank=safe_float(row.get(f"{B_pref}_rank"), 0),
        A_height=safe_float(row.get(f"{A_pref}_ht"), 0),
        B_height=safe_float(row.get(f"{B_pref}_ht"), 0),
        A_age=safe_float(row.get(f"{A_pref}_age"), 0),
        B_age=safe_float(row.get(f"{B_pref}_age"), 0),
        A_hand=row.get(f"{A_pref}_hand"),
        B_hand=row.get(f"{B_pref}_hand"),
    )
    meta = {
        "year": safe_int(str(row.get("tourney_id"))[:4], 0),
        "tourney_id": str(row.get("tourney_id")),
        "tourney_name": str(row.get("tourney_name")),
        "tourney_date": safe_int(row.get("tourney_date"), 0),
        "round": str(row.get("round") or ""),
        "match_num": safe_float(row.get("match_num"), 0),
        "surface": canonical_surface(row.get("surface")),
        "A_name": str(row.get(f"{A_pref}_name")),
        "B_name": str(row.get(f"{B_pref}_name")),
        "winner_name": str(row.get("winner_name")),
        "loser_name": str(row.get("loser_name")),
        "A_is_winner": bool(A_is_winner),
        "old_p_A": feats.get("old_premium", 0.5),
        "old_confidence": max(feats.get("old_premium", 0.5), 1.0 - feats.get("old_premium", 0.5)),
        "old_pred_A": feats.get("old_premium", 0.5) >= 0.5,
    }
    return feats, y, meta


def update_player_states_for_match(row: pd.Series, states: Dict[str, PlayerState], key_mode: str = "id") -> None:
    winner_key = row_key(row, "winner", key_mode)
    loser_key = row_key(row, "loser", key_mode)
    surface = canonical_surface(row.get("surface"))
    level = str(row.get("tourney_level") or "")
    tourney_id = str(row.get("tourney_id"))
    W = states[winner_key]
    L = states[loser_key]
    reset_tourney_if_needed(W, tourney_id)
    reset_tourney_if_needed(L, tourney_id)
    mult = 1.10 if level == "G" else 1.0

    exp = elo_prob(W.g_elo - L.g_elo)
    kW = 250.0 / ((W.g_count + 5) ** 0.4)
    kL = 250.0 / ((L.g_count + 5) ** 0.4)
    W.g_elo += mult * kW * (1.0 - exp)
    L.g_elo += mult * kL * (0.0 - (1.0 - exp))
    W.g_count += 1
    L.g_count += 1

    exp_s = elo_prob(W.s_elo[surface] - L.s_elo[surface])
    kWs = 250.0 / ((W.s_count[surface] + 5) ** 0.4)
    kLs = 250.0 / ((L.s_count[surface] + 5) ** 0.4)
    W.s_elo[surface] += mult * kWs * (1.0 - exp_s)
    L.s_elo[surface] += mult * kLs * (0.0 - (1.0 - exp_s))
    W.s_count[surface] += 1
    L.s_count[surface] += 1

    W.res5.append(1.0)
    L.res5.append(0.0)
    W.res10.append(1.0)
    L.res10.append(0.0)
    W.surf_res5[surface].append(1.0)
    L.surf_res5[surface].append(0.0)

    mW = player_stats_from_row(row, "w", "l")
    mL = player_stats_from_row(row, "l", "w")
    domW = dominance_from_metrics(mW, mL)
    if mW.get("stats_available", 0) <= 0 or mL.get("stats_available", 0) <= 0:
        sets, games, tbs, five = score_meta(row.get("score"))
        if games > 0:
            parsed = parse_score_sets(row.get("score"))
            gw = sum(a for a, b in parsed)
            gl = sum(b for a, b in parsed)
            if gw + gl > 0:
                domW = 0.18 * ((gw - gl) / (gw + gl)) + 0.04 * 1.0
    W.dom5.append(domW)
    L.dom5.append(-domW)

    for metric, v in mW.items():
        W.metrics10[metric].append(v)
        W.surf_metrics10[surface][metric].append(v)
    for metric, v in mL.items():
        L.metrics10[metric].append(v)
        L.surf_metrics10[surface][metric].append(v)

    minutes = safe_float(row.get("minutes"), 0)
    sets, games, tbs, five = score_meta(row.get("score"))
    retired = 0.0 if valid_score(row.get("score")) else 1.0
    for st in (W, L):
        st.last_minutes = minutes
        st.last_sets = sets
        st.last_games = games
        st.last_tbs = tbs
        st.last_five = five
        st.current_tourney_matches += 1
        st.current_tourney_minutes += minutes
        st.current_tourney_games += games
        st.current_tourney_sets += sets
        st.current_tourney_tbs += tbs
        st.recent_retired.append(retired)


def load_history_dataframe(data_dir: Path, years: Iterable[int] = (2022, 2023, 2024, 2025, 2026)) -> pd.DataFrame:
    dfs = []
    for year in years:
        p = data_dir / f"{year}.csv"
        if not p.exists():
            p = data_dir / f"atp_matches_{year}.csv"
        if not p.exists():
            continue
        df = pd.read_csv(p)
        df["src_year"] = year
        dfs.append(df)
    if not dfs:
        raise FileNotFoundError(f"No history CSV found in {data_dir}")
    df = pd.concat(dfs, ignore_index=True)
    df = df[df["winner_name"].notna() & df["loser_name"].notna() & df["surface"].notna()]
    df = df[df["score"].apply(valid_score)]
    df["round_order"] = df["round"].apply(round_idx)
    df["tourney_date_int"] = df["tourney_date"].apply(safe_int)
    df["match_num_float"] = df["match_num"].apply(safe_float)
    return df.sort_values(["tourney_date_int", "tourney_id", "round_order", "match_num_float"]).reset_index(drop=True)


def build_dataset_from_history(
    data_dir: Path,
    feature_order: List[str],
    years: Iterable[int] = (2022, 2023, 2024, 2025, 2026),
    key_mode: str = "id",
    orientation_meta: Optional[pd.DataFrame] = None,
) -> Tuple[pd.DataFrame, np.ndarray, pd.DataFrame]:
    df = load_history_dataframe(data_dir, years)
    states: Dict[str, PlayerState] = defaultdict(PlayerState)
    X: List[Dict[str, float]] = []
    y: List[int] = []
    metas: List[Dict[str, Any]] = []
    row_index = 0
    for (_td, _tid), tg in df.groupby(["tourney_date_int", "tourney_id"], sort=True):
        for _ro, rg in tg.sort_values(["round_order", "match_num_float"]).groupby("round_order", sort=True):
            for _, row in rg.iterrows():
                if orientation_meta is not None:
                    A_is_winner = bool(orientation_meta.iloc[row_index]["A_is_winner"])
                else:
                    A_is_winner = stable_orientation(row.get("tourney_id"), row.get("match_num"), row.get("winner_id"), row.get("loser_id"))
                feats, yy, meta = build_features_from_historical_row(row, A_is_winner, states, key_mode=key_mode)
                missing = [f for f in feature_order if f not in feats]
                if missing:
                    raise KeyError(f"Missing STEP56 features: {missing[:8]} total={len(missing)}")
                X.append({f: feats[f] for f in feature_order})
                y.append(yy)
                metas.append(meta)
                row_index += 1
            for _, row in rg.iterrows():
                update_player_states_for_match(row, states, key_mode=key_mode)
    return pd.DataFrame(X), np.array(y, dtype=int), pd.DataFrame(metas)


def compute_model_probability(feature_row: Dict[str, float], artifact: Dict[str, Any]) -> float:
    features = artifact["features"]
    mean = artifact["scaler_mean"]
    scale = artifact["scaler_scale"]
    coef = artifact["coef"]
    intercept = float(artifact["intercept"][0])
    z = intercept
    for i, f in enumerate(features):
        s = float(scale[i]) if float(scale[i]) != 0 else 1.0
        x = (float(feature_row.get(f, 0.0)) - float(mean[i])) / s
        z += float(coef[i]) * x
    return sigmoid(z)
