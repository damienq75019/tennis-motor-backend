import csv
import math
import os
import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_YEARS = [2022, 2023, 2024, 2025, 2026]

INVALID_SCORE_TOKENS = ("RET", "W/O", "WO", "DEF", "ABD", "ABN")


def normalize_surface(surface: str) -> str:
    if not surface:
        return "Hard"

    value = str(surface).strip().title()

    if value == "Carpet":
        return "Hard"
    if "Hard" in value:
        return "Hard"
    if "Clay" in value:
        return "Clay"
    if "Grass" in value:
        return "Grass"

    return "Hard"


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    if value is None:
        return False

    text = str(value).strip().lower()
    return text in {"true", "1", "oui", "yes", "y"}


def display_name_for_history(name: str) -> str:
    """Return a stable display name matching Jeff Sackmann style when possible.

    Sportradar commonly provides names as "Surname, Firstname" while the
    historical CSVs use "Firstname Surname". The engine history keys must not
    treat those as two different players.
    """
    value = re.sub(r"\s+", " ", (name or "").strip())
    if "," in value:
        left, right = value.split(",", 1)
        left = re.sub(r"\s+", " ", left.strip())
        right = re.sub(r"\s+", " ", right.strip())
        if left and right:
            return f"{right} {left}"
    return value


def canonical_name(name: str) -> str:
    value = display_name_for_history(name).strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9\s\-']", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def clamp_probability(p: float) -> float:
    return min(max(p, 1e-6), 1.0 - 1e-6)


def logistic_probability_from_diff(diff: float) -> float:
    return 1.0 / (1.0 + (10.0 ** (-diff / 400.0)))


def sigmoid(x: float) -> float:
    if x >= 60:
        return 1.0
    if x <= -60:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def logit(p: float) -> float:
    p = clamp_probability(p)
    return math.log(p / (1.0 - p))


def atp_points_probability(points_a: float, points_b: float, scale: float = 1800.0) -> float:
    diff = points_a - points_b
    return 1.0 / (1.0 + (10.0 ** (-diff / scale)))


def rank_probability(rank_a: int, rank_b: int, scale: float = 32.0) -> float:
    if rank_a <= 0 or rank_b <= 0:
        return 0.5

    diff_rank = rank_b - rank_a
    return 1.0 / (1.0 + math.exp(-diff_rank / scale))


def form_probability(form_a: float, form_b: float, scale: float = 0.16) -> float:
    return 1.0 / (1.0 + math.exp(-((form_a - form_b) / scale)))


def resolve_history_file(year: int) -> str:
    """
    Trouve le fichier historique ATP singles pour une année.
    Accepte les deux conventions utilisées dans ton projet :
    - data/2026.csv
    - data/atp_matches_2026.csv

    Fallback possible si le fichier est placé à côté du script backend :
    - 2026.csv
    - atp_matches_2026.csv
    """
    candidates = [
        os.path.join(DATA_DIR, f"{year}.csv"),
        os.path.join(DATA_DIR, f"atp_matches_{year}.csv"),
        os.path.join(BASE_DIR, f"{year}.csv"),
        os.path.join(BASE_DIR, f"atp_matches_{year}.csv"),
    ]

    for filepath in candidates:
        if os.path.exists(filepath) and os.path.isfile(filepath):
            return filepath

    return ""


def load_history_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    wanted_stats = {
        "w_ace", "w_df", "w_svpt", "w_1stIn", "w_1stWon", "w_2ndWon", "w_bpSaved", "w_bpFaced",
        "l_ace", "l_df", "l_svpt", "l_1stIn", "l_1stWon", "l_2ndWon", "l_bpSaved", "l_bpFaced",
    }

    for year in HISTORY_YEARS:
        filepath = resolve_history_file(year)
        if not filepath:
            continue

        with open(filepath, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)

            required_columns = {"winner_name", "loser_name", "surface", "tourney_date"}
            if not reader.fieldnames or not required_columns.issubset(set(reader.fieldnames)):
                continue

            for row in reader:
                winner = (row.get("winner_name") or "").strip()
                loser = (row.get("loser_name") or "").strip()
                score = (row.get("score") or "").upper().strip()

                if not winner or not loser:
                    continue

                if any(token in score for token in INVALID_SCORE_TOKENS):
                    continue

                surface = normalize_surface(row.get("surface", ""))
                level = (row.get("tourney_level") or "").strip().upper()
                if level in {"", "250", "500", "A"}:
                    level = "A"

                round_name = (row.get("round") or "").strip()
                if round_name == "BR":
                    round_name = "R128"
                if not round_name:
                    round_name = "R128"

                item: Dict[str, Any] = {
                    "winner_name": winner,
                    "loser_name": loser,
                    "winner_key": canonical_name(winner),
                    "loser_key": canonical_name(loser),
                    "surface": surface,
                    "tourney_level": level,
                    "round": round_name,
                    "tourney_date": safe_int(row.get("tourney_date", 0)),
                    "match_num": safe_int(row.get("match_num", 0)),
                    "winner_rank": safe_int(row.get("winner_rank", 0)),
                    "loser_rank": safe_int(row.get("loser_rank", 0)),
                    "winner_rank_points": safe_int(row.get("winner_rank_points", 0)),
                    "loser_rank_points": safe_int(row.get("loser_rank_points", 0)),
                }

                for k in wanted_stats:
                    item[k] = safe_int(row.get(k, 0))

                rows.append(item)

    rows.sort(key=lambda r: (r["tourney_date"], r["match_num"], r["winner_key"], r["loser_key"]))
    return rows


def build_rank_reference(
    latest_rank_by_player: Dict[str, int],
    latest_points_by_player: Dict[str, int],
) -> Tuple[List[int], List[int]]:
    pairs = []
    seen = set()

    for key, points in latest_points_by_player.items():
        rank = latest_rank_by_player.get(key, 0)
        if points > 0 and rank > 0:
            pair = (points, rank)
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)

    if not pairs:
        return [], []

    pairs.sort(key=lambda x: x[0])
    points_sorted = [p for p, _ in pairs]
    ranks_sorted = [r for _, r in pairs]
    return points_sorted, ranks_sorted


def estimate_rank_from_points(points: int, state: Dict[str, Any]) -> int:
    if points <= 0:
        return 0

    points_sorted = state["rank_reference_points"]
    ranks_sorted = state["rank_reference_ranks"]

    if not points_sorted:
        return 0

    idx = bisect_left(points_sorted, points)

    if idx <= 0:
        return ranks_sorted[0]

    if idx >= len(points_sorted):
        return ranks_sorted[-1]

    left_points = points_sorted[idx - 1]
    right_points = points_sorted[idx]
    left_rank = ranks_sorted[idx - 1]
    right_rank = ranks_sorted[idx]

    if abs(points - left_points) <= abs(right_points - points):
        return left_rank

    return right_rank


def hold_proxy(
    ace: int,
    df: int,
    svpt: int,
    first_in: int,
    first_won: int,
    second_won: int,
    bp_saved: int,
    bp_faced: int,
) -> float:
    if svpt <= 0:
        return 0.5

    first_in_rate = first_in / max(svpt, 1)
    spw = (first_won + second_won) / max(svpt, 1)
    ace_rate = ace / max(svpt, 1)
    df_rate = df / max(svpt, 1)
    bp_save = bp_saved / bp_faced if bp_faced and bp_faced > 0 else 0.65

    score = (
        0.62 * spw
        + 0.10 * first_in_rate
        + 0.14 * bp_save
        + 0.08 * ace_rate
        - 0.12 * df_rate
    )
    return max(0.0, min(1.0, score))


def _has_service_stats(prefix: str, row: Dict[str, Any]) -> bool:
    return safe_int(row.get(f"{prefix}_svpt", 0)) > 0


def _parse_score_sets(score: str) -> List[Tuple[int, int]]:
    """Parse tennis score tokens from winner perspective.

    The historical CSV score is winner-first, e.g. "6-4 6-7 6-3".
    Tiebreak annotations are ignored safely.
    """
    sets: List[Tuple[int, int]] = []
    for token in re.split(r"\s+", (score or "").strip()):
        token = token.strip()
        if not token or any(x in token.upper() for x in INVALID_SCORE_TOKENS):
            continue
        token = re.sub(r"\([^)]*\)", "", token)
        m = re.match(r"^(\d{1,2})-(\d{1,2})$", token)
        if not m:
            continue
        w = safe_int(m.group(1), -1)
        l = safe_int(m.group(2), -1)
        if w < 0 or l < 0:
            continue
        # Ignore impossible/non-set fragments.
        if max(w, l) > 7 and not (w >= 10 and l >= 8):
            continue
        sets.append((w, l))
    return sets


def score_dominance_from_score(score: str) -> Optional[float]:
    """Return a moderate dominance signal from the set score.

    Output is centered around 0 from the winner perspective. It is intentionally
    softer than a betting/market signal and only replaces missing service stats.
    """
    sets = _parse_score_sets(score)
    if not sets:
        return None

    games_won = sum(w for w, _ in sets)
    games_lost = sum(l for _, l in sets)
    total_games = games_won + games_lost
    if total_games <= 0:
        return None

    set_wins = sum(1 for w, l in sets if w > l)
    set_losses = sum(1 for w, l in sets if l > w)
    set_total = max(1, set_wins + set_losses)

    game_component = (games_won - games_lost) / total_games
    set_component = (set_wins - set_losses) / set_total

    # Typical straight-set win around +0.06/+0.10, blowout capped below +0.25.
    value = 0.18 * game_component + 0.04 * set_component
    return max(-0.25, min(0.25, value))


def dominance_score_from_row(prefix_a: str, prefix_b: str, row: Dict[str, Any]) -> Optional[float]:
    if _has_service_stats(prefix_a, row) and _has_service_stats(prefix_b, row):
        own = hold_proxy(
            safe_int(row.get(f"{prefix_a}_ace", 0)),
            safe_int(row.get(f"{prefix_a}_df", 0)),
            safe_int(row.get(f"{prefix_a}_svpt", 0)),
            safe_int(row.get(f"{prefix_a}_1stIn", 0)),
            safe_int(row.get(f"{prefix_a}_1stWon", 0)),
            safe_int(row.get(f"{prefix_a}_2ndWon", 0)),
            safe_int(row.get(f"{prefix_a}_bpSaved", 0)),
            safe_int(row.get(f"{prefix_a}_bpFaced", 0)),
        )

        opp = hold_proxy(
            safe_int(row.get(f"{prefix_b}_ace", 0)),
            safe_int(row.get(f"{prefix_b}_df", 0)),
            safe_int(row.get(f"{prefix_b}_svpt", 0)),
            safe_int(row.get(f"{prefix_b}_1stIn", 0)),
            safe_int(row.get(f"{prefix_b}_1stWon", 0)),
            safe_int(row.get(f"{prefix_b}_2ndWon", 0)),
            safe_int(row.get(f"{prefix_b}_bpSaved", 0)),
            safe_int(row.get(f"{prefix_b}_bpFaced", 0)),
        )
        return own - opp

    return score_dominance_from_score(str(row.get("score") or ""))


def build_state() -> Dict[str, Any]:
    global_elo = defaultdict(lambda: 1500.0)
    global_count = defaultdict(int)

    surface_elo = defaultdict(lambda: defaultdict(lambda: 1500.0))
    surface_count = defaultdict(lambda: defaultdict(int))

    recent_results5 = defaultdict(lambda: deque(maxlen=5))
    recent_results10 = defaultdict(lambda: deque(maxlen=10))
    recent_surface5 = defaultdict(lambda: defaultdict(lambda: deque(maxlen=5)))
    recent_dominance5 = defaultdict(lambda: deque(maxlen=5))

    latest_rank_by_player = defaultdict(int)
    latest_points_by_player = defaultdict(int)

    rows = load_history_rows()

    for row in rows:
        winner_key = row["winner_key"]
        loser_key = row["loser_key"]
        surface = row["surface"]
        level = row["tourney_level"]

        if row["winner_rank"] > 0:
            latest_rank_by_player[winner_key] = row["winner_rank"]
        if row["loser_rank"] > 0:
            latest_rank_by_player[loser_key] = row["loser_rank"]

        if row["winner_rank_points"] > 0:
            latest_points_by_player[winner_key] = row["winner_rank_points"]
        if row["loser_rank_points"] > 0:
            latest_points_by_player[loser_key] = row["loser_rank_points"]

        mult = 1.10 if level == "G" else 1.00

        g_w = global_elo[winner_key]
        g_l = global_elo[loser_key]
        exp_w_global = logistic_probability_from_diff(g_w - g_l)
        exp_l_global = 1.0 - exp_w_global
        k_w_global = 250.0 / ((global_count[winner_key] + 5) ** 0.4)
        k_l_global = 250.0 / ((global_count[loser_key] + 5) ** 0.4)
        global_elo[winner_key] = g_w + mult * k_w_global * (1.0 - exp_w_global)
        global_elo[loser_key] = g_l + mult * k_l_global * (0.0 - exp_l_global)
        global_count[winner_key] += 1
        global_count[loser_key] += 1

        s_w = surface_elo[winner_key][surface]
        s_l = surface_elo[loser_key][surface]
        exp_w_surface = logistic_probability_from_diff(s_w - s_l)
        exp_l_surface = 1.0 - exp_w_surface
        k_w_surface = 250.0 / ((surface_count[winner_key][surface] + 5) ** 0.4)
        k_l_surface = 250.0 / ((surface_count[loser_key][surface] + 5) ** 0.4)
        surface_elo[winner_key][surface] = s_w + mult * k_w_surface * (1.0 - exp_w_surface)
        surface_elo[loser_key][surface] = s_l + mult * k_l_surface * (0.0 - exp_l_surface)
        surface_count[winner_key][surface] += 1
        surface_count[loser_key][surface] += 1

        recent_results5[winner_key].append(1.0)
        recent_results5[loser_key].append(0.0)
        recent_results10[winner_key].append(1.0)
        recent_results10[loser_key].append(0.0)
        recent_surface5[winner_key][surface].append(1.0)
        recent_surface5[loser_key][surface].append(0.0)

        dom_w = dominance_score_from_row("w", "l", row)
        if dom_w is not None:
            recent_dominance5[winner_key].append(dom_w)
            recent_dominance5[loser_key].append(-dom_w)

    rank_reference_points, rank_reference_ranks = build_rank_reference(
        latest_rank_by_player=latest_rank_by_player,
        latest_points_by_player=latest_points_by_player,
    )

    return {
        "global_elo": global_elo,
        "global_count": global_count,
        "surface_elo": surface_elo,
        "surface_count": surface_count,
        "recent_results5": recent_results5,
        "recent_results10": recent_results10,
        "recent_surface5": recent_surface5,
        "recent_dominance5": recent_dominance5,
        "latest_rank_by_player": latest_rank_by_player,
        "latest_points_by_player": latest_points_by_player,
        "rank_reference_points": rank_reference_points,
        "rank_reference_ranks": rank_reference_ranks,
        "history_rows_loaded": len(rows),
    }


def get_surface_weighted_elo(player: str, surface: str, state: Dict[str, Any], shrink: float = 20.0) -> float:
    player_key = canonical_name(player)
    surface = normalize_surface(surface)

    global_player_elo = state["global_elo"][player_key]
    player_surface_elo = state["surface_elo"][player_key][surface]
    n_surface = state["surface_count"][player_key][surface]

    weight_surface = n_surface / (n_surface + shrink)
    return weight_surface * player_surface_elo + (1.0 - weight_surface) * global_player_elo


def get_latest_or_estimated_rank(player_name: str, player_points: int, state: Dict[str, Any]) -> int:
    key = canonical_name(player_name)
    rank = state["latest_rank_by_player"].get(key, 0)
    if rank > 0:
        return rank
    return estimate_rank_from_points(player_points, state)


def get_form5_rate(player_name: str, state: Dict[str, Any]) -> float:
    key = canonical_name(player_name)
    values = list(state["recent_results5"][key])
    if not values:
        return 0.5
    return sum(values) / len(values)


def get_form10_rate(player_name: str, state: Dict[str, Any]) -> float:
    key = canonical_name(player_name)
    values = list(state["recent_results10"][key])
    if not values:
        return 0.5
    return sum(values) / len(values)


def get_surface_form5_rate(player_name: str, surface: str, state: Dict[str, Any]) -> float:
    key = canonical_name(player_name)
    values = list(state["recent_surface5"][key][normalize_surface(surface)])
    if not values:
        return get_form5_rate(player_name, state)
    return sum(values) / len(values)


def get_dominance5_rate(player_name: str, state: Dict[str, Any]) -> float:
    key = canonical_name(player_name)
    values = list(state["recent_dominance5"][key])
    if not values:
        return 0.0
    return sum(values) / len(values)



def get_player_history_audit(player_name: str, surface: str, state: Dict[str, Any]) -> Dict[str, Any]:
    key = canonical_name(player_name)
    surface = normalize_surface(surface)
    global_matches = int(state["global_count"].get(key, 0))
    surface_matches = int(state["surface_count"].get(key, {}).get(surface, 0))
    form5_n = len(state["recent_results5"].get(key, []))
    form10_n = len(state["recent_results10"].get(key, []))
    surface_form5_n = len(state["recent_surface5"].get(key, {}).get(surface, []))
    dominance_n = len(state["recent_dominance5"].get(key, []))

    if global_matches <= 0:
        swe_source = "default_1500_no_history"
    elif surface_matches <= 0:
        swe_source = "computed_global_history_surface_default"
    else:
        swe_source = "computed_surface_and_global_history"

    return {
        "historyKey": key,
        "historyMatches": global_matches,
        "surfaceHistoryMatches": surface_matches,
        "form5Matches": form5_n,
        "form10Matches": form10_n,
        "surfaceForm5Matches": surface_form5_n,
        "dominanceMatches": dominance_n,
        "sweSource": swe_source,
        "form5Source": "computed_history" if form5_n > 0 else "default_0_5_no_history",
        "form10Source": "computed_history" if form10_n > 0 else "default_0_5_no_history",
        "surfaceForm5Source": "computed_surface_history" if surface_form5_n > 0 else ("fallback_global_form5" if form5_n > 0 else "default_0_5_no_history"),
        "dominanceSource": "computed_stats_or_score_history" if dominance_n > 0 else "default_0_5_no_history",
    }

def apply_clay_veto(
    surface: str,
    player_a_points: float,
    player_b_points: float,
    swe_a: float,
    swe_b: float,
    player_b_is_qualifier: bool,
    player_b_tournament_wins: int,
) -> bool:
    if normalize_surface(surface) != "Clay":
        return False

    danger = player_b_is_qualifier or player_b_tournament_wins >= 2
    if not danger:
        return False

    extreme_advantage = (
        (player_a_points - player_b_points) >= 1500
        and (swe_a - swe_b) >= 120
    )
    return not extreme_advantage


def validate_match_input(match: Dict[str, Any]) -> List[str]:
    errors = []

    player_a = (match.get("playerA") or "").strip()
    player_b = (match.get("playerB") or "").strip()
    surface = (match.get("surface") or "").strip()

    if not player_a:
        errors.append("playerA manquant")
    if not player_b:
        errors.append("playerB manquant")
    if not surface:
        errors.append("surface manquante")
    if safe_int(match.get("playerAPoints"), -1) < 0:
        errors.append("playerAPoints invalide")
    if safe_int(match.get("playerBPoints"), -1) < 0:
        errors.append("playerBPoints invalide")
    if safe_int(match.get("player_b_tournament_wins"), -1) < 0:
        errors.append("player_b_tournament_wins invalide")

    return errors


def build_summary(results: List[Dict[str, Any]]) -> Dict[str, int]:
    valid_rows = [row for row in results if "error" not in row]
    return {
        "totalRows": len(results),
        "validRows": len(valid_rows),
        "errorRows": len(results) - len(valid_rows),
        "over80": sum(1 for row in valid_rows if row["premium"] > 0.80),
        "vetoCount": sum(1 for row in valid_rows if row["veto"] == "oui"),
        "jouables": sum(1 for row in valid_rows if row["decision"] == "✅ Jouable"),
    }
