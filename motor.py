from dataclasses import asdict, dataclass
from typing import Any, Dict, List

from shared_core_v2pro import (
    HISTORY_YEARS,
    apply_clay_veto,
    atp_points_probability,
    build_state,
    build_summary,
    clamp_probability,
    form_probability,
    get_dominance5_rate,
    get_form5_rate,
    get_form10_rate,
    get_latest_or_estimated_rank,
    get_surface_form5_rate,
    get_surface_weighted_elo,
    logit,
    normalize_bool,
    normalize_surface,
    rank_probability,
    safe_int,
    sigmoid,
    validate_match_input,
)

_STATE = None


@dataclass
class MatchPrediction:
    playerA: str
    playerB: str
    surface: str
    playerAPoints: int
    playerBPoints: int
    playerARank: int
    playerBRank: int
    playerAForm5: float
    playerBForm5: float
    sweA: float
    sweB: float
    pSwe: float
    pAtp: float
    pRank: float
    pForm5: float
    pForm10: float
    pSurfaceForm5: float
    pDominance: float
    premium: float
    premiumPct: float
    veto: str
    decision: str


def get_state() -> Dict[str, Any]:
    global _STATE
    if _STATE is None:
        _STATE = build_state()
    return _STATE


def shrink_prob(p: float, strength: float = 0.18) -> float:
    return 0.5 + (p - 0.5) * (1.0 - strength)


def calculate_match_prediction(match: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    errors = validate_match_input(match)
    if errors:
        return {
            "playerA": match.get("playerA", ""),
            "playerB": match.get("playerB", ""),
            "surface": match.get("surface", ""),
            "error": " | ".join(errors),
        }

    player_a = match["playerA"].strip()
    player_b = match["playerB"].strip()
    surface = normalize_surface(match["surface"])

    player_a_points = safe_int(match.get("playerAPoints", 0))
    player_b_points = safe_int(match.get("playerBPoints", 0))
    player_b_is_qualifier = normalize_bool(match.get("player_b_is_qualifier", False))
    player_b_tournament_wins = safe_int(match.get("player_b_tournament_wins", 0))

    swe_a = get_surface_weighted_elo(player_a, surface, state, shrink=26.0)
    swe_b = get_surface_weighted_elo(player_b, surface, state, shrink=26.0)
    p_swe = 1.0 / (1.0 + (10.0 ** (-(swe_a - swe_b) / 400.0)))
    p_swe = shrink_prob(p_swe, 0.08)

    p_atp = atp_points_probability(player_a_points, player_b_points, scale=1850.0)
    p_atp = shrink_prob(p_atp, 0.10)

    player_a_rank = get_latest_or_estimated_rank(player_a, player_a_points, state)
    player_b_rank = get_latest_or_estimated_rank(player_b, player_b_points, state)
    p_rank = rank_probability(player_a_rank, player_b_rank, scale=34.0)
    p_rank = shrink_prob(p_rank, 0.12)

    form5_a = get_form5_rate(player_a, state)
    form5_b = get_form5_rate(player_b, state)
    p_form5 = form_probability(form5_a, form5_b, scale=0.17)
    p_form5 = shrink_prob(p_form5, 0.20)

    form10_a = get_form10_rate(player_a, state)
    form10_b = get_form10_rate(player_b, state)
    p_form10 = form_probability(form10_a, form10_b, scale=0.16)
    p_form10 = shrink_prob(p_form10, 0.22)

    sform_a = get_surface_form5_rate(player_a, surface, state)
    sform_b = get_surface_form5_rate(player_b, surface, state)
    p_surface_form5 = form_probability(sform_a, sform_b, scale=0.18)
    p_surface_form5 = shrink_prob(p_surface_form5, 0.18)

    dom_a = get_dominance5_rate(player_a, state)
    dom_b = get_dominance5_rate(player_b, state)
    p_dom = sigmoid((dom_a - dom_b) / 0.075)
    p_dom = shrink_prob(p_dom, 0.25)

    score = (
        0.28 * logit(clamp_probability(p_swe))
        + 0.22 * logit(clamp_probability(p_atp))
        + 0.14 * logit(clamp_probability(p_rank))
        + 0.14 * logit(clamp_probability(p_form5))
        + 0.09 * logit(clamp_probability(p_form10))
        + 0.07 * logit(clamp_probability(p_surface_form5))
        + 0.06 * logit(clamp_probability(p_dom))
        + 0.18
    )

    premium = sigmoid(score / 0.96)

    veto = apply_clay_veto(
        surface,
        player_a_points,
        player_b_points,
        swe_a,
        swe_b,
        player_b_is_qualifier,
        player_b_tournament_wins,
    )

    decision = "✅ Jouable" if premium > 0.80 and not veto else "❌ Pas jouable"

    return asdict(
        MatchPrediction(
            playerA=player_a,
            playerB=player_b,
            surface=surface,
            playerAPoints=player_a_points,
            playerBPoints=player_b_points,
            playerARank=player_a_rank,
            playerBRank=player_b_rank,
            playerAForm5=round(form5_a, 3),
            playerBForm5=round(form5_b, 3),
            sweA=round(swe_a, 3),
            sweB=round(swe_b, 3),
            pSwe=round(p_swe, 3),
            pAtp=round(p_atp, 3),
            pRank=round(p_rank, 3),
            pForm5=round(p_form5, 3),
            pForm10=round(p_form10, 3),
            pSurfaceForm5=round(p_surface_form5, 3),
            pDominance=round(p_dom, 3),
            premium=round(premium, 3),
            premiumPct=round(premium * 100.0, 1),
            veto="oui" if veto else "non",
            decision=decision,
        )
    )


def calculate_predictions(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    state = get_state()
    results = [calculate_match_prediction(match, state) for match in matches]
    results.sort(key=lambda row: row.get("premium", -1), reverse=True)

    return {
        "matches": results,
        "summary": build_summary(results),
        "engine": {
            "name": "Tennis Motor V7",
            "version": "Bayesian Shrinkage",
            "historyYears": HISTORY_YEARS,
            "historyRowsLoaded": state["history_rows_loaded"],
            "premiumFormula": "Bayesian shrinkage blend of SWE, ATP, Rank, Form5, Form10, SurfaceForm5, Dominance",
            "threshold": "> 0.80",
        },
    }
