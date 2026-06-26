from __future__ import annotations

"""
Tennis Motor — STEP64 V4 Legacy Lab passif

Objectif : ajouter une couche quantitative plus stricte que la catégorisation v3,
sans modifier le moteur officiel STEP56/v3.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import isfinite
from statistics import pstdev
from typing import Any, Dict, Iterable, List, Optional, Tuple

V4_LEGACY_VERSION = "STEP64_V4_LEGACY_PASSIVE_2026-06-26"
V4_VERSION = V4_LEGACY_VERSION

PRIOR_PROBABILITY = 0.56
MIN_EDGE_BET_A_PLUS = 0.080
MIN_EDGE_BET_A = 0.055
MIN_EDGE_WATCH = 0.030
MIN_DATA_QUALITY_BET = 0.72
MIN_DATA_QUALITY_WATCH = 0.62
MIN_CALIBRATED_PROBABILITY = 0.565

PROBABILITY_SIGNAL_WEIGHTS: Tuple[Tuple[str, float], ...] = (
    ("pSwe", 0.30),
    ("pSWE", 0.30),
    ("pAtp", 0.18),
    ("pATP", 0.18),
    ("pRank", 0.14),
    ("pForm5", 0.10),
    ("pForm10", 0.08),
    ("pSurfaceForm5", 0.08),
    ("pDominance", 0.12),
)

ODD_KEYS = ("oddA", "playerAOdd", "player_a_odd", "coteA", "oddPredicted", "refuseValueOdd")
POINT_KEYS_A = ("playerAPoints", "player_a_points", "sourcePlayerAPoints")
POINT_KEYS_B = ("playerBPoints", "player_b_points", "sourcePlayerBPoints")


@dataclass
class V4Flag:
    code: str
    severity: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


def _s(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        raw = str(value).replace("%", "").replace(",", ".").strip()
        if not raw:
            return default
        out = float(raw)
        if not isfinite(out):
            return default
        return out
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    val = _safe_float(value, None)
    if val is None:
        return default
    try:
        return int(val)
    except Exception:
        return default


def _first(match: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in match and match.get(key) not in (None, ""):
            return match.get(key)
    return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _prob(value: Any, default: Optional[float] = None) -> Optional[float]:
    val = _safe_float(value, None)
    if val is None:
        return default
    if 1.0 < val <= 100.0:
        val = val / 100.0
    if val < 0.0 or val > 1.0:
        return default
    return val


def _pct_prob(match: Dict[str, Any]) -> Optional[float]:
    for key in ("step56Confidence", "premium", "premiumPct", "step56ConfidencePct"):
        if key not in match:
            continue
        p = _prob(match.get(key), None)
        if p is not None:
            return _clamp(p, 0.01, 0.99)
    return None


def _is_not_analyzed(match: Dict[str, Any]) -> bool:
    decision = _s(match.get("decision")).lower()
    status = _s(match.get("analysisStatus")).lower()
    return bool(match.get("nonAnalyzable")) or status == "not_analyzed" or "non analys" in decision


def _predicted_odd(match: Dict[str, Any]) -> float:
    for key in ODD_KEYS:
        odd = _safe_float(match.get(key), 0.0) or 0.0
        if odd > 1.0001:
            return odd
    return 0.0


def _category_v3(match: Dict[str, Any], p: Optional[float]) -> str:
    if _is_not_analyzed(match):
        return "NOT_ANALYZED"
    raw = _s(match.get("step56OfficialCategory") or match.get("category") or match.get("decisionCategory")).upper()
    if "ELITE" in raw or "PREMIUM" in raw:
        return "PREMIUM"
    if "PROCHE" in raw:
        return "PROCHE"
    if "REFUSE" in raw or "REFUS" in raw:
        return "REFUSE"
    if p is None:
        return "UNKNOWN"
    if p > 0.80:
        return "PREMIUM"
    if p >= 0.75:
        return "PROCHE"
    return "REFUSE"


def _surface(match: Dict[str, Any]) -> str:
    raw = _s(match.get("surface") or match.get("courtSurface") or "unknown").lower()
    if "clay" in raw or "terre" in raw:
        return "Clay"
    if "grass" in raw or "gazon" in raw:
        return "Grass"
    if "indoor" in raw:
        return "Indoor"
    if "hard" in raw or "dur" in raw:
        return "Hard"
    if "carpet" in raw:
        return "Carpet"
    return "Unknown"


def _minimum_history(match: Dict[str, Any], a_keys: Iterable[str], b_keys: Iterable[str]) -> int:
    a = _safe_int(_first(match, a_keys, 0), 0)
    b = _safe_int(_first(match, b_keys, 0), 0)
    return min(a, b)


def _valid_points(match: Dict[str, Any]) -> bool:
    a = _safe_float(_first(match, POINT_KEYS_A, 0), 0.0) or 0.0
    b = _safe_float(_first(match, POINT_KEYS_B, 0), 0.0) or 0.0
    return a > 0 and b > 0


def _signal_consensus(match: Dict[str, Any]) -> Tuple[Optional[float], Dict[str, float], float]:
    values: Dict[str, float] = {}
    weighted_sum = 0.0
    weight_sum = 0.0
    seen = set()
    for key, weight in PROBABILITY_SIGNAL_WEIGHTS:
        canonical = key.lower()
        if canonical in seen:
            continue
        p = _prob(match.get(key), None)
        if p is None:
            continue
        seen.add(canonical)
        values[key] = round(p, 6)
        weighted_sum += p * weight
        weight_sum += weight
    if weight_sum <= 0:
        return None, values, 0.0
    consensus = _clamp(weighted_sum / weight_sum, 0.01, 0.99)
    dispersion = pstdev(list(values.values())) if len(values) >= 2 else 0.0
    return consensus, values, dispersion


def _audit_penalties(match: Dict[str, Any]) -> Tuple[float, List[V4Flag]]:
    penalty = 0.0
    flags: List[V4Flag] = []
    audit = match.get("audit") if isinstance(match.get("audit"), dict) else {}
    audit_flags = audit.get("flagCodes") if isinstance(audit, dict) else []
    if not isinstance(audit_flags, list):
        audit_flags = []
    critical_codes = {"ATP_POINTS_MISSING", "CATEGORY_MISMATCH", "PREMIUM_PCT_INVALID", "PROBABILITY_FIELD_INVALID"}
    medium_codes = {"SURFACE_SAMPLE_TOO_LOW", "HISTORY_SAMPLE_TOO_LOW", "FORM_DATA_INCOMPLETE", "ODDS_MISSING"}
    for code in audit_flags:
        code_s = _s(code)
        if code_s in critical_codes:
            penalty += 0.22
            flags.append(V4Flag(code=f"AUDIT_{code_s}", severity="critical", message=f"Flag audit v3 critique : {code_s}."))
        elif code_s in medium_codes:
            penalty += 0.06
            flags.append(V4Flag(code=f"AUDIT_{code_s}", severity="medium", message=f"Flag audit v3 à surveiller : {code_s}."))
    return min(0.45, penalty), flags


def _data_quality(match: Dict[str, Any], signal_dispersion: float, odd: float) -> Tuple[float, List[V4Flag], Dict[str, Any]]:
    flags: List[V4Flag] = []
    details: Dict[str, Any] = {}
    quality = 1.0
    if _is_not_analyzed(match):
        return 0.0, [V4Flag("NOT_ANALYZED", "critical", "Match non analysé par le moteur officiel.")], {"notAnalyzed": True}
    if not _valid_points(match):
        quality -= 0.50
        flags.append(V4Flag("ATP_POINTS_INVALID", "critical", "Points ATP manquants ou à 0."))

    history_min = _minimum_history(match, ("playerAHistoryMatches", "player_a_history_matches"), ("playerBHistoryMatches", "player_b_history_matches"))
    surface_min = _minimum_history(match, ("playerASurfaceHistoryMatches", "playerASurfaceForm5Matches", "player_a_surface_history_matches"), ("playerBSurfaceHistoryMatches", "playerBSurfaceForm5Matches", "player_b_surface_history_matches"))
    form5_min = _minimum_history(match, ("playerAForm5Matches", "player_a_form5_matches"), ("playerBForm5Matches", "player_b_form5_matches"))
    form10_min = _minimum_history(match, ("playerAForm10Matches", "player_a_form10_matches"), ("playerBForm10Matches", "player_b_form10_matches"))
    details.update({"historyMin": history_min, "surfaceHistoryMin": surface_min, "form5Min": form5_min, "form10Min": form10_min})

    if history_min < 10:
        quality -= 0.22
        flags.append(V4Flag("HISTORY_DEPTH_VERY_LOW", "medium", "Historique global très faible sur au moins un joueur."))
    elif history_min < 20:
        quality -= 0.12
        flags.append(V4Flag("HISTORY_DEPTH_LOW", "medium", "Historique global faible sur au moins un joueur."))

    surface_name = _surface(match)
    if surface_min <= 0:
        quality -= 0.18
        flags.append(V4Flag("SURFACE_DEPTH_ZERO", "medium", "Aucun historique surface exploitable sur au moins un joueur."))
    elif surface_min < 3:
        quality -= 0.13
        flags.append(V4Flag("SURFACE_DEPTH_LOW", "medium", "Historique surface très faible."))
    elif surface_min < 5:
        quality -= 0.06
        flags.append(V4Flag("SURFACE_DEPTH_BORDERLINE", "low", "Historique surface un peu court."))

    if surface_name == "Grass" and surface_min < 8:
        quality -= 0.07
        flags.append(V4Flag("GRASS_SAMPLE_FRAGILE", "medium", "Gazon : surface plus volatile avec échantillon court."))
    elif surface_name == "Indoor" and surface_min < 5:
        quality -= 0.04
        flags.append(V4Flag("INDOOR_SAMPLE_FRAGILE", "low", "Indoor : confiance réduite si l'échantillon surface est court."))

    if form5_min < 5:
        quality -= 0.04
        flags.append(V4Flag("FORM5_INCOMPLETE", "low", "Form5 incomplet pour au moins un joueur."))
    if form10_min < 8:
        quality -= 0.04
        flags.append(V4Flag("FORM10_INCOMPLETE", "low", "Form10 incomplet ou court pour au moins un joueur."))

    invalid_prob_fields = []
    for key in ("pSwe", "pSWE", "pAtp", "pATP", "pRank", "pForm5", "pForm10", "pSurfaceForm5", "pDominance"):
        if key in match and match.get(key) not in (None, "") and _prob(match.get(key), None) is None:
            invalid_prob_fields.append(key)
    if invalid_prob_fields:
        quality -= min(0.24, 0.08 * len(invalid_prob_fields))
        flags.append(V4Flag("INVALID_PROBABILITY_SIGNALS", "critical", f"Signaux de probabilité invalides : {', '.join(invalid_prob_fields)}."))
        details["invalidProbabilityFields"] = invalid_prob_fields

    if signal_dispersion > 0.18:
        quality -= 0.10
        flags.append(V4Flag("SIGNAL_DISAGREEMENT_HIGH", "medium", "Les signaux internes divergent fortement."))
    elif signal_dispersion > 0.12:
        quality -= 0.05
        flags.append(V4Flag("SIGNAL_DISAGREEMENT_MEDIUM", "low", "Les signaux internes divergent modérément."))
    details["signalDispersion"] = round(signal_dispersion, 6)

    if odd <= 1.0001:
        flags.append(V4Flag("MARKET_ODDS_MISSING", "medium", "Cote absente : edge marché impossible à calculer."))
        details["marketOddsAvailable"] = False
    else:
        details["marketOddsAvailable"] = True

    audit_penalty, audit_flags = _audit_penalties(match)
    if audit_penalty > 0:
        quality -= audit_penalty
        flags.extend(audit_flags)
    return round(_clamp(quality, 0.0, 1.0), 3), flags, details


def _blend_raw_probability(step56_p: float, consensus_p: Optional[float], dispersion: float) -> float:
    if consensus_p is None:
        return step56_p
    consensus_weight = 0.34 if dispersion <= 0.10 else 0.24 if dispersion <= 0.16 else 0.16
    return _clamp((1.0 - consensus_weight) * step56_p + consensus_weight * consensus_p, 0.01, 0.99)


def _calibrate_probability(raw_p: float, data_quality: float, surface_name: str, signal_dispersion: float) -> float:
    reliability = 0.60 + 0.30 * data_quality
    if surface_name == "Grass":
        reliability -= 0.04
    if signal_dispersion > 0.15:
        reliability -= 0.06
    reliability = _clamp(reliability, 0.45, 0.92)
    calibrated = PRIOR_PROBABILITY + (raw_p - PRIOR_PROBABILITY) * reliability
    return round(_clamp(calibrated, 0.01, 0.99), 4)


def _risk_rank(risk: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(risk, 0)


def _max_risk(a: str, b: str) -> str:
    return a if _risk_rank(a) >= _risk_rank(b) else b


def _favorite_trap_risk(*, odd: float, calibrated_p: float, market_p: Optional[float], data_quality: float, edge: Optional[float], category: str, flags: List[V4Flag]) -> Tuple[str, List[str]]:
    reasons: List[str] = []
    risk = "low"
    if odd > 1.0001 and odd < 1.18:
        reasons.append("cote très basse")
        risk = _max_risk(risk, "medium")
    if market_p is not None and calibrated_p < market_p:
        reasons.append("proba calibrée inférieure à la proba implicite")
        risk = "high"
    if edge is not None and edge < 0.015 and odd > 1.0001:
        reasons.append("edge insuffisant")
        risk = _max_risk(risk, "medium")
    if data_quality < 0.62:
        reasons.append("qualité données faible")
        risk = _max_risk(risk, "medium")
    if category == "PREMIUM" and odd > 1.0001 and edge is not None and edge <= 0:
        reasons.append("Premium v3 mais value négative")
        risk = "high"
    if any(f.code in {"SURFACE_DEPTH_ZERO", "SURFACE_DEPTH_LOW", "GRASS_SAMPLE_FRAGILE"} for f in flags) and category == "PREMIUM":
        reasons.append("Premium avec surface fragile")
        risk = _max_risk(risk, "medium")
    return risk, reasons


def _grade_and_decision(*, calibrated_p: float, edge: Optional[float], data_quality: float, odd: float, trap_risk: str, category: str) -> Tuple[str, str, str]:
    if edge is None or odd <= 1.0001:
        if calibrated_p >= 0.75 and data_quality >= 0.72:
            return "B", "WATCH_NO_MARKET", "Bonne prédiction possible, mais aucune cote exploitable : impossible de valider la value."
        return "C", "WATCH_NO_MARKET", "Cote manquante : V4 ne valide pas sans edge marché."
    if data_quality < 0.55:
        return "D", "NO_BET_DATA_RISK", "Qualité des données insuffisante pour engager une décision V4."
    if calibrated_p < MIN_CALIBRATED_PROBABILITY:
        return "D", "NO_BET_PROBABILITY_TOO_LOW", "Probabilité calibrée trop basse."
    if trap_risk == "high" and edge < MIN_EDGE_BET_A_PLUS:
        return "D", "NO_BET_FAVORITE_TRAP", "Risque de piège favori ou value négative/insuffisante."
    if edge >= MIN_EDGE_BET_A_PLUS and data_quality >= 0.82:
        return "A+", "BET_VALUE_STRONG", "Edge fort après calibration avec données propres."
    if edge >= MIN_EDGE_BET_A and data_quality >= MIN_DATA_QUALITY_BET:
        return "A", "BET_VALUE", "Edge positif après calibration avec qualité de données suffisante."
    if edge >= MIN_EDGE_WATCH and data_quality >= MIN_DATA_QUALITY_WATCH:
        return "B", "WATCH_VALUE_BORDERLINE", "Edge positif mais pas assez fort pour une validation stricte."
    if category == "PREMIUM" and edge < 0:
        return "D", "NO_BET_NEGATIVE_EDGE", "Le joueur peut gagner, mais la cote est trop basse contre la proba calibrée."
    return "C", "NO_BET_NO_EDGE", "Aucun edge V4 suffisant contre le marché."


def _stake_units(grade: str, decision: str, edge: Optional[float], data_quality: float) -> float:
    if not decision.startswith("BET") or edge is None:
        return 0.0
    if grade == "A+":
        return round(min(1.0, 0.75 + max(0.0, edge - MIN_EDGE_BET_A_PLUS) * 2.0) * data_quality, 2)
    if grade == "A":
        return round(min(0.75, 0.55 + max(0.0, edge - MIN_EDGE_BET_A) * 1.5) * data_quality, 2)
    return 0.0


def _short_reason(decision: str, grade: str, calibrated_p: float, edge: Optional[float], data_quality: float, odd: float, trap_risk: str) -> str:
    prob_txt = f"proba calibrée {calibrated_p * 100:.1f}%"
    quality_txt = f"qualité {data_quality * 100:.0f}%"
    if edge is None:
        return f"V4 {grade} — {decision} : {prob_txt}, cote absente, {quality_txt}."
    edge_txt = f"edge {edge * 100:+.1f} pts"
    return f"V4 {grade} — {decision} : {prob_txt}, cote {odd:.2f}, {edge_txt}, {quality_txt}, piège favori {trap_risk}."


def analyze_match_v4(match: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(match, dict):
        return {"version": V4_VERSION, "status": "error", "decision": "NO_BET_INVALID_ROW", "grade": "X"}
    player_a = _s(match.get("playerA") or match.get("player_a") or match.get("sourcePlayerA"))
    player_b = _s(match.get("playerB") or match.get("player_b") or match.get("sourcePlayerB"))
    surface_name = _surface(match)
    step56_p = _pct_prob(match)
    category = _category_v3(match, step56_p)
    if _is_not_analyzed(match) or step56_p is None:
        reason = _s(match.get("reason") or match.get("analysisBlockedReason") or match.get("error") or "match non analysé ou premiumPct absent")
        return {"version": V4_VERSION, "status": "blocked", "decision": "NO_BET_NOT_ANALYZED", "grade": "X", "playerA": player_a, "playerB": player_b, "v3Category": category, "reason": reason, "dataQualityScore": 0.0, "flags": [V4Flag("NOT_ANALYZABLE", "critical", reason).to_dict()], "flagCodes": ["NOT_ANALYZABLE"], "policy": "V4 ne force jamais un match non analysé par la v3/STEP56."}
    consensus_p, signal_values, dispersion = _signal_consensus(match)
    odd = _predicted_odd(match)
    data_quality, flags, quality_details = _data_quality(match, dispersion, odd)
    raw_p = _blend_raw_probability(step56_p, consensus_p, dispersion)
    calibrated_p = _calibrate_probability(raw_p, data_quality, surface_name, dispersion)
    market_p: Optional[float] = None
    edge: Optional[float] = None
    value_score: Optional[float] = None
    ev_pct: Optional[float] = None
    if odd > 1.0001:
        market_p = round(_clamp(1.0 / odd, 0.0, 1.0), 4)
        edge = round(calibrated_p - market_p, 4)
        value_score = round(edge * data_quality, 4)
        ev_pct = round(((calibrated_p * odd) - 1.0) * 100.0, 2)
    trap_risk, trap_reasons = _favorite_trap_risk(odd=odd, calibrated_p=calibrated_p, market_p=market_p, data_quality=data_quality, edge=edge, category=category, flags=flags)
    grade, decision, reason = _grade_and_decision(calibrated_p=calibrated_p, edge=edge, data_quality=data_quality, odd=odd, trap_risk=trap_risk, category=category)
    stake = _stake_units(grade, decision, edge, data_quality)
    flags_out = [f.to_dict() for f in flags]
    return {
        "version": V4_VERSION,
        "status": "ok",
        "mode": "passive_lab_no_official_mutation",
        "playerA": player_a,
        "playerB": player_b,
        "pick": player_a,
        "surface": surface_name,
        "v3Category": category,
        "v3Step56Probability": round(step56_p, 4),
        "signalConsensusProbability": round(consensus_p, 4) if consensus_p is not None else None,
        "signalValues": signal_values,
        "signalDispersion": round(dispersion, 4),
        "modelProbabilityRaw": round(raw_p, 4),
        "modelProbabilityCalibrated": calibrated_p,
        "calibrationPolicy": "conservative_shrinkage_to_prior_0_56_by_data_quality_surface_and_signal_dispersion",
        "odd": round(odd, 3) if odd > 0 else 0.0,
        "marketProbability": market_p,
        "edge": edge,
        "valueScore": value_score,
        "expectedValuePct": ev_pct,
        "dataQualityScore": data_quality,
        "dataQualityDetails": quality_details,
        "favoriteTrapRisk": trap_risk,
        "favoriteTrapReasons": trap_reasons,
        "grade": grade,
        "decision": decision,
        "reason": reason,
        "stakeAdvice": {"mode": "flat_or_fractional_unit_capped", "units": stake, "maxBankrollPct": 1.0 if stake > 0 else 0.0, "policy": "Aucune martingale, aucun rattrapage, stake plafonné."},
        "thresholds": {"betAPlusMinEdge": MIN_EDGE_BET_A_PLUS, "betAMinEdge": MIN_EDGE_BET_A, "watchMinEdge": MIN_EDGE_WATCH, "betMinDataQuality": MIN_DATA_QUALITY_BET, "watchMinDataQuality": MIN_DATA_QUALITY_WATCH, "minCalibratedProbability": MIN_CALIBRATED_PROBABILITY},
        "flags": flags_out,
        "flagCodes": [f["code"] for f in flags_out],
        "shortReason": _short_reason(decision, grade, calibrated_p, edge, data_quality, odd, trap_risk),
    }


def _sample(match: Dict[str, Any], v4: Dict[str, Any]) -> Dict[str, Any]:
    return {"playerA": match.get("playerA"), "playerB": match.get("playerB"), "v3Category": v4.get("v3Category"), "grade": v4.get("grade"), "decision": v4.get("decision"), "calibratedProbability": v4.get("modelProbabilityCalibrated"), "odd": v4.get("odd"), "edge": v4.get("edge"), "dataQualityScore": v4.get("dataQualityScore"), "reason": v4.get("shortReason")}


def build_v4_summary(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    decisions: Counter[str] = Counter()
    grades: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    trap: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    quality_buckets: Counter[str] = Counter()
    v3_premium_no_bet = 0
    v4_bet = 0
    v4_watch = 0
    v4_no_bet = 0
    positive_edge = 0
    negative_edge = 0
    no_market = 0
    edge_values: List[float] = []
    quality_values: List[float] = []
    samples: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    valid_matches = [m for m in matches or [] if isinstance(m, dict)]
    for match in valid_matches:
        v4 = match.get("v4Legacy") if isinstance(match.get("v4Legacy"), dict) else {}
        decision = _s(v4.get("decision") or "UNKNOWN")
        grade = _s(v4.get("grade") or "?")
        category = _s(v4.get("v3Category") or "UNKNOWN")
        decisions[decision] += 1
        grades[grade] += 1
        categories[category] += 1
        trap[_s(v4.get("favoriteTrapRisk") or "unknown")] += 1
        for code in v4.get("flagCodes") or []:
            flag_counts[_s(code)] += 1
        q = _safe_float(v4.get("dataQualityScore"), None)
        if q is not None:
            quality_values.append(q)
            if q >= 0.85:
                quality_buckets["excellent"] += 1
            elif q >= 0.72:
                quality_buckets["good"] += 1
            elif q >= 0.55:
                quality_buckets["fragile"] += 1
            else:
                quality_buckets["danger"] += 1
        edge = _safe_float(v4.get("edge"), None)
        if edge is None:
            no_market += 1
        else:
            edge_values.append(edge)
            if edge > 0:
                positive_edge += 1
            elif edge < 0:
                negative_edge += 1
        if decision.startswith("BET"):
            v4_bet += 1
            if len(samples["bet"]) < 10:
                samples["bet"].append(_sample(match, v4))
        elif decision.startswith("WATCH"):
            v4_watch += 1
            if len(samples["watch"]) < 10:
                samples["watch"].append(_sample(match, v4))
        else:
            v4_no_bet += 1
            if category == "PREMIUM":
                v3_premium_no_bet += 1
                if len(samples["v3PremiumNoBet"]) < 10:
                    samples["v3PremiumNoBet"].append(_sample(match, v4))
    avg_edge = round(sum(edge_values) / len(edge_values), 4) if edge_values else None
    avg_quality = round(sum(quality_values) / len(quality_values), 4) if quality_values else None
    return {"version": V4_VERSION, "mode": "passive_lab_no_official_mutation", "totalMatches": len(valid_matches), "v4Bet": v4_bet, "v4Watch": v4_watch, "v4NoBet": v4_no_bet, "v3PremiumNoBet": v3_premium_no_bet, "positiveEdge": positive_edge, "negativeEdge": negative_edge, "noMarket": no_market, "averageEdge": avg_edge, "averageDataQuality": avg_quality, "decisions": dict(decisions), "grades": dict(grades), "v3Categories": dict(categories), "favoriteTrapRisk": dict(trap), "qualityBuckets": dict(quality_buckets), "topFlags": dict(flag_counts.most_common(20)), "samples": dict(samples), "policy": "La V4 Legacy ne remplace pas la v3. Elle valide seulement value + qualité + calibration. Promotion possible uniquement après preuve future."}


def attach_v4_legacy_to_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    matches = payload.get("matches")
    if not isinstance(matches, list):
        payload["v4LegacySummary"] = build_v4_summary([])
        payload.setdefault("daily", {})
        payload["daily"]["v4Legacy"] = payload["v4LegacySummary"]
        return payload
    for match in matches:
        if not isinstance(match, dict):
            continue
        v4 = analyze_match_v4(match)
        match["v4Legacy"] = v4
        match["v4LegacyShortReason"] = v4.get("shortReason") or v4.get("reason") or v4.get("decision")
    summary = build_v4_summary(matches)
    payload["v4LegacySummary"] = summary
    payload.setdefault("daily", {})
    payload["daily"]["v4Legacy"] = {"status": "enabled", "version": V4_VERSION, "mode": "legacy_passive_parallel_lab", "officialMutation": False, "summary": summary}
    return payload


def status_payload() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": V4_VERSION,
        "mode": "legacy_passive_parallel_lab",
        "officialMutation": False,
        "mainQuestion": "La prédiction possède-t-elle une value mesurable contre la cote, après calibration et contrôle qualité ?",
        "outputs": ["modelProbabilityRaw", "modelProbabilityCalibrated", "marketProbability", "edge", "valueScore", "dataQualityScore", "favoriteTrapRisk", "grade", "decision", "stakeAdvice"],
        "grades": {"A+": "BET_VALUE_STRONG", "A": "BET_VALUE", "B": "WATCH seulement", "C": "NO_BET ou information insuffisante", "D": "NO_BET risque/données/edge", "X": "non analysable"},
        "thresholds": {"betAPlusMinEdge": MIN_EDGE_BET_A_PLUS, "betAMinEdge": MIN_EDGE_BET_A, "watchMinEdge": MIN_EDGE_WATCH, "betMinDataQuality": MIN_DATA_QUALITY_BET, "watchMinDataQuality": MIN_DATA_QUALITY_WATCH, "minCalibratedProbability": MIN_CALIBRATED_PROBABILITY, "priorProbability": PRIOR_PROBABILITY},
        "policy": "V4 est volontairement plus stricte que v3 : elle peut refuser un Premium si la cote n'offre pas d'edge ou si les données sont fragiles.",
    }
