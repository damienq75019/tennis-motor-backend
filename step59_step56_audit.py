#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP60 — STEP56 official engine support.

Purpose:
- Add STEP56 Global Direct Prediction as a DARK/AUDIT layer in the real backend.
- Do NOT change premiumPct, decision, category, veto, history sync, or Unity official decisions.
- Use NO odds and NO player names as model features.
- Player names are used only as lookup keys to reconstruct rolling historical state, like Elo/form.

Deployment status: SAFE AUDIT ONLY. Not a production motor replacement.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from step58_live_feature_builder import (
    PlayerState,
    build_features_from_context,
    canonical_name,
    compute_model_probability,
    load_history_dataframe,
    safe_float,
    safe_int,
    update_player_states_for_match,
)

SERVICE_VERSION = "step60-step56-official-engine"
MODEL_ARTIFACT_FILE = "STEP57_step56_sgd_model_artifact.json"


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _get_first_existing(d: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if isinstance(d, dict) and key in d and d.get(key) is not None:
            return d.get(key)
    return default


def _safe_float_any(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", ".").strip())
    except Exception:
        return default


def _safe_int_any(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def _parse_round_code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    compact = raw.replace(" ", "")

    # API-Tennis examples: ATP French Open - 1/32-finals
    if "1/64" in compact:
        return "R128"
    if "1/32" in compact:
        return "R64"
    if "1/16" in compact:
        return "R32"
    if "1/8" in compact or "roundof16" in compact:
        return "R16"
    if "quarter" in raw or "qf" == raw:
        return "QF"
    if "semi" in raw or "sf" == raw:
        return "SF"
    if "final" in raw:
        return "F"

    # Generic round labels.
    if "r128" in compact or "128" in compact:
        return "R128"
    if "r64" in compact or "64" in compact or "2ndround" in compact:
        return "R64"
    if "r32" in compact or "32" in compact or "3rdround" in compact:
        return "R32"
    if "r16" in compact or "16" in compact or "4thround" in compact:
        return "R16"
    return "R64"


def _infer_tournament_level(tournament: Any, season: Any, round_text: Any) -> str:
    text = f"{tournament or ''} {season or ''} {round_text or ''}".lower()
    if any(x in text for x in ["french open", "roland", "wimbledon", "australian open", "us open"]):
        return "G"
    if "masters" in text or "atp 1000" in text or "1000" in text:
        return "M"
    if "davis" in text:
        return "D"
    if "challenger" in text:
        return "C"
    return "A"


def _infer_best_of(level: str, tournament: Any, season: Any) -> int:
    text = f"{tournament or ''} {season or ''}".lower()
    if level == "G" or any(x in text for x in ["french open", "roland", "wimbledon", "australian open", "us open"]):
        return 5
    return 3


def _infer_draw_size(round_code: str, tournament: Any, season: Any) -> float:
    text = f"{tournament or ''} {season or ''}".lower()
    if any(x in text for x in ["french open", "roland", "wimbledon", "australian open", "us open"]):
        return 128.0
    mapping = {"R128": 128.0, "R64": 64.0, "R32": 32.0, "R16": 16.0, "QF": 8.0, "SF": 4.0, "F": 2.0}
    return mapping.get(round_code, 32.0)


def _surface_from_match(match: Dict[str, Any]) -> str:
    raw = str(_get_first_existing(match, ["surface"], "Hard") or "Hard").strip().title()
    if "Clay" in raw:
        return "Clay"
    if "Grass" in raw:
        return "Grass"
    if "Carpet" in raw:
        return "Carpet"
    return "Hard"


class Step56LiveAuditor:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent)
        self.data_dir = self.base_dir / "data"
        self.artifact_path = self.base_dir / MODEL_ARTIFACT_FILE
        self.lock = threading.Lock()
        self.loaded = False
        self.load_error = ""
        self.artifact: Dict[str, Any] = {}
        self.feature_order: List[str] = []
        self.states: Dict[str, PlayerState] = defaultdict(PlayerState)
        self.player_profile: Dict[str, Dict[str, Any]] = {}
        self.history_rows_loaded = 0
        self.history_years: List[int] = []

    def load(self, force: bool = False) -> None:
        with self.lock:
            if self.loaded and not force:
                return
            self.loaded = False
            self.load_error = ""
            self.states = defaultdict(PlayerState)
            self.player_profile = {}
            self.history_rows_loaded = 0
            self.history_years = []
            try:
                with self.artifact_path.open("r", encoding="utf-8") as f:
                    self.artifact = json.load(f)
                self.feature_order = list(self.artifact.get("features") or [])
                if not self.feature_order:
                    raise RuntimeError("STEP56 artifact has no features")

                years = []
                for path in sorted(self.data_dir.glob("*.csv")):
                    m = re.match(r"(?:atp_matches_)?(\d{4})\.csv$", path.name)
                    if m:
                        years.append(int(m.group(1)))
                years = sorted(set(years)) or [2022, 2023, 2024, 2025, 2026]
                self.history_years = years
                df = load_history_dataframe(self.data_dir, years=years)
                self.history_rows_loaded = int(len(df))
                self._build_player_profile(df)

                # Replay full historical state by canonical player name.
                for (_td, _tid), tg in df.groupby(["tourney_date_int", "tourney_id"], sort=True):
                    for _ro, rg in tg.sort_values(["round_order", "match_num_float"]).groupby("round_order", sort=True):
                        for _, row in rg.iterrows():
                            update_player_states_for_match(row, self.states, key_mode="name")
                self.loaded = True
            except Exception as exc:
                self.load_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
                self.loaded = False

    def _build_player_profile(self, df: pd.DataFrame) -> None:
        profiles: Dict[str, Dict[str, Any]] = {}
        for _, row in df.iterrows():
            for pref in ("winner", "loser"):
                name = str(row.get(f"{pref}_name") or "")
                key = canonical_name(name)
                if not key:
                    continue
                p = profiles.setdefault(key, {"name": name, "hand": "", "height": 0.0, "age": 0.0, "rank": 0.0, "points": 0.0, "seen": 0})
                p["seen"] = int(p.get("seen", 0)) + 1
                hand = str(row.get(f"{pref}_hand") or "").strip()
                if hand:
                    p["hand"] = hand
                ht = safe_float(row.get(f"{pref}_ht"), 0)
                if ht > 0:
                    p["height"] = ht
                age = safe_float(row.get(f"{pref}_age"), 0)
                if age > 0:
                    p["age"] = age
                rank = safe_float(row.get(f"{pref}_rank"), 0)
                if rank > 0:
                    p["rank"] = rank
                pts = safe_float(row.get(f"{pref}_rank_points"), 0)
                if pts > 0:
                    p["points"] = pts
        self.player_profile = profiles

    def status(self) -> Dict[str, Any]:
        self.load(force=False)
        return {
            "status": "ok" if self.loaded else "error",
            "serviceVersion": SERVICE_VERSION,
            "mode": "official_engine_loaded_audit_endpoint_available",
            "model": "STEP56 Global Direct Prediction",
            "deploymentStatus": "OFFICIAL_ENGINE_ON_DAILY",
            "loaded": self.loaded,
            "loadError": self.load_error if not self.loaded else "",
            "featureCount": len(self.feature_order),
            "historyYears": self.history_years,
            "historyRowsLoaded": self.history_rows_loaded,
            "playerProfiles": len(self.player_profile),
            "noOdds": True,
            "noPlayerNamesAsFeatures": True,
            "namesUsedOnlyForHistoryLookup": True,
            "decisionMutation": False,
        }

    def _profile(self, name: str) -> Dict[str, Any]:
        return self.player_profile.get(canonical_name(name), {})

    def _match_context(self, match: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        warnings: List[str] = []
        player_a = str(_get_first_existing(match, ["playerA", "player_a"], "") or "")
        player_b = str(_get_first_existing(match, ["playerB", "player_b"], "") or "")
        A_key = canonical_name(player_a)
        B_key = canonical_name(player_b)
        if not A_key or not B_key:
            warnings.append("missing_player_name")
        prof_a = self._profile(player_a)
        prof_b = self._profile(player_b)
        if not prof_a:
            warnings.append("playerA_no_history_profile")
        if not prof_b:
            warnings.append("playerB_no_history_profile")

        surface = _surface_from_match(match)
        round_text = _get_first_existing(match, ["round"], "")
        round_code = _parse_round_code(round_text)
        tournament = _get_first_existing(match, ["tournament", "seasonName"], "")
        season = _get_first_existing(match, ["seasonName"], "")
        level = _infer_tournament_level(tournament, season, round_text)
        best_of = _infer_best_of(level, tournament, season)
        draw_size = _infer_draw_size(round_code, tournament, season)

        tourney_id = str(_get_first_existing(
            match,
            ["tourney_id", "sportradarCompetitionId", "competitionId", "tournament", "seasonName"],
            "live_unknown_tournament",
        ) or "live_unknown_tournament")

        A_points = _safe_float_any(_get_first_existing(match, ["playerAPoints", "player_a_points"], 0), 0)
        B_points = _safe_float_any(_get_first_existing(match, ["playerBPoints", "player_b_points"], 0), 0)
        A_rank = _safe_float_any(_get_first_existing(match, ["playerARank", "player_a_rank"], prof_a.get("rank", 0)), 0)
        B_rank = _safe_float_any(_get_first_existing(match, ["playerBRank", "player_b_rank"], prof_b.get("rank", 0)), 0)
        if A_points <= 0 or B_points <= 0:
            warnings.append("missing_atp_points")
        if A_rank <= 0 or B_rank <= 0:
            warnings.append("missing_rank")

        ctx = {
            "states": self.states,
            "A_key": A_key,
            "B_key": B_key,
            "surface": surface,
            "tourney_id": tourney_id,
            "level": level,
            "indoor": _get_first_existing(match, ["indoor"], ""),
            "best_of": best_of,
            "draw_size": draw_size,
            "rnd": round_code,
            "A_points": A_points,
            "B_points": B_points,
            "A_rank": A_rank,
            "B_rank": B_rank,
            "A_height": _safe_float_any(prof_a.get("height", 0), 0),
            "B_height": _safe_float_any(prof_b.get("height", 0), 0),
            "A_age": _safe_float_any(prof_a.get("age", 0), 0),
            "B_age": _safe_float_any(prof_b.get("age", 0), 0),
            "A_hand": prof_a.get("hand", ""),
            "B_hand": prof_b.get("hand", ""),
        }
        return ctx, warnings

    def audit_match(self, match: Dict[str, Any]) -> Dict[str, Any]:
        self.load(force=False)
        if not self.loaded:
            return {
                "status": "error",
                "serviceVersion": SERVICE_VERSION,
                "error": self.load_error,
                "decisionMutation": False,
            }
        if not isinstance(match, dict):
            return {"status": "error", "serviceVersion": SERVICE_VERSION, "error": "invalid_match", "decisionMutation": False}
        if match.get("nonAnalyzable") or match.get("analysisStatus") == "not_analyzed":
            return {
                "status": "skipped",
                "serviceVersion": SERVICE_VERSION,
                "reason": "not_analyzed_by_official_engine",
                "decisionMutation": False,
            }

        try:
            player_a = str(_get_first_existing(match, ["playerA", "player_a"], "") or "")
            player_b = str(_get_first_existing(match, ["playerB", "player_b"], "") or "")
            ctx, warnings = self._match_context(match)
            features = build_features_from_context(**ctx)
            missing = [f for f in self.feature_order if f not in features]
            if missing:
                warnings.append("missing_step56_features")
                return {
                    "status": "error",
                    "serviceVersion": SERVICE_VERSION,
                    "error": f"missing features: {missing[:8]}",
                    "missingFeatureCount": len(missing),
                    "decisionMutation": False,
                }
            prob_a = float(compute_model_probability({f: features[f] for f in self.feature_order}, self.artifact))
            pred_a = prob_a >= 0.5
            predicted = player_a if pred_a else player_b
            confidence = prob_a if pred_a else 1.0 - prob_a
            current_pick = player_a  # calculate_from_matches displays engine-chosen side as playerA.
            current_premium = _safe_float_any(_get_first_existing(match, ["premium", "premiumPct"], 0), 0)
            if current_premium > 1.0:
                current_premium /= 100.0
            category = "AUDIT_ELITE" if confidence >= 0.88 else ("AUDIT_PREMIUM" if confidence >= 0.80 else ("AUDIT_CLOSE" if confidence >= 0.75 else "AUDIT_REFUSE"))
            agreement = (predicted == current_pick)

            compact_features = {
                "p_swe": round(features.get("p_swe", 0.0), 4),
                "p_atp": round(features.get("p_atp", 0.0), 4),
                "p_rank": round(features.get("p_rank", 0.0), 4),
                "old_premium": round(features.get("old_premium", 0.0), 4),
                "spw_diff10": round(features.get("spw_diff10", 0.0), 4),
                "rpw_diff10": round(features.get("rpw_diff10", 0.0), 4),
                "surf_spw_diff10": round(features.get("surf_spw_diff10", 0.0), 4),
                "surf_rpw_diff10": round(features.get("surf_rpw_diff10", 0.0), 4),
                "last_minutes_diff": round(features.get("last_minutes_diff", 0.0), 2),
                "current_tourney_matches_diff": round(features.get("current_tourney_matches_diff", 0.0), 2),
                "height_diff": round(features.get("height_diff", 0.0), 2),
                "age_diff": round(features.get("age_diff", 0.0), 2),
            }
            return {
                "status": "ok",
                "serviceVersion": SERVICE_VERSION,
                "mode": "step56_probability_computation",
                "model": "STEP56 Global Direct Prediction",
                "noOddsUsed": True,
                "noPlayerNamesAsFeatures": True,
                "namesUsedOnlyForHistoryLookup": True,
                "decisionMutation": False,
                "featureCount": len(self.feature_order),
                "warnings": warnings,
                "playerA": player_a,
                "playerB": player_b,
                "probabilityA": round(prob_a, 6),
                "probabilityAPct": round(prob_a * 100.0, 2),
                "predictedWinner": predicted,
                "predictedWinnerSide": "A" if pred_a else "B",
                "confidence": round(confidence, 6),
                "confidencePct": round(confidence * 100.0, 2),
                "auditCategory": category,
                "agreesWithOfficialDisplayedPick": agreement,
                "officialDisplayedPick": current_pick,
                "officialPremiumPct": round(current_premium * 100.0, 2),
                "context": {
                    "surface": ctx.get("surface"),
                    "level": ctx.get("level"),
                    "bestOf": ctx.get("best_of"),
                    "round": ctx.get("rnd"),
                    "drawSize": ctx.get("draw_size"),
                    "tourneyId": ctx.get("tourney_id"),
                    "playerAProfileSeen": int(self._profile(player_a).get("seen", 0) or 0),
                    "playerBProfileSeen": int(self._profile(player_b).get("seen", 0) or 0),
                },
                "featureSample": compact_features,
            }
        except Exception as exc:
            return {
                "status": "error",
                "serviceVersion": SERVICE_VERSION,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=4),
                "decisionMutation": False,
            }

    def enrich_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        matches = response.get("matches") if isinstance(response, dict) else []
        if not isinstance(matches, list):
            return {"status": "error", "serviceVersion": SERVICE_VERSION, "error": "response_has_no_matches_list"}
        counts = {"ok": 0, "error": 0, "skipped": 0, "agree": 0, "disagree": 0, "auditElite": 0, "auditPremium": 0, "auditClose": 0, "auditRefuse": 0}
        sample: List[Dict[str, Any]] = []
        for match in matches:
            if not isinstance(match, dict):
                counts["error"] += 1
                continue
            audit = self.audit_match(match)
            match["step56Audit"] = audit
            status = audit.get("status", "error")
            if status == "ok":
                counts["ok"] += 1
                if audit.get("agreesWithOfficialDisplayedPick"):
                    counts["agree"] += 1
                else:
                    counts["disagree"] += 1
                cat = audit.get("auditCategory")
                if cat == "AUDIT_ELITE": counts["auditElite"] += 1
                elif cat == "AUDIT_PREMIUM": counts["auditPremium"] += 1
                elif cat == "AUDIT_CLOSE": counts["auditClose"] += 1
                else: counts["auditRefuse"] += 1
                if len(sample) < 12:
                    sample.append({
                        "match": f"{audit.get('playerA')} vs {audit.get('playerB')}",
                        "officialPremiumPct": audit.get("officialPremiumPct"),
                        "step56ConfidencePct": audit.get("confidencePct"),
                        "step56PredictedWinner": audit.get("predictedWinner"),
                        "auditCategory": audit.get("auditCategory"),
                        "agreement": audit.get("agreesWithOfficialDisplayedPick"),
                    })
            elif status == "skipped":
                counts["skipped"] += 1
            else:
                counts["error"] += 1
        response.setdefault("daily", {})
        response["daily"]["step56Audit"] = {
            "status": "ok" if counts["error"] == 0 else "partial",
            "serviceVersion": SERVICE_VERSION,
            "mode": "step56_probability_computation",
            "officialEngineUnchanged": False,
            "historyWrittenOnlyByDailyEndpoint": True,
            "noOddsUsed": True,
            "noPlayerNamesAsFeatures": True,
            "counts": counts,
            "sample": sample,
        }
        return response["daily"]["step56Audit"]


_AUDITOR: Optional[Step56LiveAuditor] = None
_AUDITOR_LOCK = threading.Lock()


def get_step56_auditor() -> Step56LiveAuditor:
    global _AUDITOR
    with _AUDITOR_LOCK:
        if _AUDITOR is None:
            _AUDITOR = Step56LiveAuditor()
        return _AUDITOR
