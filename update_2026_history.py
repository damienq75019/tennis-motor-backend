#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - update_2026_history.py

But :
- Mettre à jour data/2026.csv SANS Jeff Sackmann.
- Ajouter uniquement les matchs ATP singles terminés de la veille.
- Ne jamais injecter un match du jour non terminé dans l'Elo.
- Utiliser le payload Tennis Motor de la veille pour récupérer :
  joueurs, surface, points ATP utilisés par ton moteur.
- Utiliser Flashscore uniquement pour vérifier le résultat réel terminé.
- Garder le même format CSV que tes fichiers 2025/2026.

Fonctionnement attendu avec ton app.py actuel :
- app.py lance ce fichier avant /daily, /predictions ou /calculate.
- Ce fichier cherche output/payload_YYYY-MM-DD.json pour la veille.
- Il lit Flashscore sur la date de la veille.
- Il ajoute les matchs terminés trouvés dans data/2026.csv.
- Il dédoublonne pour ne pas ajouter deux fois le même match.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore


# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

HISTORY_PATH = DATA_DIR / "2026.csv"
AUDIT_PATH = OUTPUT_DIR / "update_2026_history_audit.json"
PREMIUM_HISTORY_PATH = OUTPUT_DIR / "premium_history.json"
PREMIUM_HISTORY_SUMMARY_PATH = OUTPUT_DIR / "premium_history_summary.json"

FLASH_URL_FR = "https://www.flashscore.fr/tennis/"
FLASH_URL_COM = "https://www.flashscore.com/tennis/"


# Colonnes standard type Jeff Sackmann.
# Si data/2026.csv existe déjà, le script conserve ses colonnes exactes.
STANDARD_COLUMNS = [
    "tourney_id",
    "tourney_name",
    "surface",
    "draw_size",
    "tourney_level",
    "tourney_date",
    "match_num",
    "winner_id",
    "winner_seed",
    "winner_entry",
    "winner_name",
    "winner_hand",
    "winner_ht",
    "winner_ioc",
    "winner_age",
    "loser_id",
    "loser_seed",
    "loser_entry",
    "loser_name",
    "loser_hand",
    "loser_ht",
    "loser_ioc",
    "loser_age",
    "score",
    "best_of",
    "round",
    "minutes",
    "w_ace",
    "w_df",
    "w_svpt",
    "w_1stIn",
    "w_1stWon",
    "w_2ndWon",
    "w_SvGms",
    "w_bpSaved",
    "w_bpFaced",
    "l_ace",
    "l_df",
    "l_svpt",
    "l_1stIn",
    "l_1stWon",
    "l_2ndWon",
    "l_SvGms",
    "l_bpSaved",
    "l_bpFaced",
    "winner_rank",
    "winner_rank_points",
    "loser_rank",
    "loser_rank_points",
]


# ---------------------------------------------------------------------------
# Base utils
# ---------------------------------------------------------------------------

def _setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def paris_today() -> date:
    try:
        if ZoneInfo is not None:
            return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:
        pass
    return date.today()


def resolve_target_day() -> date:
    """
    Par défaut : la veille.
    Override possible pour test local :
        set UPDATE_2026_TARGET_DATE=2026-05-14
        py update_2026_history.py
    """
    raw = os.getenv("UPDATE_2026_TARGET_DATE", "").strip()
    if raw:
        return date.fromisoformat(raw)
    return paris_today() - timedelta(days=1)


def ymd_int(day: date) -> int:
    return int(day.strftime("%Y%m%d"))


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def strip_accents(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def norm_name(name: str) -> str:
    value = strip_accents(name).lower()
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(?:wc|q|ll|pr|alt|seed)\b", " ", value)
    return normalize_space(value)


def name_tokens(name: str, keep_short: bool = False) -> List[str]:
    tokens = norm_name(name).split()
    if keep_short:
        return [x for x in tokens if x]
    return [x for x in tokens if len(x) >= 2]


def same_player(a: str, b: str) -> bool:
    """
    Matching robuste ATP full name <-> Flashscore.
    Exemples :
    - "Carlos Alcaraz" == "Alcaraz C."
    - "Felix Auger Aliassime" == "Auger-Aliassime F."
    - "Giovanni Mpetshi Perricard" == "Mpetshi Perricard G."
    """
    na = norm_name(a)
    nb = norm_name(b)

    if not na or not nb:
        return False

    if na == nb:
        return True

    ta = name_tokens(a, keep_short=True)
    tb = name_tokens(b, keep_short=True)

    if not ta or not tb:
        return False

    # Même nom de famille simple.
    if len(ta[-1]) >= 4 and ta[-1] == tb[-1]:
        return True

    # Flashscore : "Cilic M." => ["cilic", "m"]
    def surname_initial(parts: List[str]) -> Tuple[str, str]:
        if len(parts) < 2:
            return (" ".join(parts), "")
        if len(parts[-1]) == 1:
            return (" ".join(parts[:-1]), parts[-1])
        if len(parts[0]) == 1:
            return (" ".join(parts[1:]), parts[0])
        return (" ".join(parts[-2:]), parts[0][0])

    surname_a, initial_a = surname_initial(ta)
    surname_b, initial_b = surname_initial(tb)

    full_a_first = ta[0][0] if ta and ta[0] else ""
    full_b_first = tb[0][0] if tb and tb[0] else ""

    tail_a_1 = ta[-1]
    tail_b_1 = tb[-1]
    tail_a_2 = " ".join(ta[-2:]) if len(ta) >= 2 else tail_a_1
    tail_b_2 = " ".join(tb[-2:]) if len(tb) >= 2 else tail_b_1
    tail_a_3 = " ".join(ta[-3:]) if len(ta) >= 3 else tail_a_2
    tail_b_3 = " ".join(tb[-3:]) if len(tb) >= 3 else tail_b_2

    if initial_a and initial_a == full_b_first:
        if surname_a in {tail_b_1, tail_b_2, tail_b_3}:
            return True

    if initial_b and initial_b == full_a_first:
        if surname_b in {tail_a_1, tail_a_2, tail_a_3}:
            return True

    # Nom composé partiel.
    if tail_a_2 == tail_b_2 or tail_a_3 == tail_b_3:
        return True

    return False


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def normalize_surface(value: Any) -> str:
    text = str(value or "").strip().title()
    if "Clay" in text or "Terre" in text:
        return "Clay"
    if "Grass" in text or "Gazon" in text:
        return "Grass"
    if "Hard" in text or "Dur" in text:
        return "Hard"
    return "Hard"


def extract_matches_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in ("matches", "payload", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

    return []


def get_first(match: Dict[str, Any], keys: List[str], default: Any = "") -> Any:
    for key in keys:
        if key in match and match.get(key) not in (None, ""):
            return match.get(key)
    return default


# ---------------------------------------------------------------------------
# Payload Tennis Motor de la veille
# ---------------------------------------------------------------------------

def possible_payload_paths(target_day: date) -> List[Path]:
    day = target_day.isoformat()
    return [
        OUTPUT_DIR / f"payload_{day}.json",
        OUTPUT_DIR / f"result_{day}.json",
        OUTPUT_DIR / f"daily_{day}.json",
        OUTPUT_DIR / "payload_latest.json",
    ]


def load_target_payload(target_day: date, audit: List[str]) -> List[Dict[str, Any]]:
    """
    Lit le payload daily de la veille.
    C'est lui qui donne les surfaces et les points ATP du moteur.
    """
    day = target_day.isoformat()

    for path in possible_payload_paths(target_day):
        if not path.exists():
            audit.append(f"payload_missing={path}")
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            audit.append(f"payload_read_error={path} error={type(exc).__name__}: {exc}")
            continue

        # Pour payload_latest, on vérifie qu'il correspond bien au jour demandé.
        if path.name == "payload_latest.json" and isinstance(data, dict):
            found_day = (
                str(data.get("targetDay") or "")
                or str((data.get("daily") or {}).get("targetDay") or "")
                or str((data.get("meta") or {}).get("targetDay") or "")
            )
            if found_day and found_day != day:
                audit.append(f"payload_latest_ignored_targetDay={found_day} expected={day}")
                continue

        matches = extract_matches_from_payload(data)
        if matches:
            audit.append(f"payload_used={path} matches={len(matches)}")
            return matches

        audit.append(f"payload_no_matches={path}")

    return []


def payload_context(match: Dict[str, Any]) -> Dict[str, Any]:
    """
    Reconstitue la vraie paire source + points + surface.

    Ton app.py peut inverser A/B pour analyser le meilleur côté.
    Donc on garde sourcePlayerA/sourcePlayerB quand ils existent.
    """
    display_a = str(get_first(match, ["playerA", "player_a"], "") or "").strip()
    display_b = str(get_first(match, ["playerB", "player_b"], "") or "").strip()

    source_a = str(get_first(match, ["sourcePlayerA", "source_player_a"], display_a) or "").strip()
    source_b = str(get_first(match, ["sourcePlayerB", "source_player_b"], display_b) or "").strip()

    surface = normalize_surface(get_first(match, ["surface"], "Hard"))

    display_a_points = safe_int(get_first(match, ["playerAPoints", "player_a_points"], 0))
    display_b_points = safe_int(get_first(match, ["playerBPoints", "player_b_points"], 0))

    display_a_rank = safe_int(get_first(match, ["playerARank", "player_a_rank"], 0))
    display_b_rank = safe_int(get_first(match, ["playerBRank", "player_b_rank"], 0))

    points_by_name: Dict[str, int] = {}
    rank_by_name: Dict[str, int] = {}

    def put_player(name: str, points: int, rank: int) -> None:
        key = norm_name(name)
        if not key:
            return
        if points > 0:
            points_by_name[key] = points
        if rank > 0:
            rank_by_name[key] = rank

    put_player(display_a, display_a_points, display_a_rank)
    put_player(display_b, display_b_points, display_b_rank)

    # Si sourceA/sourceB existent mais l'affichage a été inversé,
    # on mappe quand même les points correctement.
    if source_a and display_a and same_player(source_a, display_a):
        put_player(source_a, display_a_points, display_a_rank)
    if source_a and display_b and same_player(source_a, display_b):
        put_player(source_a, display_b_points, display_b_rank)
    if source_b and display_a and same_player(source_b, display_a):
        put_player(source_b, display_a_points, display_a_rank)
    if source_b and display_b and same_player(source_b, display_b):
        put_player(source_b, display_b_points, display_b_rank)

    return {
        "sourceA": source_a,
        "sourceB": source_b,
        "surface": surface,
        "pointsByName": points_by_name,
        "rankByName": rank_by_name,
    }


# ---------------------------------------------------------------------------
# Flashscore terminé
# ---------------------------------------------------------------------------

def click_optional(page: Any, labels: List[str], timeout_ms: int = 2500) -> bool:
    for label in labels:
        for exact in (True, False):
            try:
                page.get_by_text(label, exact=exact).first.click(timeout=timeout_ms)
                return True
            except Exception:
                pass

        for selector in (
            f"text={label}",
            f"button:has-text('{label}')",
            f"[role='button']:has-text('{label}')",
        ):
            try:
                page.locator(selector).first.click(timeout=timeout_ms)
                return True
            except Exception:
                pass

    return False


def scroll_until_stable(page: Any, audit: List[str], max_rounds: int = 12) -> None:
    previous_count = -1
    stable_rounds = 0

    for idx in range(max_rounds):
        try:
            count = int(page.evaluate("() => document.querySelectorAll('[class*=event__match], [id^=g_2_], [id^=g_1_]').length"))
        except Exception:
            count = previous_count

        audit.append(f"flashscore_scroll_round={idx + 1} rows_before={count}")

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass

        try:
            page.mouse.wheel(0, 1800)
        except Exception:
            pass

        try:
            page.wait_for_timeout(900)
        except Exception:
            pass

        try:
            new_count = int(page.evaluate("() => document.querySelectorAll('[class*=event__match], [id^=g_2_], [id^=g_1_]').length"))
        except Exception:
            new_count = count

        audit.append(f"flashscore_scroll_round={idx + 1} rows_after={new_count}")

        if new_count <= previous_count or new_count == count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        previous_count = max(previous_count, new_count)

        if stable_rounds >= 3:
            break


def flashscore_urls_for_day(target_day: date) -> List[str]:
    """
    Flashscore accepte généralement ?d=-1 pour hier.
    On garde plusieurs variantes, puis fallback sans paramètre.
    """
    delta = (target_day - paris_today()).days
    urls = [
        f"{FLASH_URL_FR}?d={delta}",
        f"{FLASH_URL_COM}?d={delta}",
    ]

    if delta == -1:
        urls.extend([
            f"{FLASH_URL_FR}?d=-1",
            f"{FLASH_URL_COM}?d=-1",
        ])

    urls.extend([FLASH_URL_FR, FLASH_URL_COM])

    out: List[str] = []
    for url in urls:
        if url not in out:
            out.append(url)
    return out


def completed_results_js() -> str:
    return r"""
() => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

    const textOne = (root, selectors) => {
        for (const sel of selectors) {
            const el = root.querySelector(sel);
            const t = el ? clean(el.textContent) : '';
            if (t) return t;
        }
        return '';
    };

    const texts = (root, selectors) => {
        const out = [];
        const seen = new Set();

        for (const sel of selectors) {
            root.querySelectorAll(sel).forEach((el) => {
                const t = clean(el.textContent);
                if (t && !seen.has(t)) {
                    seen.add(t);
                    out.push(t);
                }
            });
            if (out.length) break;
        }

        return out;
    };

    const headerFor = (node) => {
        let cur = node.previousElementSibling;
        for (let i = 0; cur && i < 60; i++) {
            const cls = cur.className || '';
            const txt = clean(cur.innerText || cur.textContent || '');
            if (txt && (
                String(cls).includes('event__header') ||
                txt.includes('ATP') ||
                txt.includes('WTA') ||
                txt.includes('CHALLENGER') ||
                txt.includes('ITF')
            )) {
                return txt;
            }
            cur = cur.previousElementSibling;
        }

        let parent = node.parentElement;
        for (let depth = 0; parent && depth < 5; depth++) {
            const txt = clean(parent.innerText || parent.textContent || '');
            if (txt && txt.includes('ATP')) return txt.slice(0, 200);
            parent = parent.parentElement;
        }

        return '';
    };

    const nodes = Array.from(document.querySelectorAll(
        '[class*="event__match"], [id^="g_2_"], [id^="g_1_"]'
    ));

    const rows = [];

    for (const node of nodes) {
        const playerA = textOne(node, [
            '[class*="event__participant--home"]',
            '[class*="participant__participantNameWrapper"]:nth-of-type(1)',
            '[class*="participantName"]:nth-of-type(1)'
        ]);

        const playerB = textOne(node, [
            '[class*="event__participant--away"]',
            '[class*="participant__participantNameWrapper"]:nth-of-type(2)',
            '[class*="participantName"]:nth-of-type(2)'
        ]);

        const status = textOne(node, [
            '[class*="event__stage"]',
            '[class*="event__time"]',
            '[class*="event__status"]'
        ]);

        const homeScores = texts(node, [
            '[class*="event__score--home"]',
            '[class*="score--home"]'
        ]);

        const awayScores = texts(node, [
            '[class*="event__score--away"]',
            '[class*="score--away"]'
        ]);

        const raw = clean(node.innerText || node.textContent || '');
        const header = headerFor(node);

        if (playerA && playerB) {
            rows.push({
                playerA,
                playerB,
                status,
                homeScores,
                awayScores,
                header,
                raw
            });
        }
    }

    return rows;
}
"""


def int_list(values: Any) -> List[int]:
    out: List[int] = []
    source = values if isinstance(values, list) else [values]

    for item in source:
        text = str(item or "")
        # Scores entiers uniquement. N'attrape pas les cotes 1.40 / 2.95.
        for m in re.finditer(r"(?<![\d.,])\d{1,2}(?![\d.,])", text):
            try:
                out.append(int(m.group(0)))
            except Exception:
                pass

    return out


def is_completed_row(row: Dict[str, Any]) -> bool:
    status = str(row.get("status") or "").lower()
    raw = str(row.get("raw") or "").lower()

    markers = ("termin", "finished", "après", "after", "fini")
    if any(x in status for x in markers) or any(x in raw for x in markers):
        return True

    home = int_list(row.get("homeScores"))
    away = int_list(row.get("awayScores"))

    # Format fréquent : [2, 6, 6] / [0, 3, 4]
    if home and away and home[0] != away[0] and max(home[0], away[0]) >= 2:
        return True

    return False


def winner_from_completed_row(row: Dict[str, Any]) -> str:
    a = str(row.get("playerA") or "")
    b = str(row.get("playerB") or "")
    home = int_list(row.get("homeScores"))
    away = int_list(row.get("awayScores"))

    if home and away and home[0] != away[0]:
        return a if home[0] > away[0] else b

    raw = str(row.get("raw") or "")
    nums = int_list(raw)
    if len(nums) >= 6 and nums[0] != nums[3]:
        return a if nums[0] > nums[3] else b

    return ""


def score_from_completed_row(row: Dict[str, Any], winner_name: str) -> str:
    """
    Construit un score simple au format lisible par le moteur.
    Exemple : 6-3 6-4
    Si les jeux par set ne sont pas lisibles : 2-0.
    """
    fs_a = str(row.get("playerA") or "")
    fs_b = str(row.get("playerB") or "")
    home = int_list(row.get("homeScores"))
    away = int_list(row.get("awayScores"))

    if not home or not away:
        return ""

    winner_is_home = same_player(winner_name, fs_a)
    winner_sets = home[0] if winner_is_home else away[0]
    loser_sets = away[0] if winner_is_home else home[0]

    home_games = home[1:]
    away_games = away[1:]

    parts: List[str] = []
    for h, a in zip(home_games, away_games):
        if winner_is_home:
            parts.append(f"{h}-{a}")
        else:
            parts.append(f"{a}-{h}")

    if parts:
        return " ".join(parts)

    if winner_sets or loser_sets:
        return f"{winner_sets}-{loser_sets}"

    return ""


def looks_like_atp_singles_header(header: str) -> bool:
    """
    On reste prudent :
    - Le vrai filtre ATP singles vient surtout du payload Tennis Motor de la veille.
    - Ce filtre évite seulement de prendre doubles/WTA si les noms matchent bizarrement.
    """
    h = strip_accents(header or "").lower()

    if not h:
        return True

    banned = [
        "wta",
        "doubles",
        "double",
        "challenger",
        "itf",
        "exhibition",
        "mixed",
        "juniors",
        "girls",
        "boys",
    ]
    if any(x in h for x in banned):
        return False

    if "atp" in h or "hommes" in h or "men" in h or "simples" in h or "singles" in h:
        return True

    return True




def is_flashscore_score_number(line: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}", normalize_space(str(line or ""))))


def is_flashscore_section_category(line: str) -> bool:
    n = strip_accents(str(line or "")).lower()
    return any(key in n for key in (
        "atp - simples", "wta - simples", "atp - doubles", "wta - doubles",
        "challenger masculin", "challenger feminin", "itf masculin", "itf feminin",
        "exhibition", "garcons", "filles", "doubles mixtes"
    ))


def is_flashscore_tournament_header(line: str) -> bool:
    s = normalize_space(str(line or ""))
    n = strip_accents(s).lower()
    if not s:
        return False
    if is_flashscore_section_category(s):
        return False
    if s in {"Tableau", "Classement", "Calendrier", "Résultats", "Resultats", "Publicité", "Publicite"}:
        return False
    if n.startswith(("termine", "forfait", "abandon", "direct", "a venir")):
        return False
    # Flashscore écrit généralement : "Genève (Suisse) - Qualifications, terre battue"
    # ou "Rome (Italie), terre battue".
    return ("(" in s and ")" in s and ("," in s or " - " in s))


def parse_flashscore_text_completed_results(body_text: str, audit: List[str]) -> List[Dict[str, Any]]:
    """
    Parse le texte complet de Flashscore, beaucoup plus fiable que certains sélecteurs DOM.

    Format vu sur Flashscore FR :
      Rome (Italie), terre battue
      ATP - SIMPLES:
      Tableau
      Terminé
      Sinner J.
      Medvedev D.
      2
      1
      6
      2
      ...

    Règle stricte : on garde uniquement les sections ATP - SIMPLES.
    On rejette WTA, ATP doubles, Challenger, ITF, Exhibition, juniors.
    """
    raw_lines = [normalize_space(x) for x in str(body_text or "").splitlines()]
    lines = [x for x in raw_lines if x]

    results: List[Dict[str, Any]] = []
    current_tournament = ""
    current_category = ""
    current_header = ""
    i = 0

    while i < len(lines):
        line = lines[i]
        n = strip_accents(line).lower()

        if is_flashscore_tournament_header(line):
            current_tournament = line
            current_header = line
            i += 1
            continue

        if is_flashscore_section_category(line):
            current_category = line
            current_header = (current_tournament + " " + line).strip()
            i += 1
            continue

        is_atp_singles = "atp - simples" in strip_accents(current_category).lower()

        if is_atp_singles and n.startswith("termine"):
            j = i + 1

            # Rejeter abandons/forfaits : pas de résultat propre pour Elo.
            if j < len(lines) and any(x in strip_accents(lines[j]).lower() for x in ("abandon", "forfait", "walkover")):
                i += 1
                continue

            players: List[str] = []
            while j < len(lines) and len(players) < 2:
                candidate = lines[j]
                cn = strip_accents(candidate).lower()
                if (
                    not is_flashscore_score_number(candidate)
                    and not is_flashscore_section_category(candidate)
                    and not is_flashscore_tournament_header(candidate)
                    and candidate.lower() not in {"tableau", "classement"}
                    and not cn.startswith(("termine", "forfait", "abandon"))
                ):
                    players.append(candidate)
                j += 1

            if len(players) < 2:
                i += 1
                continue

            nums: List[int] = []
            while j < len(lines):
                nxt = lines[j]
                nn = strip_accents(nxt).lower()
                if is_flashscore_score_number(nxt):
                    nums.append(int(nxt))
                    j += 1
                    continue
                # fin du bloc match dès qu'une nouvelle section/un nouveau match commence
                if nn.startswith(("termine", "forfait", "abandon")) or is_flashscore_tournament_header(nxt) or is_flashscore_section_category(nxt):
                    break
                # Texte parasite : on stoppe pour éviter de manger le match suivant.
                break

            if len(nums) >= 2 and nums[0] != nums[1] and max(nums[0], nums[1]) >= 2:
                player_a, player_b = players[0], players[1]
                winner = player_a if nums[0] > nums[1] else player_b
                score_parts: List[str] = []
                set_count = max(1, min(nums[0] + nums[1], 5))
                games = nums[2:]

                # Si Flashscore ajoute les points de tie-break comme lignes séparées,
                # le nombre de valeurs dépasse set_count*2. Dans ce cas, on garde
                # seulement le score en sets pour éviter d'écrire un faux 7-7 / 6-6.
                detailed_score_is_safe = len(games) == set_count * 2
                if detailed_score_is_safe:
                    for k in range(0, len(games), 2):
                        if k + 1 >= len(games):
                            break
                        a_games, b_games = games[k], games[k + 1]
                        if winner == player_a:
                            score_parts.append(f"{a_games}-{b_games}")
                        else:
                            score_parts.append(f"{b_games}-{a_games}")

                set_score = f"{max(nums[0], nums[1])}-{min(nums[0], nums[1])}"

                results.append({
                    "playerA": player_a,
                    "playerB": player_b,
                    "winner": winner,
                    "score": " ".join(score_parts) if score_parts else set_score,
                    "homeScores": nums[0:1] + nums[2::2],
                    "awayScores": nums[1:2] + nums[3::2],
                    "status": "Terminé",
                    "header": current_header[:250],
                    "tournament": current_tournament,
                    "surface": infer_surface_from_tournament(current_header, "Hard"),
                    "raw": " | ".join(lines[i:j])[:500],
                })
                i = j
                continue

        i += 1

    # déduplication stricte par date/paires/score implicite
    dedup: List[Dict[str, Any]] = []
    seen = set()
    for row in results:
        key = (
            norm_name(row.get("playerA")),
            norm_name(row.get("playerB")),
            str(row.get("score") or ""),
            norm_name(row.get("header")),
        )
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)

    audit.append(f"flashscore_text_rows_kept={len(dedup)}")
    if dedup:
        audit.append("flashscore_text_sample=" + " || ".join(
            f"{r.get('playerA')} - {r.get('playerB')} winner={r.get('winner')} score={r.get('score')}"
            for r in dedup[:20]
        ))
    return dedup



def collect_flashscore_body_text_by_scrolling(page: Any, audit: List[str], max_steps: int = 28) -> str:
    """
    Flashscore virtualise une partie des lignes : si on lit le body uniquement
    en bas de page, on perd des matchs déjà sortis du DOM. Cette fonction
    collecte le texte à plusieurs positions de scroll puis concatène les blocs
    uniques. C'est indispensable pour récupérer toutes les sections ATP - SIMPLES
    d'une journée complète.
    """
    blocks: List[str] = []
    seen_blocks = set()

    def add_current(label: str) -> None:
        try:
            txt = page.inner_text("body", timeout=8000)
        except Exception as exc:
            audit.append(f"flashscore_body_text_error_{label}={type(exc).__name__}: {exc}")
            return
        clean = str(txt or "").strip()
        if not clean:
            return
        # IMPORTANT : Flashscore garde souvent le même haut/bas de page (pubs, cookies, menu)
        # pendant que le contenu central change avec la virtualisation.
        # L'ancienne signature début+fin+longueur confondait donc plusieurs vues différentes
        # et ne gardait que 2 blocs. On hash maintenant tout le texte du viewport.
        sig = hashlib.sha1(clean.encode("utf-8", "ignore")).hexdigest()
        if sig not in seen_blocks:
            seen_blocks.add(sig)
            blocks.append(clean)

    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(700)
    except Exception:
        pass

    add_current("top")

    last_y = -1
    stable = 0
    for step in range(max_steps):
        try:
            info = page.evaluate("""() => {
                const y = window.scrollY || document.documentElement.scrollTop || 0;
                const h = document.body.scrollHeight || document.documentElement.scrollHeight || 0;
                const vh = window.innerHeight || 900;
                window.scrollBy(0, Math.max(420, Math.floor(vh * 0.45)));
                return {y, h, vh};
            }""")
        except Exception:
            info = {"y": last_y, "h": 0, "vh": 0}

        try:
            page.mouse.wheel(0, 700)
        except Exception:
            pass

        try:
            page.wait_for_timeout(850)
        except Exception:
            pass

        add_current(f"step{step + 1}")

        try:
            y2 = int(page.evaluate("() => window.scrollY || document.documentElement.scrollTop || 0"))
        except Exception:
            y2 = last_y

        audit.append(f"flashscore_text_scroll_step={step + 1} y={y2} blocks={len(blocks)}")

        if y2 == last_y:
            stable += 1
        else:
            stable = 0
        last_y = y2
        if stable >= 3:
            break

    combined = "\n".join(blocks)
    atp_sections = len(re.findall(r"ATP\s*-\s*SIMPLES", strip_accents(combined), flags=re.IGNORECASE))
    audit.append(f"flashscore_text_blocks_collected={len(blocks)} combined_len={len(combined)} atp_sections_seen={atp_sections}")
    return combined

def fetch_flashscore_completed_results(target_day: date, audit: List[str]) -> List[Dict[str, Any]]:
    """
    Lit Flashscore avec Playwright et récupère les matchs terminés ATP simples.

    Version corrigée :
    - source principale = texte complet de la page Flashscore après clic TERMINÉS ;
    - garde toutes les sections ATP - SIMPLES ;
    - rejette WTA / doubles / Challenger / ITF ;
    - évite les mauvais couples DOM du type Duckworth-Butvilas qui venaient des sélecteurs.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        audit.append(f"playwright_import_error={type(exc).__name__}: {exc}")
        return []

    all_rows: List[Dict[str, Any]] = []
    seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1365, "height": 2200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            },
        )

        for url in flashscore_urls_for_day(target_day):
            page = context.new_page()
            try:
                audit.append(f"flashscore_url={url}")
                page.goto(url, wait_until="domcontentloaded", timeout=45000)

                click_optional(page, ["J'accepte", "Tout refuser", "Accepter", "OK"], timeout_ms=3000)

                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass

                clicked = click_optional(page, ["TERMINÉS", "Terminés", "Terminé", "Finished", "Results", "Résultats"], timeout_ms=5000)
                audit.append(f"completed_tab_clicked={clicked}")

                try:
                    page.wait_for_timeout(3000)
                except Exception:
                    pass

                # Flashscore virtualise les lignes : on doit collecter le texte
                # à plusieurs positions de scroll, sinon on ne récupère qu'une partie
                # des matchs ATP simples terminés.
                body_text = collect_flashscore_body_text_by_scrolling(page, audit, max_steps=55)

                text_rows = parse_flashscore_text_completed_results(body_text, audit)
                audit.append(f"flashscore_text_body_len={len(body_text)}")

                if text_rows:
                    rows = text_rows
                    audit.append("flashscore_parser=text_body")
                else:
                    # Fallback DOM seulement si le texte complet ne donne rien.
                    rows = page.evaluate(completed_results_js())
                    audit.append(f"flashscore_rows_raw={len(rows)}")
                    audit.append("flashscore_parser=dom_fallback")

                kept = 0
                for row in rows:
                    a = str(row.get("playerA") or "").strip()
                    b = str(row.get("playerB") or "").strip()
                    if not a or not b:
                        continue
                    if "/" in a or "/" in b:
                        continue
                    if not looks_like_atp_singles_header(str(row.get("header") or "")):
                        continue
                    if not is_completed_row(row):
                        continue

                    winner = str(row.get("winner") or "").strip() or winner_from_completed_row(row)
                    if not winner:
                        continue

                    key = (norm_name(a), norm_name(b), str(row.get("score") or row.get("raw") or "")[:120])
                    if key in seen:
                        continue
                    seen.add(key)

                    clean = {
                        "playerA": a,
                        "playerB": b,
                        "winner": winner,
                        "score": str(row.get("score") or score_from_completed_row(row, winner) or ""),
                        "homeScores": int_list(row.get("homeScores")),
                        "awayScores": int_list(row.get("awayScores")),
                        "status": str(row.get("status") or ""),
                        "header": str(row.get("header") or "")[:250],
                        "tournament": str(row.get("tournament") or row.get("header") or "")[:250],
                        "surface": infer_surface_from_tournament(str(row.get("header") or row.get("tournament") or ""), "Hard"),
                        "raw": str(row.get("raw") or "")[:500],
                    }
                    all_rows.append(clean)
                    kept += 1

                audit.append(f"flashscore_rows_kept={kept}")

                # Si on a trouvé des lignes sur l'URL datée, pas besoin du fallback sans date.
                if kept > 0 and "?d=" in url:
                    break

            except Exception as exc:
                audit.append(f"flashscore_error url={url} error={type(exc).__name__}: {exc}")
            finally:
                try:
                    page.close()
                except Exception:
                    pass

        browser.close()

    if all_rows:
        sample = []
        for row in all_rows[:25]:
            sample.append(
                f"{row.get('playerA')} - {row.get('playerB')} "
                f"winner={row.get('winner')} score={row.get('score')} surface={row.get('surface')}"
            )
        audit.append("flashscore_sample=" + " || ".join(sample))

    return all_rows


def find_completed_for_payload(source_a: str, source_b: str, completed: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for row in completed:
        fs_a = str(row.get("playerA") or "")
        fs_b = str(row.get("playerB") or "")

        same_order = same_player(source_a, fs_a) and same_player(source_b, fs_b)
        reversed_order = same_player(source_a, fs_b) and same_player(source_b, fs_a)

        if same_order or reversed_order:
            return row

    return None


# ---------------------------------------------------------------------------
# TennisExplorer ATP singles terminés par date (source principale 2026.csv)
# ---------------------------------------------------------------------------

TENNIS_EXPLORER_RESULTS_URL = "https://www.tennisexplorer.com/results/"

BANNED_TE_TOURNAMENT_WORDS = (
    "challenger",
    "futures",
    "utr",
    "exhibition",
    "boys",
    "girls",
    "juniors",
    "wta",
    "doubles",
    "double",
)

TOURNAMENT_SURFACE_HINTS = {
    # Terre battue période actuelle / tournois ATP connus.
    "rome": "Clay",
    "roma": "Clay",
    "geneva": "Clay",
    "geneve": "Clay",
    "genève": "Clay",
    "hamburg": "Clay",
    "hambourg": "Clay",
    "lyon": "Clay",
    "munich": "Clay",
    "munchen": "Clay",
    "muenchen": "Clay",
    "madrid": "Clay",
    "monte carlo": "Clay",
    "barcelona": "Clay",
    "barcelone": "Clay",
    "bastad": "Clay",
    "kitzbuhel": "Clay",
    "kitzbuehel": "Clay",
    "gstaad": "Clay",
    "roland garros": "Clay",
    "french open": "Clay",
    "buenos aires": "Clay",
    "rio de janeiro": "Clay",
    "santiago": "Clay",
    "houston": "Clay",
    "estoril": "Clay",
    "marrakech": "Clay",
    "bucharest": "Clay",
    "bucarest": "Clay",
    "belgrade": "Clay",
    "umag": "Clay",
    "los cabos": "Hard",
    "acapulco": "Hard",
    "doha": "Hard",
    "dubai": "Hard",
    "indian wells": "Hard",
    "miami": "Hard",
    # Gazon.
    "halle": "Grass",
    "stuttgart": "Grass",
    "s hertogenbosch": "Grass",
    "hertogenbosch": "Grass",
    "queens": "Grass",
    "queen": "Grass",
    "mallorca": "Grass",
    "eastbourne": "Grass",
    "wimbledon": "Grass",
}

TOURNAMENT_LEVEL_HINTS = {
    "rome": "M",
    "madrid": "M",
    "monte carlo": "M",
    "miami": "M",
    "indian wells": "M",
    "canada": "M",
    "cincinnati": "M",
    "shanghai": "M",
    "paris masters": "M",
    "roland garros": "G",
    "french open": "G",
    "australian open": "G",
    "wimbledon": "G",
    "us open": "G",
}


def tennis_explorer_url_for_day(target_day: date) -> str:
    # TennisExplorer accepte ces paramètres. La page /results/ affiche aussi une navigation
    # jour précédent/suivant ; le parser limite ensuite au bloc target_day.
    return (
        f"{TENNIS_EXPLORER_RESULTS_URL}"
        f"?type=atp-single&year={target_day.year}&month={target_day.month:02d}&day={target_day.day:02d}"
    )


def tournament_is_allowed_atp_main(name: str) -> bool:
    n = strip_accents(name or "").lower()
    if not n:
        return False
    if any(w in n for w in BANNED_TE_TOURNAMENT_WORDS):
        return False
    # On garde les tournois ATP principaux. Les challengers/futures sont explicitement rejetés.
    return True


def infer_surface_from_tournament(name: str, fallback: str = "Hard") -> str:
    """
    Déduit la surface depuis le libellé tournoi/source.

    IMPORTANT pour Tennis Motor : ne jamais laisser le fallback mettre Hard
    quand la source contient clairement "terre battue". Le fallback Flashscore
    renvoie souvent des headers français du type :
      "Genève (Suisse) - Qualifications, terre battue ATP - SIMPLES: Tableau"
      "Hambourg (Allemagne) - Qualifications, terre battue ATP - SIMPLES: Tableau"
    Ces lignes doivent devenir Clay, sinon le Surface-Weighted Elo est pollué.
    """
    raw = str(name or "")
    n = strip_accents(raw).lower()
    n = n.replace("'", " ").replace("’", " ")
    n = re.sub(r"[^a-z0-9]+", " ", n)
    n = normalize_space(n)

    # Indices explicites de surface, prioritaires sur les noms de tournoi.
    clay_words = (
        "terre battue",
        "clay",
        "red clay",
        "green clay",
        "terre",
        "terra batida",
        "polvere di mattone",
    )
    grass_words = (
        "gazon",
        "grass",
        "herbe",
        "cesped",
    )
    hard_words = (
        "hard",
        "dur",
        "dure",
        "indoor hard",
        "outdoor hard",
        "cement",
        "carpet",
    )

    for word in clay_words:
        if word in n:
            return "Clay"
    for word in grass_words:
        if word in n:
            return "Grass"
    for word in hard_words:
        if word in n:
            return "Hard"

    # Indices par tournoi ATP connus. Inclut les noms FR/accents supprimés.
    for key, value in TOURNAMENT_SURFACE_HINTS.items():
        if key in n:
            return value

    return normalize_surface(fallback)


def infer_level_from_tournament(name: str) -> str:
    n = strip_accents(name or "").lower()
    for key, value in TOURNAMENT_LEVEL_HINTS.items():
        if key in n:
            return value
    return "A"


def clean_te_player_name(raw: str) -> str:
    text = normalize_space(raw)
    text = re.sub(r"\(\d+\)", " ", text)          # seed
    text = re.sub(r"\b(?:WC|Q|LL|PR|ALT)\b", " ", text, flags=re.I)
    return normalize_space(text)


def canonical_from_known_names(name: str, known_names: List[str]) -> str:
    for known in known_names:
        if same_player(name, known):
            return known
    return clean_te_player_name(name)


def parse_te_player_line(line: str, starts_with_time: bool) -> Optional[Dict[str, Any]]:
    """
    Parse une ligne TennisExplorer du type :
      11:10 Feldbausch K. 2 6 6 3.01 1.36 info
      Ofner S. (3) 0 4 0
    """
    text = normalize_space(line)
    if not text:
        return None

    if starts_with_time:
        m = re.match(r"^(\d{1,2}:\d{2}|--:--)\s+(.+)$", text)
        if not m:
            return None
        time_value = m.group(1)
        rest = m.group(2)
    else:
        time_value = ""
        rest = text

    rest = re.sub(r"\s+info\b.*$", "", rest, flags=re.I).strip()
    # Retire les cotes décimales finales.
    rest = re.sub(r"\s+\d+\.\d+\s+\d+\.\d+\s*$", "", rest).strip()
    rest = re.sub(r"\s+\d+\.\d+\s*$", "", rest).strip()

    # Les scores apparaissent sous forme d'entiers éventuellement suivis d'un tie-break ^{x}.
    score_matches = list(re.finditer(r"(?<![A-Za-z])\d{1,2}(?:\^\{\d+\})?(?![A-Za-z])", rest))
    if not score_matches:
        return None

    first_score = score_matches[0]
    name_raw = rest[:first_score.start()].strip()
    nums_raw = [m.group(0) for m in score_matches]

    if not name_raw or not nums_raw:
        return None

    sets = safe_int(re.sub(r"\^\{\d+\}", "", nums_raw[0]), -1)
    games_raw = nums_raw[1:]

    games: List[str] = []
    for item in games_raw:
        tb = re.match(r"^(\d+)(?:\^\{(\d+)\})?$", item)
        if not tb:
            continue
        if tb.group(2):
            games.append(f"{tb.group(1)}({tb.group(2)})")
        else:
            games.append(tb.group(1))

    return {
        "time": time_value,
        "name": clean_te_player_name(name_raw),
        "sets": sets,
        "games": games,
        "raw": line,
    }


def build_score_from_te(winner_line: Dict[str, Any], loser_line: Dict[str, Any]) -> str:
    wg = list(winner_line.get("games") or [])
    lg = list(loser_line.get("games") or [])
    parts = []
    for w, l in zip(wg, lg):
        if str(w) and str(l):
            parts.append(f"{w}-{l}")
    if parts:
        return " ".join(parts)
    ws = safe_int(winner_line.get("sets"), 0)
    ls = safe_int(loser_line.get("sets"), 0)
    return f"{ws}-{ls}" if ws or ls else ""


def tennis_explorer_text_lines(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    # On privilégie le texte ligne par ligne : TennisExplorer est assez stable en texte.
    text = soup.get_text("\n", strip=True)
    lines = [normalize_space(x) for x in text.splitlines()]
    return [x for x in lines if x]


def fetch_tennis_explorer_atp_singles_results(target_day: date, audit: List[str], known_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    known_names = known_names or []
    url = tennis_explorer_url_for_day(target_day)
    audit.append(f"tennisexplorer_url={url}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    try:
        response = requests.get(url, headers=headers, timeout=35)
        audit.append(f"tennisexplorer_status={response.status_code}")
        response.raise_for_status()
    except Exception as exc:
        audit.append(f"tennisexplorer_fetch_error={type(exc).__name__}: {exc}")
        return []

    lines = tennis_explorer_text_lines(response.text)
    audit.append(f"tennisexplorer_lines={len(lines)}")

    # La page peut contenir aujourd'hui + hier. On parse tout, puis on ne garde que le bloc
    # après la date cible jusqu'à la date précédente/suivante suivante.
    date_marker = f"{target_day.day:02d}. {target_day.month:02d}. {target_day.year}"
    alt_marker = f"{target_day.day}. {target_day.month:02d}. {target_day.year}"

    start_idx = None
    for idx, line in enumerate(lines):
        if date_marker in line or alt_marker in line:
            start_idx = idx
            break

    if start_idx is None:
        audit.append(f"tennisexplorer_target_date_not_found={date_marker}")
        work_lines = lines
    else:
        work_lines = []
        for line in lines[start_idx + 1:]:
            if re.search(r"\b\d{1,2}\.\s*\d{2}\.\s*\d{4}\b", line):
                break
            work_lines.append(line)

    results: List[Dict[str, Any]] = []
    current_tournament = ""
    seen = set()
    i = 0

    def is_tournament_header(line: str) -> bool:
        if re.match(r"^(\d{1,2}:\d{2}|--:--)", line):
            return False
        low = strip_accents(line).lower()
        if " s 1 2 3 4 5" in low or re.search(r"\bS\s+1\s+2\s+3\s+4\s+5\b", line):
            return True
        # Fallback : noms de tournois connus suivis d'un S.
        return bool(re.search(r"\bS\s+1\s+2\s+3", line))

    while i < len(work_lines):
        line = work_lines[i]

        if is_tournament_header(line):
            current_tournament = re.sub(r"\s+S\s+1\s+2\s+3.*$", "", line).strip()
            i += 1
            continue

        if not re.match(r"^(\d{1,2}:\d{2}|--:--)", line):
            i += 1
            continue

        if i + 1 >= len(work_lines):
            i += 1
            continue

        p1 = parse_te_player_line(work_lines[i], starts_with_time=True)
        p2 = parse_te_player_line(work_lines[i + 1], starts_with_time=False)
        i += 2

        if not p1 or not p2:
            continue

        if not tournament_is_allowed_atp_main(current_tournament):
            continue

        s1 = safe_int(p1.get("sets"), -1)
        s2 = safe_int(p2.get("sets"), -1)
        if s1 < 0 or s2 < 0 or s1 == s2 or max(s1, s2) < 2:
            continue

        winner_line = p1 if s1 > s2 else p2
        loser_line = p2 if s1 > s2 else p1
        winner = canonical_from_known_names(str(winner_line.get("name") or ""), known_names)
        loser = canonical_from_known_names(str(loser_line.get("name") or ""), known_names)

        if not winner or not loser or "/" in winner or "/" in loser:
            continue

        key = (norm_name(current_tournament), norm_name(winner), norm_name(loser), build_score_from_te(winner_line, loser_line))
        if key in seen:
            continue
        seen.add(key)

        score = build_score_from_te(winner_line, loser_line)
        results.append({
            "source": "TennisExplorer",
            "playerA": winner,
            "playerB": loser,
            "winner": winner,
            "loser": loser,
            "score": score,
            "tournament": current_tournament,
            "surface": infer_surface_from_tournament(current_tournament, "Hard"),
            "level": infer_level_from_tournament(current_tournament),
            "time": str(p1.get("time") or ""),
            "header": current_tournament,
            "raw": f"{work_lines[i-2]} || {work_lines[i-1]}",
        })

    audit.append(f"tennisexplorer_rows_kept={len(results)}")
    if results:
        audit.append("tennisexplorer_sample=" + " || ".join(
            f"{r['tournament']}: {r['winner']} d. {r['loser']} {r['score']}" for r in results[:20]
        ))
    return results


def known_player_names_from_history(rows: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    seen = set()
    for row in rows:
        for key in ("winner_name", "loser_name"):
            name = str(row.get(key) or "").strip()
            nk = norm_name(name)
            if name and nk and nk not in seen:
                seen.add(nk)
                names.append(name)
    return names


def latest_player_info_from_history(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    info: Dict[str, Dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: safe_int(r.get("tourney_date"), 0)):
        for prefix in ("winner", "loser"):
            name = str(row.get(f"{prefix}_name") or "").strip()
            nk = norm_name(name)
            if not nk:
                continue
            current = info.setdefault(nk, {"name": name})
            for field in ("id", "hand", "ht", "ioc", "age", "rank", "rank_points"):
                v = row.get(f"{prefix}_{field}")
                if v not in (None, "", 0, "0"):
                    current[field] = v
            current["name"] = name
    return info


def add_payload_player_info(payload_matches: List[Dict[str, Any]], info: Dict[str, Dict[str, Any]]) -> None:
    for match in payload_matches:
        ctx = payload_context(match)
        for name in (ctx.get("sourceA"), ctx.get("sourceB")):
            nk = norm_name(str(name or ""))
            if nk and name:
                info.setdefault(nk, {"name": str(name)})
        for nk, pts in (ctx.get("pointsByName") or {}).items():
            if pts:
                info.setdefault(nk, {"name": nk})["rank_points"] = str(pts)
        for nk, rk in (ctx.get("rankByName") or {}).items():
            if rk:
                info.setdefault(nk, {"name": nk})["rank"] = str(rk)


def find_player_info(name: str, info: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    nk = norm_name(name)
    if nk in info:
        return info[nk]
    for key, value in info.items():
        if same_player(name, str(value.get("name") or key)):
            return value
    return {"name": name}


def make_history_row_from_result(
    fieldnames: List[str],
    result: Dict[str, Any],
    target_day: date,
    match_num: int,
    player_info: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    winner = str(result.get("winner") or result.get("playerA") or "").strip()
    loser = str(result.get("loser") or result.get("playerB") or "").strip()
    winfo = find_player_info(winner, player_info)
    linfo = find_player_info(loser, player_info)

    base: Dict[str, Any] = {key: "" for key in fieldnames}
    tourney_name = str(result.get("tournament") or "ATP Daily Completed").strip() or "ATP Daily Completed"
    tourney_level = str(result.get("level") or infer_level_from_tournament(tourney_name) or "A")
    surface = normalize_surface(result.get("surface") or infer_surface_from_tournament(tourney_name, "Hard"))

    values = {
        "tourney_id": f"{target_day.year}-TE-{target_day.strftime('%m%d')}-{norm_name(tourney_name).replace(' ', '-')[:18]}",
        "tourney_name": tourney_name,
        "surface": surface,
        "draw_size": "",
        "tourney_level": tourney_level,
        "indoor": "",
        "tourney_date": str(ymd_int(target_day)),
        "match_num": str(match_num),
        "winner_id": str(winfo.get("id") or ""),
        "winner_seed": "",
        "winner_entry": "",
        "winner_name": winner,
        "winner_hand": str(winfo.get("hand") or ""),
        "winner_ht": str(winfo.get("ht") or ""),
        "winner_ioc": str(winfo.get("ioc") or ""),
        "winner_age": str(winfo.get("age") or ""),
        "winner_rank": str(winfo.get("rank") or ""),
        "winner_rank_points": str(winfo.get("rank_points") or ""),
        "loser_id": str(linfo.get("id") or ""),
        "loser_seed": "",
        "loser_entry": "",
        "loser_name": loser,
        "loser_hand": str(linfo.get("hand") or ""),
        "loser_ht": str(linfo.get("ht") or ""),
        "loser_ioc": str(linfo.get("ioc") or ""),
        "loser_age": str(linfo.get("age") or ""),
        "loser_rank": str(linfo.get("rank") or ""),
        "loser_rank_points": str(linfo.get("rank_points") or ""),
        "score": str(result.get("score") or ""),
        "best_of": "3",
        "round": "",
        "minutes": "",
        "w_ace": "",
        "w_df": "",
        "w_svpt": "",
        "w_1stIn": "",
        "w_1stWon": "",
        "w_2ndWon": "",
        "w_SvGms": "",
        "w_bpSaved": "",
        "w_bpFaced": "",
        "l_ace": "",
        "l_df": "",
        "l_svpt": "",
        "l_1stIn": "",
        "l_1stWon": "",
        "l_2ndWon": "",
        "l_SvGms": "",
        "l_bpSaved": "",
        "l_bpFaced": "",
    }

    for key, value in values.items():
        if key in base:
            base[key] = value
    return base


# ---------------------------------------------------------------------------
# CSV 2026
# ---------------------------------------------------------------------------

def read_history_fieldnames() -> List[str]:
    if HISTORY_PATH.exists():
        try:
            with HISTORY_PATH.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    return list(reader.fieldnames)
        except Exception:
            pass
    return list(STANDARD_COLUMNS)


def read_existing_rows(fieldnames: List[str]) -> List[Dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []

    rows: List[Dict[str, Any]] = []
    with HISTORY_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            clean = {key: row.get(key, "") for key in fieldnames}
            rows.append(clean)
    return rows


def existing_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    d = str(row.get("tourney_date") or "")
    w = norm_name(str(row.get("winner_name") or ""))
    l = norm_name(str(row.get("loser_name") or ""))
    pair = "||".join(sorted([w, l]))
    return (d, pair, str(row.get("score") or ""))


def pair_day_key(row: Dict[str, Any]) -> Tuple[str, str]:
    d = str(row.get("tourney_date") or "")
    w = norm_name(str(row.get("winner_name") or ""))
    l = norm_name(str(row.get("loser_name") or ""))
    pair = "||".join(sorted([w, l]))
    return (d, pair)


def next_match_num(existing_rows: List[Dict[str, Any]], target_day: date) -> int:
    day_int = ymd_int(target_day)
    nums = [
        safe_int(row.get("match_num"), 0)
        for row in existing_rows
        if safe_int(row.get("tourney_date"), 0) == day_int
    ]
    if nums:
        return max(nums) + 1

    # Numéro stable et triable.
    # Exemple : 2026051401
    return day_int * 100 + 1


def make_history_row(
    fieldnames: List[str],
    ctx: Dict[str, Any],
    completed_row: Dict[str, Any],
    target_day: date,
    match_num: int,
) -> Dict[str, Any]:
    source_a = str(ctx.get("sourceA") or "")
    source_b = str(ctx.get("sourceB") or "")
    real_winner_fs = str(completed_row.get("winner") or "")

    if same_player(real_winner_fs, source_a):
        winner = source_a
        loser = source_b
    elif same_player(real_winner_fs, source_b):
        winner = source_b
        loser = source_a
    else:
        # Fallback : garder le nom Flashscore du gagnant, mais seulement si paire matchée.
        winner = real_winner_fs
        loser = source_b if same_player(real_winner_fs, source_a) else source_a

    points_by_name: Dict[str, int] = ctx.get("pointsByName") or {}
    rank_by_name: Dict[str, int] = ctx.get("rankByName") or {}

    winner_key = norm_name(winner)
    loser_key = norm_name(loser)

    score = score_from_completed_row(completed_row, real_winner_fs) or str(completed_row.get("score") or "")

    base: Dict[str, Any] = {key: "" for key in fieldnames}

    values = {
        "tourney_id": f"{target_day.year}-TM-{target_day.strftime('%m%d')}",
        "tourney_name": "ATP Daily Completed",
        "surface": normalize_surface(ctx.get("surface")),
        "draw_size": "",
        "tourney_level": "A",
        "tourney_date": str(ymd_int(target_day)),
        "match_num": str(match_num),
        "winner_id": "",
        "winner_seed": "",
        "winner_entry": "",
        "winner_name": winner,
        "winner_hand": "",
        "winner_ht": "",
        "winner_ioc": "",
        "winner_age": "",
        "loser_id": "",
        "loser_seed": "",
        "loser_entry": "",
        "loser_name": loser,
        "loser_hand": "",
        "loser_ht": "",
        "loser_ioc": "",
        "loser_age": "",
        "score": score,
        "best_of": "3",
        "round": "R128",
        "minutes": "",
        "w_ace": "0",
        "w_df": "0",
        "w_svpt": "0",
        "w_1stIn": "0",
        "w_1stWon": "0",
        "w_2ndWon": "0",
        "w_SvGms": "0",
        "w_bpSaved": "0",
        "w_bpFaced": "0",
        "l_ace": "0",
        "l_df": "0",
        "l_svpt": "0",
        "l_1stIn": "0",
        "l_1stWon": "0",
        "l_2ndWon": "0",
        "l_SvGms": "0",
        "l_bpSaved": "0",
        "l_bpFaced": "0",
        "winner_rank": str(rank_by_name.get(winner_key, 0) or ""),
        "winner_rank_points": str(points_by_name.get(winner_key, 0) or ""),
        "loser_rank": str(rank_by_name.get(loser_key, 0) or ""),
        "loser_rank_points": str(points_by_name.get(loser_key, 0) or ""),
    }

    for key, value in values.items():
        if key in base:
            base[key] = value

    return base


def backup_history_if_needed() -> str:
    if not HISTORY_PATH.exists():
        return ""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DATA_DIR / f"2026_backup_before_auto_update_{stamp}.csv"
    shutil.copyfile(HISTORY_PATH, backup)
    return str(backup)


def write_history(fieldnames: List[str], rows: List[Dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    def sort_key(row: Dict[str, Any]) -> Tuple[int, int, str, str]:
        return (
            safe_int(row.get("tourney_date"), 0),
            safe_int(row.get("match_num"), 0),
            norm_name(str(row.get("winner_name") or "")),
            norm_name(str(row.get("loser_name") or "")),
        )

    rows.sort(key=sort_key)

    with HISTORY_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})



# ---------------------------------------------------------------------------
# Historique premiums Unity : settlement pending -> win/loss
# ---------------------------------------------------------------------------

def _parse_iso_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _is_pending_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"", "pending", "open", "en attente"}


def _premium_row_players(row: Dict[str, Any]) -> Tuple[str, str, str]:
    source_a = str(get_first(row, ["sourcePlayerA", "playerA", "source_player_a"], "") or "").strip()
    source_b = str(get_first(row, ["sourcePlayerB", "opponent", "playerB", "source_player_b"], "") or "").strip()
    predicted = str(get_first(row, ["predictedWinner", "pick", "winner", "playerA"], "") or "").strip()
    if predicted and source_b and not source_a:
        source_a = predicted
    return source_a, source_b, predicted


def _find_completed_for_history_row(row: Dict[str, Any], completed_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    source_a, source_b, predicted = _premium_row_players(row)
    if source_a and source_b:
        found = find_completed_for_payload(source_a, source_b, completed_rows)
        if found:
            return found
    opponent = str(get_first(row, ["opponent", "sourcePlayerB", "playerB"], "") or "").strip()
    if predicted and opponent:
        found = find_completed_for_payload(predicted, opponent, completed_rows)
        if found:
            return found
    return None


def _rebuild_premium_summary(history: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(history) if isinstance(history, dict) else {}
    out["rows"] = rows

    settled_rows = [r for r in rows if str(r.get("result", "")).lower() in {"win", "loss"}]
    wins = sum(1 for r in settled_rows if str(r.get("result", "")).lower() == "win")
    losses = sum(1 for r in settled_rows if str(r.get("result", "")).lower() == "loss")
    pending = sum(1 for r in rows if _is_pending_value(r.get("result")))

    summary = dict(out.get("summary") or {}) if isinstance(out.get("summary"), dict) else {}
    stake_eur = safe_int(summary.get("stakeEur", 100), 100)

    by_day: Dict[str, Dict[str, Any]] = {}
    total_profit_units = 0.0

    for r in rows:
        d = str(r.get("date") or "")[:10]
        if not d:
            continue

        bucket = by_day.setdefault(d, {
            "date": d,
            "wins": 0,
            "losses": 0,
            "settled": 0,
            "pending": 0,
            "profitUnits": 0.0,
            "profitEur": 0.0,
            "winRate": 0.0,
            "hadPremiumToday": True,
            "hadPremiumSettledToday": False,
        })

        res = str(r.get("result") or "").lower()
        if res == "win":
            bucket["wins"] += 1
            bucket["settled"] += 1
            bucket["hadPremiumSettledToday"] = True
            odd = str(get_first(r, ["oddPredicted", "playerAOdd", "oddA", "coteA"], "") or "").replace(",", ".")
            try:
                profit = max(float(odd) - 1.0, 0.0)
            except Exception:
                profit = 0.0
            bucket["profitUnits"] += profit
            total_profit_units += profit
        elif res == "loss":
            bucket["losses"] += 1
            bucket["settled"] += 1
            bucket["hadPremiumSettledToday"] = True
            bucket["profitUnits"] -= 1.0
            total_profit_units -= 1.0
        else:
            bucket["pending"] += 1

    days: List[Dict[str, Any]] = []
    cumulative_days: List[Dict[str, Any]] = []
    cum_wins = 0
    cum_losses = 0
    cum_profit = 0.0

    for d in sorted(by_day.keys()):
        b = by_day[d]
        b["profitUnits"] = round(float(b["profitUnits"]), 4)
        b["profitEur"] = round(float(b["profitUnits"]) * stake_eur, 2)
        b["winRate"] = round((b["wins"] / b["settled"] * 100.0), 2) if b["settled"] else 0.0
        days.append(b)

        cum_wins += int(b["wins"])
        cum_losses += int(b["losses"])
        cum_profit += float(b["profitUnits"])
        cum_settled = cum_wins + cum_losses
        cumulative_days.append({
            "date": d,
            "cumulativeWins": cum_wins,
            "cumulativeLosses": cum_losses,
            "cumulativeSettled": cum_settled,
            "cumulativeWinRate": round((cum_wins / cum_settled * 100.0), 2) if cum_settled else 0.0,
            "cumulativeProfitUnits": round(cum_profit, 4),
            "cumulativeProfitEur": round(cum_profit * stake_eur, 2),
            "pendingThatDay": int(b["pending"]),
            "hadPremiumToday": bool(b["hadPremiumToday"]),
            "hadPremiumSettledToday": bool(b["hadPremiumSettledToday"]),
        })

    settled_count = wins + losses
    summary.update({
        "total": len(rows),
        "wins": wins,
        "losses": losses,
        "settled": settled_count,
        "pending": pending,
        "winRate": round((wins / settled_count * 100.0), 2) if settled_count else 0.0,
        "profitUnits": round(total_profit_units, 4),
        "profitEur": round(total_profit_units * stake_eur, 2),
        "days": days,
        "cumulativeDays": cumulative_days,
        "description": "days = jour par jour ; cumulativeDays = courbe cumulée qui ne repart jamais à zéro.",
        "stakeEur": stake_eur,
        "euroAxisMin": summary.get("euroAxisMin", -2000.0),
        "euroAxisMax": summary.get("euroAxisMax", 2000.0),
        "winRateAxisMin": summary.get("winRateAxisMin", 0.0),
        "winRateAxisMax": summary.get("winRateAxisMax", 100.0),
    })
    out["summary"] = summary
    return out


def premium_pending_dates(max_day: date, audit: List[str], lookback_days: int = 14) -> List[date]:
    """
    Retourne TOUTES les dates qui ont encore au moins un premium en pending.

    Ancien bug : le script ne vérifiait que la date courante / target_day,
    donc les premiums de J-1, J-2, etc. restaient bloqués en pending dans Unity.

    Règle corrigée : on relit output/premium_history.json et on récupère les dates
    directement depuis les lignes result=pending. On ignore seulement les dates futures.
    Le paramètre lookback_days est conservé pour compatibilité, mais il n'est plus
    utilisé pour bloquer les anciens pending.
    """
    if not PREMIUM_HISTORY_PATH.exists():
        audit.append(f"premium_history_missing={PREMIUM_HISTORY_PATH}")
        return []

    try:
        history = json.loads(PREMIUM_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        audit.append(f"premium_history_read_error={type(exc).__name__}: {exc}")
        return []

    # Accepte les deux formats possibles de premium_history.json :
    # 1) objet normal : {"summary": {...}, "rows": [...]}
    # 2) ancienne sauvegarde / format brut : [{...}, {...}]
    if isinstance(history, dict):
        rows = history.get("rows")
    elif isinstance(history, list):
        rows = history
        audit.append("premium_history_root=list_accepted")
    else:
        rows = None

    if not isinstance(rows, list):
        audit.append("premium_history_rows_invalid")
        return []

    today = paris_today()
    dates = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _is_pending_value(row.get("result")):
            continue
        d = _parse_iso_date(row.get("date"))
        if d and d <= today:
            dates.add(d)

    out = sorted(dates)
    audit.append("premium_pending_dates_all=" + ",".join(x.isoformat() for x in out))
    return out


def settle_premium_history(completed_by_day: Dict[str, List[Dict[str, Any]]], audit: List[str]) -> Dict[str, Any]:
    if not PREMIUM_HISTORY_PATH.exists():
        return {"status": "missing", "path": str(PREMIUM_HISTORY_PATH), "settled": 0}

    try:
        history = json.loads(PREMIUM_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        audit.append(f"premium_history_settle_read_error={type(exc).__name__}: {exc}")
        return {"status": "read_error", "error": f"{type(exc).__name__}: {exc}", "settled": 0}

    original_root_is_list = False

    # Accepte les deux formats possibles :
    # - objet : {"summary": {...}, "rows": [...]}
    # - liste brute : [{...}, {...}]
    # Ancien bug : une liste brute déclenchait invalid_json_root et bloquait Unity.
    if isinstance(history, dict):
        rows = history.get("rows")
        if not isinstance(rows, list):
            return {"status": "missing_rows", "settled": 0}
    elif isinstance(history, list):
        rows = history
        history = {"rows": rows, "summary": {}}
        original_root_is_list = True
        audit.append("premium_history_root=list_accepted_for_settle")
    else:
        return {"status": "invalid_json_root", "rootType": type(history).__name__, "settled": 0}

    settled = 0
    checked = 0
    samples: List[str] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _is_pending_value(row.get("result")):
            continue

        d = _parse_iso_date(row.get("date"))
        if not d:
            continue

        day_key = d.isoformat()
        completed_rows = completed_by_day.get(day_key) or []
        if not completed_rows:
            continue

        checked += 1
        found = _find_completed_for_history_row(row, completed_rows)
        if not found:
            continue

        real_winner = str(found.get("winner") or "").strip()
        if not real_winner:
            continue

        predicted = str(get_first(row, ["predictedWinner", "pick", "playerA"], "") or "").strip()
        if not predicted:
            continue

        row["result"] = "win" if same_player(real_winner, predicted) else "loss"
        row["realWinner"] = real_winner
        row["settledAt"] = day_key
        row["settledSource"] = "Flashscore"
        row["settledScore"] = str(found.get("score") or "")
        settled += 1

        if len(samples) < 10:
            samples.append(f"{day_key}: predicted={predicted} realWinner={real_winner} result={row['result']}")

    if settled:
        backup = PREMIUM_HISTORY_PATH.with_name(
            f"premium_history_backup_before_settle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        try:
            shutil.copyfile(PREMIUM_HISTORY_PATH, backup)
        except Exception as exc:
            audit.append(f"premium_history_backup_error={type(exc).__name__}: {exc}")

        rebuilt = _rebuild_premium_summary(history, rows)

        # Préserve le format existant du fichier principal pour ne pas casser Unity :
        # si premium_history.json était une liste brute, on réécrit une liste brute mise à jour.
        # Le résumé complet est quand même écrit dans premium_history_summary.json.
        if original_root_is_list:
            PREMIUM_HISTORY_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            output_format = "list"
        else:
            PREMIUM_HISTORY_PATH.write_text(json.dumps(rebuilt, ensure_ascii=False, indent=2), encoding="utf-8")
            output_format = "dict"

        PREMIUM_HISTORY_SUMMARY_PATH.write_text(json.dumps(rebuilt.get("summary", {}), ensure_ascii=False, indent=2), encoding="utf-8")
        audit.append(f"premium_history_settled={settled}")
        return {
            "status": "ok",
            "path": str(PREMIUM_HISTORY_PATH),
            "summaryPath": str(PREMIUM_HISTORY_SUMMARY_PATH),
            "settled": settled,
            "checkedPendingRows": checked,
            "sample": samples,
            "backupPath": str(backup),
            "format": output_format,
        }

    return {
        "status": "ok",
        "path": str(PREMIUM_HISTORY_PATH),
        "settled": 0,
        "checkedPendingRows": checked,
        "sample": samples,
    }



def latest_history_date(existing_rows: List[Dict[str, Any]]) -> Optional[date]:
    """Retourne la dernière date réellement présente dans data/2026.csv."""
    best: Optional[date] = None
    for row in existing_rows:
        raw = str(row.get("tourney_date") or "").strip()
        if not re.fullmatch(r"\d{8}", raw):
            continue
        try:
            d = datetime.strptime(raw, "%Y%m%d").date()
        except Exception:
            continue
        if best is None or d > best:
            best = d
    return best


def resolve_sync_days(existing_rows: List[Dict[str, Any]], audit: List[str]) -> List[date]:
    """
    Mode propre pour Elo 2026 : synchroniser depuis la dernière date du CSV
    jusqu'à hier inclus.

    Si UPDATE_2026_TARGET_DATE est défini, on garde le mode test manuel
    sur une seule date.
    """
    raw = os.getenv("UPDATE_2026_TARGET_DATE", "").strip()
    if raw:
        d = date.fromisoformat(raw)
        audit.append(f"sync_mode=single_env_target date={d.isoformat()}")
        return [d]

    today = paris_today()
    end_day = today - timedelta(days=1)
    last_day = latest_history_date(existing_rows)

    if last_day is None:
        start_day = end_day
        audit.append("sync_mode=no_history_fallback_yesterday")
    elif last_day >= end_day:
        # Historique Elo déjà synchronisé au moins jusqu'à hier.
        # Ne pas produire une plage incohérente du type start > end :
        # app.py doit pouvoir marquer le run comme proprement terminé.
        audit.append(
            f"sync_mode=history_already_current last_csv_date={last_day.isoformat()} end={end_day.isoformat()}"
        )
        audit.append("sync_days=none_history_already_current")
        return []
    else:
        start_day = last_day + timedelta(days=1)
        audit.append(
            f"sync_mode=from_last_csv_date last_csv_date={last_day.isoformat()} "
            f"start={start_day.isoformat()} end={end_day.isoformat()}"
        )

    days: List[date] = []
    d = start_day
    while d <= end_day:
        days.append(d)
        d += timedelta(days=1)

    # Sécurité anti-run massif involontaire : on limite par défaut aux 45 derniers jours.
    # Tu peux augmenter avec UPDATE_2026_MAX_SYNC_DAYS.
    max_days = int(os.getenv("UPDATE_2026_MAX_SYNC_DAYS", "45"))
    if len(days) > max_days:
        audit.append(f"sync_days_truncated original={len(days)} max={max_days}")
        days = days[-max_days:]

    audit.append("sync_days=" + ",".join(x.isoformat() for x in days))
    return days


def source_rows_from_flashscore_rows(
    flash_rows: List[Dict[str, Any]],
    known_names: List[str],
) -> List[Dict[str, Any]]:
    source_rows: List[Dict[str, Any]] = []
    for row in flash_rows:
        tournament = str(row.get("header") or "ATP Daily Completed")
        if not looks_like_atp_singles_header(tournament):
            continue
        winner = str(row.get("winner") or "").strip()
        fs_a = str(row.get("playerA") or "").strip()
        fs_b = str(row.get("playerB") or "").strip()
        loser = fs_b if same_player(winner, fs_a) else fs_a
        if not winner or not loser:
            continue
        source_rows.append({
            "source": "Flashscore",
            "playerA": winner,
            "playerB": loser,
            "winner": canonical_from_known_names(winner, known_names),
            "loser": canonical_from_known_names(loser, known_names),
            "score": str(row.get("score") or score_from_completed_row(row, winner) or ""),
            "tournament": tournament if tournament else "ATP Daily Completed",
            "surface": infer_surface_from_tournament(tournament, "Hard"),
            "level": infer_level_from_tournament(tournament),
            "header": tournament,
            "raw": str(row.get("raw") or ""),
        })
    return source_rows


def fetch_source_rows_for_day(
    target_day: date,
    audit: List[str],
    known_names: List[str],
) -> Tuple[List[Dict[str, Any]], str, List[Dict[str, Any]]]:
    """Récupère les résultats ATP simples terminés pour une date donnée."""
    audit.append(f"--- sync_day_start={target_day.isoformat()} ---")

    payload_matches = load_target_payload(target_day, audit)

    source_rows = fetch_tennis_explorer_atp_singles_results(target_day, audit, known_names=known_names)
    source_name = "TennisExplorer"

    if not source_rows:
        audit.append(f"tennisexplorer_empty_fallback_flashscore=on date={target_day.isoformat()}")
        try:
            flash_rows = fetch_flashscore_completed_results(target_day, audit)
        except Exception as exc:
            flash_rows = []
            audit.append(f"flashscore_global_error date={target_day.isoformat()} {type(exc).__name__}: {exc}")
        source_rows = source_rows_from_flashscore_rows(flash_rows, known_names)
        source_name = "FlashscoreFallback"

    audit.append(f"sync_day_done={target_day.isoformat()} source={source_name} source_rows={len(source_rows)} payload_rows={len(payload_matches)}")
    return source_rows, source_name, payload_matches

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    _setup_stdout()

    audit: List[str] = []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    audit.append("source_policy=sync_from_last_csv_date_atp_singles_results")
    audit.append("primary_source=TennisExplorer")
    audit.append("fallback_source=Flashscore_text_scroll")
    audit.append("payload_policy=secondary_for_player_points_only")
    audit.append("jeff_sackmann=disabled")
    audit.append("non_completed_today_policy=reject")

    fieldnames = read_history_fieldnames()
    for col in STANDARD_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)

    existing_rows = read_existing_rows(fieldnames)
    existing_rows_before = len(existing_rows)
    known_names = known_player_names_from_history(existing_rows)
    player_info = latest_player_info_from_history(existing_rows)

    sync_days = resolve_sync_days(existing_rows, audit)

    seen_pair_day = {pair_day_key(row) for row in existing_rows}
    added_rows: List[Dict[str, Any]] = []
    skipped_duplicate = 0
    skipped_bad_source = 0
    total_source_rows = 0
    total_payload_rows = 0
    sources_by_day: Dict[str, str] = {}
    source_rows_by_day: Dict[str, int] = {}
    added_by_day: Dict[str, int] = {}
    completed_by_day: Dict[str, List[Dict[str, Any]]] = {}

    for target_day in sync_days:
        source_rows, source_name, payload_matches = fetch_source_rows_for_day(target_day, audit, known_names)
        add_payload_player_info(payload_matches, player_info)

        day_key = target_day.isoformat()
        sources_by_day[day_key] = source_name
        source_rows_by_day[day_key] = len(source_rows)
        total_source_rows += len(source_rows)
        total_payload_rows += len(payload_matches)
        completed_by_day[day_key] = source_rows

        match_num = next_match_num(existing_rows + added_rows, target_day)
        day_added = 0

        for result_row in source_rows:
            winner = str(result_row.get("winner") or "").strip()
            loser = str(result_row.get("loser") or result_row.get("playerB") or "").strip()
            score = str(result_row.get("score") or "").strip()
            tournament = str(result_row.get("tournament") or "").strip()

            if not winner or not loser or not score or not tournament:
                skipped_bad_source += 1
                continue
            if "/" in winner or "/" in loser:
                skipped_bad_source += 1
                continue
            if not tournament_is_allowed_atp_main(tournament):
                skipped_bad_source += 1
                continue

            temp_row = make_history_row_from_result(fieldnames, result_row, target_day, match_num, player_info)
            key = pair_day_key(temp_row)

            if key in seen_pair_day:
                skipped_duplicate += 1
                continue

            seen_pair_day.add(key)
            added_rows.append(temp_row)
            day_added += 1
            match_num += 1

            # Les lignes ajoutées peuvent enrichir légèrement le mapping points/rangs
            # pour les dates suivantes du même run.
            for side in ("winner", "loser"):
                nm = str(temp_row.get(f"{side}_name") or "").strip()
                if nm:
                    player_info[norm_name(nm)] = {
                        "rank": str(temp_row.get(f"{side}_rank") or ""),
                        "rank_points": str(temp_row.get(f"{side}_rank_points") or ""),
                    }
                    known_names.append(nm)

        added_by_day[day_key] = day_added
        audit.append(f"sync_day_added={day_key} added={day_added}")

    # Même si aucun jour Elo n'est à synchroniser, on règle l'historique premium
    # en relisant toutes les dates encore pending.
    base_for_pending = paris_today() - timedelta(days=1)
    for pending_day in premium_pending_dates(base_for_pending, audit, lookback_days=21):
        day_key = pending_day.isoformat()
        if day_key in completed_by_day:
            continue
        rows_for_pending, _source_name, _payload = fetch_source_rows_for_day(pending_day, audit, known_names)
        completed_by_day[day_key] = rows_for_pending

    premium_settlement = settle_premium_history(completed_by_day, audit)
    dates_checked = sorted(completed_by_day.keys())
    audit.append("dates_checked=" + ",".join(dates_checked))

    backup_path = ""
    if added_rows:
        backup_path = backup_history_if_needed()
        write_history(fieldnames, existing_rows + added_rows)

    result = {
        "status": "ok",
        "mode": "sync_from_last_csv_date",
        "historyPath": str(HISTORY_PATH),
        "backupPath": backup_path,
        "syncDays": [d.isoformat() for d in sync_days],
        "datesChecked": dates_checked,
        "sourcesByDay": sources_by_day,
        "sourceRowsByDay": source_rows_by_day,
        "addedByDay": added_by_day,
        "sourceRows": total_source_rows,
        "completedRows": total_source_rows,
        "payloadRows": total_payload_rows,
        "historyAlreadyCurrent": len(sync_days) == 0,
        "existingRowsBefore": existing_rows_before,
        "addedRows": len(added_rows),
        "premiumSettlement": premium_settlement,
        "skippedDuplicate": skipped_duplicate,
        "skippedBadSource": skipped_bad_source,
        "finalRows": existing_rows_before + len(added_rows),
        "addedSample": [
            {
                "tourney_date": row.get("tourney_date"),
                "tourney_name": row.get("tourney_name"),
                "winner_name": row.get("winner_name"),
                "loser_name": row.get("loser_name"),
                "surface": row.get("surface"),
                "score": row.get("score"),
                "winner_rank_points": row.get("winner_rank_points"),
                "loser_rank_points": row.get("loser_rank_points"),
            }
            for row in added_rows[:50]
        ],
        "audit": audit,
    }

    AUDIT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("✅ update_2026_history terminé")
    print("mode=sync_from_last_csv_date")
    print(f"history={HISTORY_PATH}")
    if backup_path:
        print(f"backup={backup_path}")
    print("sync_days=" + ",".join(d.isoformat() for d in sync_days))
    print(f"source_rows={total_source_rows}")
    print(f"payload_rows={total_payload_rows}")
    print(f"added_rows={len(added_rows)}")
    print(f"premium_history_settled={premium_settlement.get('settled', 0)}")
    print(f"skipped_duplicate={skipped_duplicate}")
    print(f"final_rows={existing_rows_before + len(added_rows)}")
    print(f"a_done={AUDIT_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
