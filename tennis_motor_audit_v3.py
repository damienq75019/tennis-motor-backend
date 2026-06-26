from __future__ import annotations

"""
Tennis Motor — STEP63 Audit v3 passif

But : ajouter une couche d'audit explicable sans modifier le moteur STEP56/v3.
- Ne change pas premiumPct.
- Ne change pas decision.
- Ne change pas la catégorie officielle.
- Ajoute seulement match["audit"], match["auditShortReason"] et payload["auditSummary"].

Compatible avec le backend STEP62 fourni :
- STEP56 officiel actif.
- Refuse Value persistant.
- Veto terre battue en audit only.
- Points ATP manquants = non analysé.
- Jannik Sinner exclu.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple

AUDIT_VERSION = "STEP63_AUDIT_V3_PASSIVE_2026-06-26"

# Seuils réellement utilisés par app.py : Premium strictement > 80, Proche de 75 à 80 inclus côté bas.
PREMIUM_THRESHOLD_PCT = 80.0
PROCHE_THRESHOLD_PCT = 75.0

MIN_HISTORY_MATCHES = 20
MIN_SURFACE_HISTORY_MATCHES = 3
MIN_FORM5_MATCHES = 5
MIN_FORM10_MATCHES = 10

EXCLUDED_ANALYSIS_PLAYERS = {"jannik sinner"}

PROBABILITY_FIELDS = [
    "premium",
    "premiumPct",
    "pSwe",
    "pSWE",
    "pAtp",
    "pATP",
    "pRank",
    "pForm5",
    "pForm10",
    "pSurfaceForm5",
    "pDominance",
    "playerAForm5",
    "playerBForm5",
    "step56Confidence",
]

RESULT_STATUS_FIELDS = [
    "result",
    "resultStatus",
    "matchStatus",
    "statusText",
    "status",
    "winnerStatus",
    "score",
]

VOID_TERMS = {
    "void",
    "refunded",
    "refund",
    "cancelled",
    "canceled",
    "abandoned",
    "retired",
    "retirement",
    "walkover",
    "withdrawn",
    "withdrawal",
    "forfeit",
    "w/o",
    "wo",
}


@dataclass
class AuditFlag:
    code: str
    severity: str
    message: str
    field: Optional[str] = None
    value: Any = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.field is not None:
            out["field"] = self.field
        if self.value is not None:
            out["value"] = self.value
        return out


@dataclass
class MatchAuditBuilder:
    flags: List[AuditFlag] = field(default_factory=list)
    checks: Dict[str, str] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)

    def add(self, code: str, severity: str, message: str, field: Optional[str] = None, value: Any = None) -> None:
        self.flags.append(AuditFlag(code=code, severity=severity, message=message, field=field, value=value))

    def set_check(self, name: str, status: str) -> None:
        current = self.checks.get(name)
        order = {"not_checked": 0, "ok": 1, "warning": 2, "critical": 3}
        if current is None or order.get(status, 0) >= order.get(current, 0):
            self.checks[name] = status

    def severity(self) -> str:
        if any(flag.severity == "critical" for flag in self.flags):
            return "critical"
        if any(flag.severity == "medium" for flag in self.flags):
            return "medium"
        if any(flag.severity == "low" for flag in self.flags):
            return "low"
        return "clean"

    def status(self) -> str:
        sev = self.severity()
        if sev == "critical":
            return "blocked"
        if sev in {"medium", "low"}:
            return "warning"
        return "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": AUDIT_VERSION,
            "status": self.status(),
            "severity": self.severity(),
            "flags": [flag.to_dict() for flag in self.flags],
            "flagCodes": [flag.code for flag in self.flags],
            "checks": self.checks,
            "details": self.details,
        }


def _s(value: Any) -> str:
    return str(value or "").strip()


def _canon(value: Any) -> str:
    text = _s(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _first_present(match: Dict[str, Any], keys: Iterable[str]) -> Tuple[Optional[str], Any]:
    for key in keys:
        if key in match and match.get(key) not in (None, ""):
            return key, match.get(key)
    return None, None


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        raw = str(value).replace("%", "").replace(",", ".").strip()
        if not raw:
            return default
        return float(raw)
    except Exception:
        return default


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _s(value).lower() in {"1", "true", "yes", "oui", "y", "on"}


def _pct_from_any(value: Any) -> Optional[float]:
    val = _to_float(value, None)
    if val is None:
        return None
    if 0.0 <= val <= 1.0:
        return val * 100.0
    return val


def _probability_0_1(value: Any) -> Optional[float]:
    val = _to_float(value, None)
    if val is None:
        return None
    if 1.0 < val <= 100.0:
        return val / 100.0
    return val


def _player_names(match: Dict[str, Any]) -> Tuple[str, str]:
    _, player_a = _first_present(match, ["playerA", "player_a", "sourcePlayerA", "apiTennisRawFirstPlayer"])
    _, player_b = _first_present(match, ["playerB", "player_b", "sourcePlayerB", "apiTennisRawSecondPlayer"])
    return _s(player_a), _s(player_b)


def _premium_pct(match: Dict[str, Any]) -> Optional[float]:
    _, value = _first_present(match, ["premiumPct", "step56ConfidencePct", "premium", "step56Confidence"])
    return _pct_from_any(value)


def _is_not_analyzed(match: Dict[str, Any]) -> bool:
    decision = _s(match.get("decision")).lower()
    status = _s(match.get("analysisStatus")).lower()
    return bool(match.get("nonAnalyzable")) or status == "not_analyzed" or "non analys" in decision


def _expected_category(match: Dict[str, Any]) -> str:
    if _is_not_analyzed(match):
        return "NOT_ANALYZED"
    pct = _premium_pct(match)
    if pct is None:
        return "UNKNOWN"
    if pct > PREMIUM_THRESHOLD_PCT:
        return "PREMIUM"
    if pct >= PROCHE_THRESHOLD_PCT:
        return "PROCHE"
    return "REFUSE"


def _category_from_step56(value: Any) -> Optional[str]:
    raw = _s(value).upper().replace(" ", "_").replace("-", "_")
    if raw in {"STEP56_ELITE", "STEP56_PREMIUM", "ELITE", "PREMIUM"}:
        return "PREMIUM"
    if raw in {"STEP56_PROCHE", "PROCHE", "PROCHES"}:
        return "PROCHE"
    if raw in {"STEP56_REFUSE", "REFUSE", "REFUSED", "REFUS", "REFUSÉ", "REFUSES", "REFUSÉS"}:
        return "REFUSE"
    return None


def _declared_category(match: Dict[str, Any], expected: str) -> Optional[str]:
    if _is_not_analyzed(match):
        return "NOT_ANALYZED"

    # Champ le plus fiable dans ton backend STEP62.
    cat = _category_from_step56(match.get("step56OfficialCategory"))
    if cat:
        return cat

    # Refuse Value est une sous-catégorie financière des refusés, pas une prédiction différente.
    if _to_bool(match.get("refuseValueApplies")) or _s(match.get("refuseValueCategoryBase")).upper() == "REFUSE":
        return "REFUSE"

    decision = _s(match.get("decision")).lower()
    if "non analys" in decision:
        return "NOT_ANALYZED"
    if "pas jouable" in decision:
        # Pas jouable regroupe Proche et Refusé dans certains payloads : on garde la catégorie attendue par score.
        return expected if expected in {"PROCHE", "REFUSE"} else "REFUSE"
    if "jouable" in decision:
        return "PREMIUM"
    if "proche" in decision:
        return "PROCHE"
    if "refus" in decision:
        return "REFUSE"

    # Ne pas utiliser match["status"] : dans ton backend il désigne souvent le statut sportif/API du match.
    return None


def _match_like(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    keys = set(obj.keys())
    player_markers = {"playerA", "playerB", "player_a", "player_b", "sourcePlayerA", "sourcePlayerB"}
    signal_markers = {
        "premiumPct",
        "premium",
        "step56Confidence",
        "step56OfficialCategory",
        "playerAPoints",
        "playerBPoints",
        "player_a_points",
        "player_b_points",
        "analysisStatus",
        "nonAnalyzable",
    }
    return bool(keys & player_markers) and bool(keys & signal_markers)


def _audit_players(match: Dict[str, Any], audit: MatchAuditBuilder) -> None:
    player_a, player_b = _player_names(match)
    audit.details["players"] = {"playerA": player_a, "playerB": player_b}
    normalized = {_canon(player_a), _canon(player_b)}
    for excluded in EXCLUDED_ANALYSIS_PLAYERS:
        if excluded in normalized:
            audit.add("EXCLUDED_PLAYER", "critical", f"Joueur exclu de l'analyse : {excluded}.")
            audit.set_check("excluded_players", "critical")
            return
    audit.set_check("excluded_players", "ok")


def _audit_points(match: Dict[str, Any], audit: MatchAuditBuilder) -> None:
    a_key, raw_a = _first_present(match, ["playerAPoints", "player_a_points", "pointsA", "rankPointsA"])
    b_key, raw_b = _first_present(match, ["playerBPoints", "player_b_points", "pointsB", "rankPointsB"])
    points_a = _to_int(raw_a, None)
    points_b = _to_int(raw_b, None)
    audit.details["atpPoints"] = {"playerA": points_a, "playerB": points_b}

    if points_a is None or points_a <= 0:
        audit.add("ATP_POINTS_MISSING", "critical", "Points ATP joueur A manquants ou à 0 : le match ne doit pas être analysé.", a_key or "playerAPoints", raw_a)
        audit.set_check("atp_points", "critical")
    if points_b is None or points_b <= 0:
        audit.add("ATP_POINTS_MISSING", "critical", "Points ATP joueur B manquants ou à 0 : le match ne doit pas être analysé.", b_key or "playerBPoints", raw_b)
        audit.set_check("atp_points", "critical")
    if audit.checks.get("atp_points") != "critical":
        audit.set_check("atp_points", "ok")


def _audit_probabilities(match: Dict[str, Any], audit: MatchAuditBuilder) -> None:
    invalid: List[Dict[str, Any]] = []
    seen = False
    for field_name in PROBABILITY_FIELDS:
        if field_name not in match:
            continue
        seen = True
        p = _probability_0_1(match.get(field_name))
        if p is None or p < 0.0 or p > 1.0:
            invalid.append({"field": field_name, "value": match.get(field_name)})
            audit.add("PROBABILITY_FIELD_INVALID", "critical", "Champ de probabilité invalide hors intervalle 0-1 / 0-100%.", field_name, match.get(field_name))
    audit.details["invalidProbabilityFields"] = invalid
    if invalid:
        audit.set_check("probability_fields", "critical")
    elif seen:
        audit.set_check("probability_fields", "ok")
    else:
        audit.add("NO_PROBABILITY_SIGNAL", "medium", "Aucun signal de probabilité reconnu dans ce match.")
        audit.set_check("probability_fields", "warning")


def _decision_explanation(match: Dict[str, Any], expected: str, pct: Optional[float]) -> Dict[str, Any]:
    reason_code = _s(match.get("analysisBlockedReason"))
    player_a, player_b = _player_names(match)
    points = {
        "playerA": _to_int(match.get("playerAPoints"), None),
        "playerB": _to_int(match.get("playerBPoints"), None),
    }

    if expected == "NOT_ANALYZED":
        if reason_code == "points_atp_missing":
            return {
                "reason": "NOT_ANALYZED_ATP_POINTS_MISSING",
                "message": f"Non analysé : points ATP manquants ou à 0. {player_a}={points['playerA']}, {player_b}={points['playerB']}.",
            }
        if reason_code == "placeholder_player":
            return {
                "reason": "NOT_ANALYZED_PLACEHOLDER_PLAYER",
                "message": "Non analysé : joueur non connu / placeholder API-Tennis.",
            }
        return {
            "reason": reason_code or "NOT_ANALYZED",
            "message": "Non analysé : match bloqué avant calcul moteur.",
        }

    if pct is None:
        return {
            "reason": "PREMIUM_PCT_MISSING",
            "message": "Impossible d'expliquer le passage : premiumPct absent.",
        }
    if expected == "PREMIUM":
        return {
            "reason": "PREMIUM_PCT_ABOVE_80",
            "message": f"Passe Premium : score moteur {pct:.1f}% strictement au-dessus du seuil 80%.",
        }
    if expected == "PROCHE":
        return {
            "reason": "PREMIUM_PCT_BETWEEN_75_AND_80",
            "message": f"Ne passe pas Premium : score moteur {pct:.1f}%, entre Proche 75% et Premium strict > 80%.",
        }
    if expected == "REFUSE":
        return {
            "reason": "PREMIUM_PCT_BELOW_75",
            "message": f"Refusé : score moteur {pct:.1f}%, sous le seuil Proche 75%.",
        }
    return {
        "reason": "UNKNOWN_CATEGORY",
        "message": "Catégorie impossible à déterminer.",
    }


def _audit_category(match: Dict[str, Any], audit: MatchAuditBuilder) -> None:
    pct = _premium_pct(match)
    expected = _expected_category(match)
    declared = _declared_category(match, expected)

    audit.details["premiumPct"] = pct
    audit.details["expectedCategoryByScore"] = expected
    audit.details["declaredCategory"] = declared
    audit.details["thresholds"] = {
        "prochePct": PROCHE_THRESHOLD_PCT,
        "premiumPctStrictlyGreaterThan": PREMIUM_THRESHOLD_PCT,
    }
    audit.details["decisionExplanation"] = _decision_explanation(match, expected, pct)
    audit.details["reasonForNotPassing"] = audit.details["decisionExplanation"] if expected != "PREMIUM" else None

    if pct is None and expected != "NOT_ANALYZED":
        audit.add("PREMIUM_PCT_MISSING", "medium", "premiumPct absent : impossible de vérifier les seuils Premium/Proche/Refusé.")
        audit.set_check("threshold_logic", "warning")
    else:
        audit.set_check("threshold_logic", "ok")

    if declared is None:
        audit.add("DECLARED_CATEGORY_MISSING", "low", "Catégorie officielle absente : l'audit explique seulement par le score moteur.")
        audit.set_check("category_consistency", "warning")
        return

    if expected != "UNKNOWN" and declared != expected:
        audit.add(
            "CATEGORY_MISMATCH",
            "critical",
            f"Catégorie déclarée {declared} incohérente avec la catégorie attendue {expected}.",
            "step56OfficialCategory/decision",
            declared,
        )
        audit.set_check("category_consistency", "critical")
    else:
        audit.set_check("category_consistency", "ok")


def _audit_history_depth(match: Dict[str, Any], audit: MatchAuditBuilder) -> None:
    keys = [
        "playerAHistoryMatches",
        "playerBHistoryMatches",
        "playerASurfaceHistoryMatches",
        "playerBSurfaceHistoryMatches",
        "playerAForm5Matches",
        "playerBForm5Matches",
        "playerAForm10Matches",
        "playerBForm10Matches",
        "playerASurfaceForm5Matches",
        "playerBSurfaceForm5Matches",
        "playerADominanceMatches",
        "playerBDominanceMatches",
    ]
    depth = {key: _to_int(match.get(key), None) for key in keys if key in match}
    audit.details["sampleDepth"] = depth

    # Pas critique : le moteur peut calculer avec shrinkage. C'est un warning explicatif.
    for key in ["playerAHistoryMatches", "playerBHistoryMatches"]:
        if key in match:
            n = _to_int(match.get(key), None)
            if n is None:
                audit.add("HISTORY_SAMPLE_INVALID", "medium", "Nombre de matchs historique invalide.", key, match.get(key))
                audit.set_check("history_depth", "warning")
            elif n < MIN_HISTORY_MATCHES:
                audit.add("HISTORY_SAMPLE_TOO_LOW", "medium", f"Historique global faible : {n} matchs, minimum conseillé {MIN_HISTORY_MATCHES}.", key, n)
                audit.set_check("history_depth", "warning")
    if "history_depth" not in audit.checks:
        audit.set_check("history_depth", "ok")

    for key in ["playerASurfaceHistoryMatches", "playerBSurfaceHistoryMatches"]:
        if key in match:
            n = _to_int(match.get(key), None)
            if n is None:
                audit.add("SURFACE_SAMPLE_INVALID", "medium", "Nombre de matchs surface invalide.", key, match.get(key))
                audit.set_check("surface_depth", "warning")
            elif n < MIN_SURFACE_HISTORY_MATCHES:
                audit.add("SURFACE_SAMPLE_TOO_LOW", "medium", f"Historique surface faible : {n} matchs, minimum conseillé {MIN_SURFACE_HISTORY_MATCHES}.", key, n)
                audit.set_check("surface_depth", "warning")
    if "surface_depth" not in audit.checks:
        audit.set_check("surface_depth", "ok")

    for key in ["playerAForm5Matches", "playerBForm5Matches", "playerASurfaceForm5Matches", "playerBSurfaceForm5Matches"]:
        if key in match:
            n = _to_int(match.get(key), None)
            if n is not None and n < MIN_FORM5_MATCHES:
                audit.add("FORM5_SAMPLE_INCOMPLETE", "low", f"Form5 / SurfaceForm5 incomplète : {n}/{MIN_FORM5_MATCHES}.", key, n)
                audit.set_check("form_depth", "warning")
    for key in ["playerAForm10Matches", "playerBForm10Matches"]:
        if key in match:
            n = _to_int(match.get(key), None)
            if n is not None and n < MIN_FORM10_MATCHES:
                audit.add("FORM10_SAMPLE_INCOMPLETE", "low", f"Form10 incomplète : {n}/{MIN_FORM10_MATCHES}.", key, n)
                audit.set_check("form_depth", "warning")
    if "form_depth" not in audit.checks:
        audit.set_check("form_depth", "ok")


def _predicted_odd(match: Dict[str, Any]) -> Tuple[Optional[str], Optional[float]]:
    # Dans ton backend, après orientation STEP56, playerA est le pick affiché.
    keys = ["oddPredicted", "refuseValueOdd", "oddA", "playerAOdd", "player_a_odd", "coteA", "odds", "cote"]
    key, value = _first_present(match, keys)
    odd = _to_float(value, None)
    if odd is not None and odd > 1.0:
        return key, odd
    return key, odd


def _audit_market(match: Dict[str, Any], audit: MatchAuditBuilder, include_market_check: bool) -> None:
    if not include_market_check:
        audit.set_check("market_value", "not_checked")
        return

    key, odd = _predicted_odd(match)
    pct = _premium_pct(match)
    if odd is None or odd <= 1.0:
        audit.details["market"] = {"odd": odd, "impliedProbabilityPct": None, "edgePct": None, "state": "ODDS_MISSING_OR_INVALID"}
        audit.add("ODDS_MISSING", "low", "Cote absente ou inexploitable : comparaison marché impossible.", key or "oddPredicted", odd)
        audit.set_check("market_value", "warning")
        return

    implied_pct = 100.0 / odd
    edge_pct = None if pct is None else pct - implied_pct
    audit.details["market"] = {
        "odd": round(odd, 3),
        "impliedProbabilityPct": round(implied_pct, 2),
        "edgePct": round(edge_pct, 2) if edge_pct is not None else None,
        "state": "OK" if edge_pct is not None else "NO_MODEL_PCT",
    }
    audit.set_check("market_value", "ok" if edge_pct is not None else "warning")


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _s(value)
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _audit_timestamps(match: Dict[str, Any], audit: MatchAuditBuilder, require_timestamps: bool) -> None:
    fields = ["startTime", "matchStartTime", "scheduledAt", "rankingSnapshotDate", "historySnapshotDate", "resultSyncTime"]
    found = {key: match.get(key) for key in fields if match.get(key) not in (None, "")}
    audit.details["timestamps"] = found

    if not require_timestamps:
        audit.set_check("data_leakage_risk", "not_checked")
        return

    start = _parse_dt(match.get("matchStartTime") or match.get("startTime") or match.get("scheduledAt"))
    ranking_snapshot = _parse_dt(match.get("rankingSnapshotDate"))
    history_snapshot = _parse_dt(match.get("historySnapshotDate"))

    if start is None:
        audit.add("MATCH_START_TIMESTAMP_MISSING", "medium", "Heure de début absente : audit anti-fuite incomplet.")
        audit.set_check("data_leakage_risk", "warning")
    if ranking_snapshot is None:
        audit.add("RANKING_SNAPSHOT_TIMESTAMP_MISSING", "medium", "Snapshot ranking absent : audit anti-fuite incomplet.")
        audit.set_check("data_leakage_risk", "warning")
    if history_snapshot is None:
        audit.add("HISTORY_SNAPSHOT_TIMESTAMP_MISSING", "medium", "Snapshot historique absent : audit anti-fuite incomplet.")
        audit.set_check("data_leakage_risk", "warning")

    if start and ranking_snapshot and ranking_snapshot > start:
        audit.add("RANKING_SNAPSHOT_AFTER_MATCH_START", "critical", "Snapshot ranking postérieur au début du match : risque de fuite de données.")
        audit.set_check("data_leakage_risk", "critical")
    if start and history_snapshot and history_snapshot > start:
        audit.add("HISTORY_SNAPSHOT_AFTER_MATCH_START", "critical", "Snapshot historique postérieur au début du match : risque de fuite de données.")
        audit.set_check("data_leakage_risk", "critical")

    if "data_leakage_risk" not in audit.checks:
        audit.set_check("data_leakage_risk", "ok")


def _audit_void_policy(match: Dict[str, Any], audit: MatchAuditBuilder) -> None:
    texts = []
    for key in RESULT_STATUS_FIELDS:
        if match.get(key) not in (None, ""):
            texts.append(_s(match.get(key)).lower())
    joined = " | ".join(texts)
    is_void_candidate = any(term in joined for term in VOID_TERMS)
    audit.details["voidPolicy"] = {"isVoidCandidate": is_void_candidate, "statusText": joined}

    if not is_void_candidate:
        audit.set_check("retired_void_policy", "ok")
        return

    result = _s(match.get("result")).lower()
    profit = _to_float(match.get("profit"), None)
    gain = _to_float(match.get("gain"), None)
    loss = _to_float(match.get("loss"), None)
    result_ok = result in {"void", "refunded", "refund", "cancelled", "canceled", "abandoned", "retired", "walkover", "withdrawn", ""}
    money_ok = (profit in (None, 0.0)) and (gain in (None, 0.0)) and (loss in (None, 0.0))
    if result_ok and money_ok:
        audit.set_check("retired_void_policy", "ok")
    else:
        audit.add("RETIRED_NOT_VOID", "critical", "Abandon/retired détecté mais pas clairement traité en void/remboursé à 0 gain/0 perte.")
        audit.set_check("retired_void_policy", "critical")


def _short_reason(audit_dict: Dict[str, Any]) -> str:
    details = audit_dict.get("details", {}) if isinstance(audit_dict, dict) else {}
    explanation = details.get("decisionExplanation", {}) if isinstance(details, dict) else {}
    base = _s(explanation.get("message"))
    if not base:
        status = _s(audit_dict.get("status"))
        if status == "blocked":
            base = "Audit bloqué : anomalie critique."
        elif status == "warning":
            base = "Audit warning : match analysé avec prudence."
        else:
            base = "Audit clean : aucune anomalie détectée."
    flags = audit_dict.get("flagCodes", []) if isinstance(audit_dict, dict) else []
    if flags:
        return f"{base} Flags: {', '.join([_s(x) for x in flags[:3]])}."
    return base


def audit_match(match: Dict[str, Any], require_timestamps: bool = False, include_market_check: bool = True) -> Dict[str, Any]:
    audit = MatchAuditBuilder()
    _audit_players(match, audit)
    _audit_points(match, audit)
    _audit_probabilities(match, audit)
    _audit_category(match, audit)
    _audit_history_depth(match, audit)
    _audit_market(match, audit, include_market_check=include_market_check)
    _audit_timestamps(match, audit, require_timestamps=require_timestamps)
    _audit_void_policy(match, audit)
    return audit.to_dict()


def attach_audit_to_match(match: Dict[str, Any], require_timestamps: bool = False, include_market_check: bool = True) -> Dict[str, Any]:
    out = deepcopy(match)
    audit_dict = audit_match(out, require_timestamps=require_timestamps, include_market_check=include_market_check)
    out["audit"] = audit_dict
    out["auditShortReason"] = _short_reason(audit_dict)
    return out


def _audit_recursive(obj: Any, require_timestamps: bool, include_market_check: bool) -> Tuple[Any, int]:
    if isinstance(obj, list):
        out_list: List[Any] = []
        count = 0
        for item in obj:
            audited_item, child_count = _audit_recursive(item, require_timestamps, include_market_check)
            out_list.append(audited_item)
            count += child_count
        return out_list, count

    if isinstance(obj, dict):
        if _match_like(obj):
            return attach_audit_to_match(obj, require_timestamps=require_timestamps, include_market_check=include_market_check), 1
        out_dict: Dict[str, Any] = {}
        count = 0
        for key, value in obj.items():
            if key == "auditSummary":
                continue
            audited_value, child_count = _audit_recursive(value, require_timestamps, include_market_check)
            out_dict[key] = audited_value
            count += child_count
        return out_dict, count

    return obj, 0


def _iter_audited_matches(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        if _match_like(obj) and isinstance(obj.get("audit"), dict):
            yield obj
        for value in obj.values():
            yield from _iter_audited_matches(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_audited_matches(item)


def build_audit_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    matches = list(_iter_audited_matches(payload))
    summary: Dict[str, Any] = {
        "version": AUDIT_VERSION,
        "totalAuditedMatches": len(matches),
        "clean": 0,
        "warnings": 0,
        "critical": 0,
        "blocked": 0,
        "byExpectedCategory": {},
        "byDeclaredCategory": {},
        "topFlags": {},
        "premiumClean": 0,
        "premiumWarning": 0,
        "premiumCritical": 0,
        "procheClean": 0,
        "procheWarning": 0,
        "procheCritical": 0,
        "refuseClean": 0,
        "refuseWarning": 0,
        "refuseCritical": 0,
        "notAnalyzed": 0,
        "explainabilityReady": True,
        "passiveMode": True,
        "decisionsModified": False,
    }

    for match in matches:
        audit = match.get("audit") or {}
        severity = _s(audit.get("severity")) or "clean"
        status = _s(audit.get("status")) or "ok"
        details = audit.get("details") if isinstance(audit.get("details"), dict) else {}
        expected = _s(details.get("expectedCategoryByScore")) or "UNKNOWN"
        declared = _s(details.get("declaredCategory")) or "UNKNOWN"

        if severity == "clean":
            summary["clean"] += 1
        elif severity == "critical":
            summary["critical"] += 1
        else:
            summary["warnings"] += 1
        if status == "blocked":
            summary["blocked"] += 1

        summary["byExpectedCategory"][expected] = summary["byExpectedCategory"].get(expected, 0) + 1
        summary["byDeclaredCategory"][declared] = summary["byDeclaredCategory"].get(declared, 0) + 1

        for code in audit.get("flagCodes", []) or []:
            code_s = _s(code)
            if code_s:
                summary["topFlags"][code_s] = summary["topFlags"].get(code_s, 0) + 1

        if expected == "PREMIUM":
            if severity == "clean":
                summary["premiumClean"] += 1
            elif severity == "critical":
                summary["premiumCritical"] += 1
            else:
                summary["premiumWarning"] += 1
        elif expected == "PROCHE":
            if severity == "clean":
                summary["procheClean"] += 1
            elif severity == "critical":
                summary["procheCritical"] += 1
            else:
                summary["procheWarning"] += 1
        elif expected == "REFUSE":
            if severity == "clean":
                summary["refuseClean"] += 1
            elif severity == "critical":
                summary["refuseCritical"] += 1
            else:
                summary["refuseWarning"] += 1
        elif expected == "NOT_ANALYZED":
            summary["notAnalyzed"] += 1

    summary["topFlags"] = dict(sorted(summary["topFlags"].items(), key=lambda kv: kv[1], reverse=True))
    return summary


def attach_audit_to_payload(payload: Dict[str, Any], require_timestamps: bool = False, include_market_check: bool = True) -> Dict[str, Any]:
    audited_payload, _ = _audit_recursive(payload, require_timestamps=require_timestamps, include_market_check=include_market_check)
    if isinstance(audited_payload, dict):
        audited_payload["auditSummary"] = build_audit_summary(audited_payload)
        audited_payload.setdefault("daily", {})
        if isinstance(audited_payload.get("daily"), dict):
            audited_payload["daily"]["auditV3"] = {
                "status": "enabled",
                "version": AUDIT_VERSION,
                "mode": "passive_no_decision_mutation",
                "policy": "Explique pourquoi un match passe ou ne passe pas, sans modifier STEP56/v3.",
            }
    return audited_payload


def short_reason_from_audit(audit: Dict[str, Any]) -> str:
    return _short_reason(audit)
