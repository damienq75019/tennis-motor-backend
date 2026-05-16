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
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def fetch_flashscore_completed_results(target_day: date, audit: List[str]) -> List[Dict[str, Any]]:
    """
    Lit Flashscore avec Playwright et récupère les matchs terminés.
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
            viewport={"width": 1365, "height": 1800},
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

                click_optional(page, ["J'accepte", "Tout refuser", "Accepter", "OK"], timeout_ms=2500)

                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass

                clicked = click_optional(page, ["TERMINÉS", "Terminés", "Terminé", "Finished", "Results", "Résultats"], timeout_ms=4000)
                audit.append(f"completed_tab_clicked={clicked}")

                try:
                    page.wait_for_timeout(2500)
                except Exception:
                    pass

                scroll_until_stable(page, audit, max_rounds=10)

                rows = page.evaluate(completed_results_js())
                audit.append(f"flashscore_rows_raw={len(rows)}")

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

                    winner = winner_from_completed_row(row)
                    if not winner:
                        continue

                    key = (norm_name(a), norm_name(b), str(row.get("raw") or "")[:80])
                    if key in seen:
                        continue
                    seen.add(key)

                    clean = {
                        "playerA": a,
                        "playerB": b,
                        "winner": winner,
                        "score": score_from_completed_row(row, winner),
                        "homeScores": int_list(row.get("homeScores")),
                        "awayScores": int_list(row.get("awayScores")),
                        "status": str(row.get("status") or ""),
                        "header": str(row.get("header") or "")[:250],
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
        for row in all_rows[:15]:
            sample.append(
                f"{row.get('playerA')} - {row.get('playerB')} "
                f"winner={row.get('winner')} score={row.get('score')}"
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    _setup_stdout()

    audit: List[str] = []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    target_day = resolve_target_day()
    audit.append(f"target_day={target_day.isoformat()}")
    audit.append("source_policy=payload_daily_yesterday_plus_flashscore_completed")
    audit.append("jeff_sackmann=disabled")
    audit.append("non_completed_today_policy=reject")

    payload_matches = load_target_payload(target_day, audit)

    if not payload_matches:
        result = {
            "status": "ok",
            "targetDay": target_day.isoformat(),
            "message": "Aucun payload Tennis Motor trouvé pour la veille. Rien ajouté à 2026.csv.",
            "addedRows": 0,
            "audit": audit,
        }
        AUDIT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("✅ update_2026_history terminé sans ajout")
        print("reason=no_payload_for_target_day")
        print(f"target_day={target_day.isoformat()}")
        print(f"audit={AUDIT_PATH}")
        return 0

    completed_by_day: Dict[str, List[Dict[str, Any]]] = {}

    try:
        completed_rows = fetch_flashscore_completed_results(target_day, audit)
    except Exception as exc:
        completed_rows = []
        audit.append(f"flashscore_global_error={type(exc).__name__}: {exc}")

    completed_by_day[target_day.isoformat()] = completed_rows

    # L'historique Unity peut avoir des premiums pending depuis 1 à 14 jours.
    # On corrige seulement premium_history.json, sans toucher au daily 10k.
    for pending_day in premium_pending_dates(target_day, audit, lookback_days=14):
        day_key = pending_day.isoformat()
        if day_key in completed_by_day:
            continue
        try:
            completed_by_day[day_key] = fetch_flashscore_completed_results(pending_day, audit)
        except Exception as exc:
            completed_by_day[day_key] = []
            audit.append(f"flashscore_pending_day_error date={day_key} error={type(exc).__name__}: {exc}")

    premium_settlement = settle_premium_history(completed_by_day, audit)
    dates_checked = sorted(completed_by_day.keys())
    audit.append("dates_checked=" + ",".join(dates_checked))

    if not completed_rows:
        result = {
            "status": "ok",
            "targetDay": target_day.isoformat(),
            "message": "Flashscore n'a donné aucun match terminé exploitable pour 2026.csv. Historique premium traité séparément.",
            "payloadRows": len(payload_matches),
            "completedRows": 0,
            "addedRows": 0,
            "premiumSettlement": premium_settlement,
            "datesChecked": dates_checked,
            "audit": audit,
        }
        AUDIT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("✅ update_2026_history terminé sans ajout CSV")
        print("reason=no_completed_flashscore_rows_for_target_day")
        print(f"payload_rows={len(payload_matches)}")
        print(f"premium_history_settled={premium_settlement.get('settled', 0)}")
        print(f"dates_checked={','.join(dates_checked)}")
        print(f"audit={AUDIT_PATH}")
        return 0

    fieldnames = read_history_fieldnames()

    # Sécurité : s'assurer que les colonnes minimales existent.
    for col in STANDARD_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)

    existing_rows = read_existing_rows(fieldnames)
    seen_pair_day = {pair_day_key(row) for row in existing_rows}

    added_rows: List[Dict[str, Any]] = []
    skipped_not_finished = 0
    skipped_duplicate = 0
    skipped_not_found = 0
    skipped_bad_payload = 0

    match_num = next_match_num(existing_rows, target_day)

    for match in payload_matches:
        ctx = payload_context(match)
        source_a = str(ctx.get("sourceA") or "").strip()
        source_b = str(ctx.get("sourceB") or "").strip()

        if not source_a or not source_b or "/" in source_a or "/" in source_b:
            skipped_bad_payload += 1
            continue

        found = find_completed_for_payload(source_a, source_b, completed_rows)
        if not found:
            skipped_not_found += 1
            continue

        winner = str(found.get("winner") or "")
        if not winner:
            skipped_not_finished += 1
            continue

        temp_row = make_history_row(fieldnames, ctx, found, target_day, match_num)
        key = pair_day_key(temp_row)

        if key in seen_pair_day:
            skipped_duplicate += 1
            continue

        seen_pair_day.add(key)
        added_rows.append(temp_row)
        match_num += 1

    backup_path = ""
    if added_rows:
        backup_path = backup_history_if_needed()
        write_history(fieldnames, existing_rows + added_rows)

    result = {
        "status": "ok",
        "targetDay": target_day.isoformat(),
        "historyPath": str(HISTORY_PATH),
        "backupPath": backup_path,
        "payloadRows": len(payload_matches),
        "completedRows": len(completed_rows),
        "existingRowsBefore": len(existing_rows),
        "addedRows": len(added_rows),
        "premiumSettlement": premium_settlement,
        "skippedNotFoundOnFlashscore": skipped_not_found,
        "skippedNotFinished": skipped_not_finished,
        "skippedDuplicate": skipped_duplicate,
        "skippedBadPayload": skipped_bad_payload,
        "finalRows": len(existing_rows) + len(added_rows),
        "addedSample": [
            {
                "winner_name": row.get("winner_name"),
                "loser_name": row.get("loser_name"),
                "surface": row.get("surface"),
                "score": row.get("score"),
                "winner_rank_points": row.get("winner_rank_points"),
                "loser_rank_points": row.get("loser_rank_points"),
            }
            for row in added_rows[:20]
        ],
        "audit": audit,
    }

    AUDIT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("✅ update_2026_history terminé")
    print(f"target_day={target_day.isoformat()}")
    print(f"history={HISTORY_PATH}")
    if backup_path:
        print(f"backup={backup_path}")
    print(f"payload_rows={len(payload_matches)}")
    print(f"flashscore_completed_rows={len(completed_rows)}")
    print(f"added_rows={len(added_rows)}")
    print(f"premium_history_settled={premium_settlement.get('settled', 0)}")
    print(f"skipped_not_found={skipped_not_found}")
    print(f"skipped_duplicate={skipped_duplicate}")
    print(f"audit={AUDIT_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
