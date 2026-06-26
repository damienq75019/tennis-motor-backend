from __future__ import annotations

"""
Tennis Motor — STEP65 V4 Full Candidate Engine (passif)

Objectif : analyser toutes les catégories v3/STEP56 (Premium, Proche, Refusé,
Non analysé) avec une logique V4 distincte, sans modifier la décision officielle.

La V4 ne remplace pas STEP56. Elle ajoute :
- calibration prudente de la probabilité ;
- edge contre la cote ;
- qualité des données ;
- risque de piège favori ;
- action V4 spécifique par catégorie v3 ;
- décision haute-niveau BET / WATCH / NO_BET / BLOCKED.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import isfinite
from statistics import pstdev
from typing import Any, Dict, Iterable, List, Optional, Tuple

V4_VERSION = "STEP65_V4_FULL_CANDIDATE_2026-06-26"

# Calibration prudente : Tennis Motor ne doit jamais croire brutalement son score brut.
PRIOR_PROBABILITY = 0.56

# Seuils V4 généraux.
MIN_DATA_QUALITY_BET = 0.72
MIN_DATA_QUALITY_WATCH = 0.60
MIN_CALIBRATED_PROBABILITY = 0.555

# Seuils d'edge par catégorie v3.
PREMIUM_EDGE_BET = 0.045
PREMIUM_EDGE_STRONG = 0.075
PREMIUM_EDGE_WATCH = 0.020

PROCHE_EDGE_UPGRADE = 0.060
PROCHE_EDGE_STRONG = 0.085
PROCHE_EDGE_WATCH = 0.030

REFUSE_EDGE_STRONG = 0.075
REFUSE_EDGE_WATCH = 0.045
REFUSE_MIN_CALIBRATED_PROBABILITY = 0.555

# Les signaux sont utilisés comme consensus secondaire, jamais comme remplacement brutal de STEP56.
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

ODD_KEYS = (
    "oddA",
    "playerAOdd",
    "player_a_odd",
    "coteA",
    "oddPredicted",
    "refuseValueOdd",
)
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
    reason = _s(match.get("reason") or match.get("analysisBlockedReason")).lower()
    return bool(match.get("nonAnalyzable")) or status == "not_analyzed" or "non analys" in decision or "not_analyzed" in reason


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
    consensus_weight = 0.36 if dispersion <= 0.10 else 0.25 if dispersion <= 0.16 else 0.16
    return _clamp((1.0 - consensus_weight) * step56_p + consensus_weight * consensus_p, 0.01, 0.99)


def _calibrate_probability(raw_p: float, data_quality: float, surface_name: str, signal_dispersion: float, category: str) -> float:
    reliability = 0.58 + 0.32 * data_quality
    if surface_name == "Grass":
        reliability -= 0.045
    elif surface_name == "Unknown":
        reliability -= 0.035
    if signal_dispersion > 0.15:
        reliability -= 0.06
    if category == "REFUSE":
        # Un refusé v3 doit avoir une preuve plus forte : on shrink davantage.
        reliability -= 0.035
    reliability = _clamp(reliability, 0.42, 0.92)
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
    if data_quality < 0.60:
        reasons.append("qualité données faible")
        risk = _max_risk(risk, "medium")
    if category == "PREMIUM" and odd > 1.0001 and edge is not None and edge <= 0:
        reasons.append("Premium v3 mais value négative")
        risk = "high"
    if any(f.code in {"SURFACE_DEPTH_ZERO", "SURFACE_DEPTH_LOW", "GRASS_SAMPLE_FRAGILE"} for f in flags) and category in {"PREMIUM", "PROCHE"}:
        reasons.append("catégorie haute avec surface fragile")
        risk = _max_risk(risk, "medium")
    return risk, reasons


def _decision_for_premium(*, calibrated_p: float, edge: Optional[float], data_quality: float, odd: float, trap_risk: str) -> Tuple[str, str, str, str]:
    if data_quality < 0.55:
        return "NO_BET", "DOWNGRADE_PREMIUM_DATA_RISK", "D", "Premium v3 rétrogradé : qualité de données insuffisante."
    if edge is None or odd <= 1.0001:
        if calibrated_p >= 0.76 and data_quality >= 0.72:
            return "WATCH", "WATCH_PREMIUM_NO_MARKET", "B", "Premium v3 solide, mais cote absente : value non vérifiable."
        return "NO_BET", "DOWNGRADE_PREMIUM_NO_MARKET", "C", "Premium v3 non validé : cote absente et preuve V4 incomplète."
    if trap_risk == "high" and edge < PREMIUM_EDGE_STRONG:
        return "NO_BET", "DOWNGRADE_PREMIUM_FAVORITE_TRAP", "D", "Premium v3 rétrogradé : risque de piège favori ou edge insuffisant."
    if edge >= PREMIUM_EDGE_STRONG and data_quality >= 0.82 and calibrated_p >= 0.68:
        return "BET", "VALIDATE_PREMIUM_STRONG_VALUE", "A+", "Premium v3 validé par la V4 : edge fort, données propres."
    if edge >= PREMIUM_EDGE_BET and data_quality >= MIN_DATA_QUALITY_BET and calibrated_p >= 0.64:
        return "BET", "VALIDATE_PREMIUM_VALUE", "A", "Premium v3 validé : edge positif après calibration."
    if edge >= PREMIUM_EDGE_WATCH and data_quality >= MIN_DATA_QUALITY_WATCH:
        return "WATCH", "WATCH_PREMIUM_BORDERLINE", "B", "Premium v3 à surveiller : edge positif mais pas assez fort."
    if edge < 0:
        return "NO_BET", "DOWNGRADE_PREMIUM_NEGATIVE_EDGE", "D", "Premium v3 refusé par la V4 : cote trop basse contre la proba calibrée."
    return "NO_BET", "DOWNGRADE_PREMIUM_NO_EDGE", "C", "Premium v3 non validé : edge V4 insuffisant."


def _decision_for_proche(*, calibrated_p: float, edge: Optional[float], data_quality: float, odd: float, trap_risk: str) -> Tuple[str, str, str, str]:
    if data_quality < 0.55:
        return "NO_BET", "NO_BET_PROCHE_DATA_RISK", "D", "Proche v3 refusé : données trop fragiles."
    if edge is None or odd <= 1.0001:
        return "WATCH", "WATCH_PROCHE_NO_MARKET", "C", "Proche v3 : cote absente, impossible de mesurer la value."
    if trap_risk == "high" and edge < PROCHE_EDGE_STRONG:
        return "NO_BET", "NO_BET_PROCHE_FAVORITE_TRAP", "D", "Proche v3 refusé : piège favori ou marché défavorable."
    if edge >= PROCHE_EDGE_STRONG and data_quality >= 0.78 and calibrated_p >= 0.61:
        return "BET", "UPGRADE_PROCHE_TO_STRONG_VALUE", "A", "Proche v3 monté par la V4 : edge fort et données suffisamment propres."
    if edge >= PROCHE_EDGE_UPGRADE and data_quality >= MIN_DATA_QUALITY_BET and calibrated_p >= 0.59:
        return "BET", "UPGRADE_PROCHE_TO_VALUE", "A", "Proche v3 monté : value positive après calibration."
    if edge >= PROCHE_EDGE_WATCH and data_quality >= MIN_DATA_QUALITY_WATCH:
        return "WATCH", "WATCH_PROCHE_VALUE", "B", "Proche v3 à surveiller : value possible mais limite."
    if edge < 0:
        return "NO_BET", "NO_BET_PROCHE_NEGATIVE_EDGE", "D", "Proche v3 refusé : edge négatif contre le marché."
    return "NO_BET", "NO_BET_PROCHE_NO_EDGE", "C", "Proche v3 confirmé sans value suffisante."


def _decision_for_refuse(*, match: Dict[str, Any], calibrated_p: float, edge: Optional[float], data_quality: float, odd: float, trap_risk: str) -> Tuple[str, str, str, str]:
    if data_quality < 0.52:
        return "NO_BET", "REFUSE_DATA_TOO_WEAK", "D", "Refusé v3 confirmé : données trop fragiles pour chercher une value."
    if edge is None or odd <= 1.0001:
        # La couche Refuse Value historique peut rester en WATCH sans forcer un pari.
        if bool(match.get("refuseValueStrict")) or bool(match.get("refuseValueLarge")):
            return "WATCH", "REFUSE_VALUE_WATCH_NO_MARKET", "B", "Refusé v3 avec signal Refuse Value, mais cote absente/non exploitable."
        return "NO_BET", "REFUSE_CONFIRMED_NO_MARKET", "C", "Refusé v3 confirmé : cote absente, pas de value mesurable."
    if trap_risk == "high" and edge < REFUSE_EDGE_STRONG:
        return "NO_BET", "REFUSE_CONFIRMED_FAVORITE_TRAP", "D", "Refusé v3 confirmé : marché défavorable ou piège favori."
    if edge >= REFUSE_EDGE_STRONG and data_quality >= 0.74 and calibrated_p >= REFUSE_MIN_CALIBRATED_PROBABILITY:
        return "BET", "REFUSE_VALUE_STRONG", "A", "Refusé v3 transformé en candidate V4 : edge fort après calibration."
    if edge >= REFUSE_EDGE_WATCH and data_quality >= MIN_DATA_QUALITY_WATCH and calibrated_p >= REFUSE_MIN_CALIBRATED_PROBABILITY:
        return "WATCH", "REFUSE_VALUE_WATCH", "B", "Refusé v3 à surveiller : value possible mais pas assez robuste."
    if bool(match.get("refuseValueStrict")) and edge > 0 and data_quality >= 0.58:
        return "WATCH", "REFUSE_VALUE_HISTORICAL_WATCH", "B", "Refusé v3 avec signal Refuse Value historique et edge positif modéré."
    if edge < 0:
        return "NO_BET", "REFUSE_CONFIRMED_NEGATIVE_EDGE", "D", "Refusé v3 confirmé : edge négatif."
    return "NO_BET", "REFUSE_CONFIRMED_NO_VALUE", "C", "Refusé v3 confirmé : pas assez de value pour le repêcher."


def _decision_for_unknown(*, calibrated_p: float, edge: Optional[float], data_quality: float, odd: float) -> Tuple[str, str, str, str]:
    if data_quality < 0.55:
        return "NO_BET", "UNKNOWN_CATEGORY_DATA_RISK", "D", "Catégorie v3 inconnue : données fragiles."
    if edge is not None and edge >= 0.075 and data_quality >= 0.76 and calibrated_p >= 0.60:
        return "WATCH", "UNKNOWN_CATEGORY_VALUE_WATCH", "B", "Catégorie v3 inconnue mais edge positif : à surveiller, pas à promouvoir."
    return "NO_BET", "UNKNOWN_CATEGORY_NO_BET", "C", "Catégorie v3 inconnue : aucune décision V4 forte."


def _stake_units(v4_decision: str, grade: str, edge: Optional[float], data_quality: float, category: str) -> float:
    if v4_decision != "BET" or edge is None:
        return 0.0
    # La V4 reste passive : ceci est une unité théorique plafonnée, pas une instruction d'ordre.
    base = 0.0
    if grade == "A+":
        base = 0.75 + max(0.0, edge - 0.075) * 2.0
    elif grade == "A":
        base = 0.50 + max(0.0, edge - 0.045) * 1.5
    if category == "REFUSE":
        base *= 0.75
    return round(min(1.0, base) * data_quality, 2)


def _short_reason(v4_decision: str, v4_action: str, grade: str, calibrated_p: Optional[float], edge: Optional[float], data_quality: float, odd: float, category: str) -> str:
    if calibrated_p is None:
        return f"V4 {grade} — {category} — {v4_action} : match bloqué/non analysable."
    prob_txt = f"proba calibrée {calibrated_p * 100:.1f}%"
    quality_txt = f"qualité {data_quality * 100:.0f}%"
    if edge is None:
        return f"V4 {grade} — {category} — {v4_decision}/{v4_action} : {prob_txt}, cote absente, {quality_txt}."
    edge_txt = f"edge {edge * 100:+.1f} pts"
    return f"V4 {grade} — {category} — {v4_decision}/{v4_action} : {prob_txt}, cote {odd:.2f}, {edge_txt}, {quality_txt}."


def analyze_match_v4(match: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(match, dict):
        return {"version": V4_VERSION, "status": "error", "v4Decision": "NO_BET", "v4Action": "NO_BET_INVALID_ROW", "decision": "NO_BET_INVALID_ROW", "grade": "X"}

    player_a = _s(match.get("playerA") or match.get("player_a") or match.get("sourcePlayerA"))
    player_b = _s(match.get("playerB") or match.get("player_b") or match.get("sourcePlayerB"))
    surface_name = _surface(match)
    step56_p = _pct_prob(match)
    category = _category_v3(match, step56_p)

    if _is_not_analyzed(match) or step56_p is None:
        reason = _s(match.get("reason") or match.get("analysisBlockedReason") or match.get("error") or "match non analysé ou premiumPct absent")
        blocked_reason = "BLOCKED_NOT_ANALYZED"
        if "point" in reason.lower() or "atp" in reason.lower():
            blocked_reason = "BLOCKED_ATP_POINTS_MISSING"
        elif "sinner" in reason.lower() or "excluded" in reason.lower() or "exclu" in reason.lower():
            blocked_reason = "BLOCKED_PLAYER_EXCLUDED"
        out = {
            "version": V4_VERSION,
            "status": "blocked",
            "mode": "full_candidate_passive_no_official_mutation",
            "scope": "all_categories",
            "v4Decision": "BLOCKED",
            "v4Action": blocked_reason,
            "decision": blocked_reason,
            "grade": "X",
            "playerA": player_a,
            "playerB": player_b,
            "pick": player_a,
            "surface": surface_name,
            "v3Category": category,
            "reason": reason,
            "dataQualityScore": 0.0,
            "flags": [V4Flag("NOT_ANALYZABLE", "critical", reason).to_dict()],
            "flagCodes": ["NOT_ANALYZABLE"],
            "shortReason": f"V4 X — {blocked_reason} : {reason}",
            "policy": "V4 ne force jamais un match non analysé par la v3/STEP56.",
        }
        return out

    consensus_p, signal_values, dispersion = _signal_consensus(match)
    odd = _predicted_odd(match)
    data_quality, flags, quality_details = _data_quality(match, dispersion, odd)
    raw_p = _blend_raw_probability(step56_p, consensus_p, dispersion)
    calibrated_p = _calibrate_probability(raw_p, data_quality, surface_name, dispersion, category)

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

    if category == "PREMIUM":
        v4_decision, v4_action, grade, reason = _decision_for_premium(calibrated_p=calibrated_p, edge=edge, data_quality=data_quality, odd=odd, trap_risk=trap_risk)
    elif category == "PROCHE":
        v4_decision, v4_action, grade, reason = _decision_for_proche(calibrated_p=calibrated_p, edge=edge, data_quality=data_quality, odd=odd, trap_risk=trap_risk)
    elif category == "REFUSE":
        v4_decision, v4_action, grade, reason = _decision_for_refuse(match=match, calibrated_p=calibrated_p, edge=edge, data_quality=data_quality, odd=odd, trap_risk=trap_risk)
    else:
        v4_decision, v4_action, grade, reason = _decision_for_unknown(calibrated_p=calibrated_p, edge=edge, data_quality=data_quality, odd=odd)

    stake = _stake_units(v4_decision, grade, edge, data_quality, category)
    flags_out = [f.to_dict() for f in flags]
    short_reason = _short_reason(v4_decision, v4_action, grade, calibrated_p, edge, data_quality, odd, category)

    return {
        "version": V4_VERSION,
        "status": "ok",
        "mode": "full_candidate_passive_no_official_mutation",
        "scope": "all_categories",
        "playerA": player_a,
        "playerB": player_b,
        "pick": player_a,
        "surface": surface_name,
        "v3Category": category,
        "v3Step56Probability": round(step56_p, 4),
        "candidateType": {
            "PREMIUM": "control_and_validate_or_downgrade",
            "PROCHE": "upgrade_watch_or_confirm_no_bet",
            "REFUSE": "search_refuse_value_or_confirm_refuse",
            "UNKNOWN": "conservative_watch_only",
        }.get(category, "blocked_or_unknown"),
        "signalConsensusProbability": round(consensus_p, 4) if consensus_p is not None else None,
        "signalValues": signal_values,
        "signalDispersion": round(dispersion, 4),
        "modelProbabilityRaw": round(raw_p, 4),
        "modelProbabilityCalibrated": calibrated_p,
        "calibrationPolicy": "conservative_shrinkage_to_prior_0_56_by_data_quality_surface_signal_dispersion_and_category",
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
        "v4Decision": v4_decision,
        "v4Action": v4_action,
        # Compatibilité STEP64/Unity : `decision` garde l'action détaillée.
        "decision": v4_action,
        "reason": reason,
        "stakeAdvice": {
            "mode": "flat_or_fractional_unit_capped",
            "units": stake,
            "maxBankrollPct": 1.0 if stake > 0 else 0.0,
            "policy": "Aucune martingale, aucun rattrapage, stake plafonné. V4 reste passive tant qu'elle n'est pas validée hors-échantillon.",
        },
        "thresholds": {
            "premiumEdgeBet": PREMIUM_EDGE_BET,
            "premiumEdgeStrong": PREMIUM_EDGE_STRONG,
            "premiumEdgeWatch": PREMIUM_EDGE_WATCH,
            "procheEdgeUpgrade": PROCHE_EDGE_UPGRADE,
            "procheEdgeStrong": PROCHE_EDGE_STRONG,
            "procheEdgeWatch": PROCHE_EDGE_WATCH,
            "refuseEdgeStrong": REFUSE_EDGE_STRONG,
            "refuseEdgeWatch": REFUSE_EDGE_WATCH,
            "betMinDataQuality": MIN_DATA_QUALITY_BET,
            "watchMinDataQuality": MIN_DATA_QUALITY_WATCH,
            "minCalibratedProbability": MIN_CALIBRATED_PROBABILITY,
        },
        "flags": flags_out,
        "flagCodes": [f["code"] for f in flags_out],
        "shortReason": short_reason,
    }


def _sample(match: Dict[str, Any], v4: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "playerA": match.get("playerA"),
        "playerB": match.get("playerB"),
        "v3Category": v4.get("v3Category"),
        "grade": v4.get("grade"),
        "v4Decision": v4.get("v4Decision"),
        "v4Action": v4.get("v4Action") or v4.get("decision"),
        "calibratedProbability": v4.get("modelProbabilityCalibrated"),
        "odd": v4.get("odd"),
        "edge": v4.get("edge"),
        "dataQualityScore": v4.get("dataQualityScore"),
        "reason": v4.get("shortReason") or v4.get("reason"),
    }


def build_v4_summary(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    actions: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    grades: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    trap: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    quality_buckets: Counter[str] = Counter()
    upgrades = 0
    downgrades = 0
    validations = 0
    refuse_value_candidates = 0
    blocked = 0
    positive_edge = 0
    negative_edge = 0
    no_market = 0
    edge_values: List[float] = []
    quality_values: List[float] = []
    samples: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    valid_matches = [m for m in matches or [] if isinstance(m, dict)]
    for match in valid_matches:
        v4 = match.get("v4Lab") if isinstance(match.get("v4Lab"), dict) else {}
        action = _s(v4.get("v4Action") or v4.get("decision") or "UNKNOWN")
        high_decision = _s(v4.get("v4Decision") or "UNKNOWN")
        grade = _s(v4.get("grade") or "?")
        category = _s(v4.get("v3Category") or "UNKNOWN")
        actions[action] += 1
        decisions[high_decision] += 1
        grades[grade] += 1
        categories[category] += 1
        trap[_s(v4.get("favoriteTrapRisk") or "unknown")] += 1
        for code in v4.get("flagCodes") or []:
            flag_counts[_s(code)] += 1

        if high_decision == "BLOCKED":
            blocked += 1
            if len(samples["blocked"]) < 10:
                samples["blocked"].append(_sample(match, v4))
        if action.startswith("UPGRADE_PROCHE"):
            upgrades += 1
            if len(samples["upgradedProche"]) < 10:
                samples["upgradedProche"].append(_sample(match, v4))
        if action.startswith("DOWNGRADE_PREMIUM"):
            downgrades += 1
            if len(samples["downgradedPremium"]) < 10:
                samples["downgradedPremium"].append(_sample(match, v4))
        if action.startswith("VALIDATE_PREMIUM"):
            validations += 1
            if len(samples["validatedPremium"]) < 10:
                samples["validatedPremium"].append(_sample(match, v4))
        if action.startswith("REFUSE_VALUE"):
            refuse_value_candidates += 1
            if len(samples["refuseValueCandidates"]) < 10:
                samples["refuseValueCandidates"].append(_sample(match, v4))

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

        if high_decision == "BET" and len(samples["bet"]) < 10:
            samples["bet"].append(_sample(match, v4))
        elif high_decision == "WATCH" and len(samples["watch"]) < 10:
            samples["watch"].append(_sample(match, v4))
        elif high_decision == "NO_BET" and len(samples["noBet"]) < 10:
            samples["noBet"].append(_sample(match, v4))

    avg_edge = round(sum(edge_values) / len(edge_values), 4) if edge_values else None
    avg_quality = round(sum(quality_values) / len(quality_values), 4) if quality_values else None
    return {
        "version": V4_VERSION,
        "mode": "full_candidate_passive_no_official_mutation",
        "scope": "all_categories",
        "totalMatches": len(valid_matches),
        "v4Bet": int(decisions.get("BET", 0)),
        "v4Watch": int(decisions.get("WATCH", 0)),
        "v4NoBet": int(decisions.get("NO_BET", 0)),
        "v4Blocked": blocked,
        "premiumValidated": validations,
        "premiumDowngraded": downgrades,
        "procheUpgraded": upgrades,
        "refuseValueCandidates": refuse_value_candidates,
        "positiveEdge": positive_edge,
        "negativeEdge": negative_edge,
        "noMarket": no_market,
        "averageEdge": avg_edge,
        "averageDataQuality": avg_quality,
        "v4Decisions": dict(decisions),
        "v4Actions": dict(actions),
        "grades": dict(grades),
        "v3Categories": dict(categories),
        "favoriteTrapRisk": dict(trap),
        "qualityBuckets": dict(quality_buckets),
        "topFlags": dict(flag_counts.most_common(20)),
        "samples": dict(samples),
        "policy": "V4 Full Candidate analyse Premium, Proche, Refusé et Non analysé séparément. Elle reste passive et ne remplace pas STEP56 tant qu'elle n'a pas gagné sur données futures.",
    }


def attach_v4_lab_to_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    matches = payload.get("matches")
    if not isinstance(matches, list):
        payload["v4Summary"] = build_v4_summary([])
        payload.setdefault("daily", {})
        payload["daily"]["v4Lab"] = payload["v4Summary"]
        return payload
    for match in matches:
        if not isinstance(match, dict):
            continue
        v4 = analyze_match_v4(match)
        match["v4Lab"] = v4
        match["v4ShortReason"] = v4.get("shortReason") or v4.get("reason") or v4.get("v4Action") or v4.get("decision")
    summary = build_v4_summary(matches)
    payload["v4Summary"] = summary
    payload.setdefault("daily", {})
    payload["daily"]["v4Lab"] = {
        "status": "enabled",
        "version": V4_VERSION,
        "mode": "full_candidate_passive_parallel_lab",
        "scope": "all_categories",
        "officialMutation": False,
        "summary": summary,
    }
    return payload


def status_payload() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": V4_VERSION,
        "mode": "full_candidate_passive_parallel_lab",
        "scope": "all_categories",
        "officialMutation": False,
        "mainQuestion": "Chaque catégorie v3 cache-t-elle une décision V4 différente : valider, downgrader, upgrader, surveiller, confirmer le refus ou bloquer ?",
        "categoryLogic": {
            "PREMIUM": ["VALIDATE_PREMIUM_VALUE", "WATCH_PREMIUM_BORDERLINE", "DOWNGRADE_PREMIUM_NEGATIVE_EDGE", "DOWNGRADE_PREMIUM_DATA_RISK"],
            "PROCHE": ["UPGRADE_PROCHE_TO_VALUE", "WATCH_PROCHE_VALUE", "NO_BET_PROCHE_NO_EDGE", "NO_BET_PROCHE_NEGATIVE_EDGE"],
            "REFUSE": ["REFUSE_VALUE_STRONG", "REFUSE_VALUE_WATCH", "REFUSE_CONFIRMED_NO_VALUE", "REFUSE_DATA_TOO_WEAK"],
            "NOT_ANALYZED": ["BLOCKED_ATP_POINTS_MISSING", "BLOCKED_PLAYER_EXCLUDED", "BLOCKED_NOT_ANALYZED"],
        },
        "outputs": ["v4Decision", "v4Action", "grade", "modelProbabilityCalibrated", "marketProbability", "edge", "valueScore", "dataQualityScore", "favoriteTrapRisk", "stakeAdvice"],
        "grades": {"A+": "BET très fort", "A": "BET candidat", "B": "WATCH", "C": "NO_BET normal", "D": "NO_BET risque/edge/données", "X": "bloqué/non analysable"},
        "thresholds": {
            "premiumEdgeBet": PREMIUM_EDGE_BET,
            "premiumEdgeStrong": PREMIUM_EDGE_STRONG,
            "premiumEdgeWatch": PREMIUM_EDGE_WATCH,
            "procheEdgeUpgrade": PROCHE_EDGE_UPGRADE,
            "procheEdgeStrong": PROCHE_EDGE_STRONG,
            "procheEdgeWatch": PROCHE_EDGE_WATCH,
            "refuseEdgeStrong": REFUSE_EDGE_STRONG,
            "refuseEdgeWatch": REFUSE_EDGE_WATCH,
            "betMinDataQuality": MIN_DATA_QUALITY_BET,
            "watchMinDataQuality": MIN_DATA_QUALITY_WATCH,
            "priorProbability": PRIOR_PROBABILITY,
        },
        "policy": "La V4 Full Candidate est plus autonome que STEP64 : elle ne contrôle pas seulement les Premium, elle cherche aussi les upgrades Proche et les Refuse Value, mais sans muter la v3 officielle.",
    }
