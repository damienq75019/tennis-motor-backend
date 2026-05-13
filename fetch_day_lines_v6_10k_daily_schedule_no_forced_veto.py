#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - DAILY ATP SINGLES FETCHER FINAL CLEAN

Objectif unique :
    Trouver les matchs ATP simples du jour demandé, aujourd'hui/demain/YYYY-MM-DD.

Règle verrouillée :
    1. Source principale : ATP daily-schedule officiel.
    2. Si ATP retourne 0 match exploitable : fallback Flashscore ATP - SIMPLE / ATP - SINGLES uniquement.
    3. Jamais WTA.
    4. Jamais doubles.
    5. Jamais équipes avec "/".
    6. Points ATP introuvables : match gardé avec 1 point.
    7. Le script écrit toujours les fichiers attendus par app.py / Unity.

Sorties :
    output/lines_YYYY-MM-DD.txt
    output/audit_YYYY-MM-DD.txt
    output/payload_YYYY-MM-DD.json
    output/payload_latest.json
    output/result_YYYY-MM-DD.json
    output/result_YYYY-MM-DD.txt
    output/result_latest.txt

Usage :
    python fetch_day_lines_v6_11_final_clean.py today --no-send-backend
    python fetch_day_lines_v6_11_final_clean.py tomorrow --no-send-backend
    python fetch_day_lines_v6_11_final_clean.py 2026-05-12 --no-send-backend

Dépendances :
    requests
    beautifulsoup4
    playwright   (fortement recommandé pour Flashscore et pages JS)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

import requests
from bs4 import BeautifulSoup


MODE = "V6_11G_FINAL_CLEAN_B_WINS_AND_ORIENTATION_CONTEXT"
OUT_DIR = Path("output")

ATP_BASE = "https://www.atptour.com"
ATP_CURRENT_URL = "https://www.atptour.com/en/scores/current"
ATP_RANKINGS_LIVE_URL = "https://www.atptour.com/en/rankings/singles/live"
ATP_RANKINGS_URL = "https://www.atptour.com/en/rankings/singles"
TENNIS_TEMPLE_ATP_LIVE_URL = "https://fr.tennistemple.com/classement-atp-live"
TENNIS_TEMPLE_ATP_LIVE_URL_EN = "https://en.tennistemple.com/atp-live-rankings"
LIVE_TENNIS_ATP_LIVE_URL_EN = "https://live-tennis.eu/en/atp-live-ranking"
LIVE_TENNIS_ATP_LIVE_URL_FR = "https://live-tennis.eu/fr/classement-atp-live"
FLASHSCORE_URL_FR = "https://www.flashscore.fr/tennis/"
FLASHSCORE_URL_EN_ATP = "https://www.flashscore.com/tennis/atp-singles/"

REQUEST_TIMEOUT = 35


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class PayloadItem:
    playerA: str
    playerB: str
    surface: str
    playerAPoints: int
    playerBPoints: int
    # Champs historiques / veto pour le joueur A et B.
    # app.py utilise player_a_tournament_wins si le moteur inverse le match.
    player_a_is_qualifier: bool
    player_b_is_qualifier: bool
    player_a_tournament_wins: int
    player_b_tournament_wins: int
    tournament: str
    source: str


def now_paris_date() -> date:
    if ZoneInfo is not None:
        try:
            from datetime import datetime
            return datetime.now(ZoneInfo("Europe/Paris")).date()
        except Exception:
            pass
    return date.today()


def parse_target_day(value: str) -> date:
    raw = normalize_space(str(value or "today")).lower()
    today = now_paris_date()

    if raw in {"today", "aujourd'hui", "aujourdhui"}:
        return today
    if raw in {"tomorrow", "demain"}:
        return today + timedelta(days=1)
    if raw in {"yesterday", "hier"}:
        return today - timedelta(days=1)

    return date.fromisoformat(raw)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def strip_accents_light(text: str) -> str:
    # suffisant pour matching noms ATP/Flashscore
    table = str.maketrans({
        "à": "a", "á": "a", "â": "a", "ä": "a", "ã": "a", "å": "a",
        "ç": "c",
        "è": "e", "é": "e", "ê": "e", "ë": "e",
        "ì": "i", "í": "i", "î": "i", "ï": "i",
        "ñ": "n",
        "ò": "o", "ó": "o", "ô": "o", "ö": "o", "õ": "o",
        "ù": "u", "ú": "u", "û": "u", "ü": "u",
        "ý": "y", "ÿ": "y",
        "À": "A", "Á": "A", "Â": "A", "Ä": "A", "Ã": "A", "Å": "A",
        "Ç": "C",
        "È": "E", "É": "E", "Ê": "E", "Ë": "E",
        "Ì": "I", "Í": "I", "Î": "I", "Ï": "I",
        "Ñ": "N",
        "Ò": "O", "Ó": "O", "Ô": "O", "Ö": "O", "Õ": "O",
        "Ù": "U", "Ú": "U", "Û": "U", "Ü": "U",
        "Ý": "Y",
    })
    return text.translate(table)


def clean_name(name: str) -> str:
    value = normalize_space(name)
    value = re.sub(r"\[[^\]]*\]", " ", value)
    value = re.sub(r"\((Q|WC|LL|SE|PR|Alt|ALT)\)", " ", value, flags=re.I)
    value = re.sub(r"\b(Q|WC|LL|SE|PR|Alt|ALT)\b", " ", value, flags=re.I)
    value = re.sub(r"\bImage:.*$", " ", value, flags=re.I)
    value = re.sub(r"\bPlayer Photo\b", " ", value, flags=re.I)
    value = normalize_space(value)
    return value


def canonical_name(name: str) -> str:
    value = strip_accents_light(clean_name(name)).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return normalize_space(value)


def is_name_like(name: str) -> bool:
    value = clean_name(name)
    if not value or "/" in value:
        return False
    if any(ch.isdigit() for ch in value):
        return False
    parts = value.split()
    if len(parts) < 2 or len(parts) > 6:
        return False
    bad = {"vs", "defeats", "walkover", "retired", "h2h", "image", "photo"}
    return not any(canonical_name(p) in bad for p in parts)


def unordered_pair_key(a: str, b: str) -> Tuple[str, str]:
    return tuple(sorted([canonical_name(a), canonical_name(b)]))  # type: ignore[return-value]


def is_doubles_marker(text: str) -> bool:
    t = normalize_space(text).lower()
    if re.search(r"[a-zà-ÿ]\s*/\s*[a-zà-ÿ]", t, flags=re.I):
        return True
    return bool(re.search(r"\bdoubles?\b|\bdoppel\b|\bdobles\b|\bdouble\b", t, flags=re.I))


def is_wta_marker(text: str) -> bool:
    t = normalize_space(text).lower()
    return bool(re.search(r"\bwta\b|women|women's|femmes|dames", t, flags=re.I))


def surface_from_text(text: str, tournament: str = "") -> str:
    t = normalize_space(f"{text} {tournament}").lower()
    if "clay" in t or "terre" in t:
        return "Clay"
    if "grass" in t or "gazon" in t:
        return "Grass"
    if "hard" in t or "dur" in t:
        return "Hard"

    # Tournois ATP récurrents terre battue.
    if any(x in t for x in [
        "rome", "madrid", "monte", "barcelona", "munich", "geneva",
        "hamburg", "bastad", "gstaad", "kitzbuhel", "estoril", "bucharest",
        "marrakech", "houston", "santiago", "rio", "buenos aires", "cordoba",
        "roland garros", "french open"
    ]):
        return "Clay"

    # Gazon.
    if any(x in t for x in [
        "wimbledon", "halle", "queen", "stuttgart", "s-hertogenbosch",
        "eastbourne", "mallorca", "newport"
    ]):
        return "Grass"

    return "Hard"


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


def fetch_html(session: requests.Session, url: str) -> str:
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text or ""


def player_name_from_href(href: str, fallback: str = "") -> str:
    # ATP slugs fréquents : /en/players/jannik-sinner/s0ag/overview
    m = re.search(r"/players/([^/]+)/", href or "", flags=re.I)
    if m:
        slug = m.group(1).replace("-", " ")
        name = " ".join(x.capitalize() for x in slug.split())
        return clean_name(name)
    return clean_name(fallback)


def title_name(raw: str) -> str:
    words = normalize_space(raw.replace("-", " ")).split()
    return " ".join(w.capitalize() if len(w) > 2 else w.upper() for w in words)


def discover_atp_daily_schedule_urls(session: requests.Session, include_challenger: bool, audit: List[str]) -> List[str]:
    urls: List[str] = []
    start_urls = [ATP_CURRENT_URL]
    if include_challenger:
        start_urls.append("https://www.atptour.com/en/scores/current-challenger")

    patterns = [
        r"https://www\.atptour\.com/en/scores/current(?:-challenger)?/[^\"'\s<>]+/\d+/daily-schedule(?:\?[^\"'\s<>]*)?",
        r"/en/scores/current(?:-challenger)?/[^\"'\s<>]+/\d+/daily-schedule(?:\?[^\"'\s<>]*)?",
        r"https://www\.atptour\.com/en/scores/current(?:-challenger)?/[^\"'\s<>]+/\d+/(?:draws|results|live-scores)(?:\?[^\"'\s<>]*)?",
        r"/en/scores/current(?:-challenger)?/[^\"'\s<>]+/\d+/(?:draws|results|live-scores)(?:\?[^\"'\s<>]*)?",
    ]

    for url in start_urls:
        try:
            html = fetch_html(session, url)
        except Exception as exc:
            audit.append(f"[ATP DISCOVER FAIL] {url} | {type(exc).__name__}: {exc}")
            continue

        for pat in patterns:
            for raw in re.findall(pat, html, flags=re.I):
                full = urljoin(ATP_BASE, raw)
                full = re.sub(r"/(draws|results|live-scores)(\?[^\"'\s<>]*)?$", "/daily-schedule", full, flags=re.I)
                if full not in urls:
                    urls.append(full)

    # Sécurité : si Rome est dans current mais pas découvert à cause du HTML, les liens directs courants marchent.
    # Ces URLs ne créent rien si le tournoi n'est pas actif.
    fallback_candidates = [
        "https://www.atptour.com/en/scores/current/rome/416/daily-schedule",
        "https://www.atptour.com/en/scores/current/geneva/322/daily-schedule",
        "https://www.atptour.com/en/scores/current/hamburg/414/daily-schedule",
        "https://www.atptour.com/en/scores/current/roland-garros/520/daily-schedule",
        "https://www.atptour.com/en/scores/current/stuttgart/321/daily-schedule",
        "https://www.atptour.com/en/scores/current/halle/500/daily-schedule",
        "https://www.atptour.com/en/scores/current/london/311/daily-schedule",
        "https://www.atptour.com/en/scores/current/wimbledon/540/daily-schedule",
    ]

    for u in fallback_candidates:
        if u not in urls:
            urls.append(u)

    audit.append(f"atp_daily_urls_found={len(urls)}")
    for u in urls[:40]:
        audit.append(f"[ATP DAILY URL] {u}")
    return urls


def tournament_from_atp_daily_url(url: str) -> str:
    m = re.search(r"/scores/current(?:-challenger)?/([^/]+)/(\d+)/daily-schedule", url, flags=re.I)
    if m:
        return title_name(m.group(1))
    return "ATP"


def extract_atp_pairs_from_html(html: str, url: str, target_day: date, audit: List[str]) -> List[Dict[str, str]]:
    """
    Parse ATP daily-schedule :
    - priorité aux liens /players/ ;
    - pattern : player A ... Vs/Defeats ... player B ;
    - refuse doubles/WTA/bruit.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Remplace les liens ATP /players/ par marqueurs propres.
    for a in soup.find_all("a", href=True):
        href = a.get("href", "") or ""
        if "/players/" not in href:
            continue
        nm = player_name_from_href(href, a.get_text(" ", strip=True))
        if nm and is_name_like(nm):
            a.clear()
            a.append(f"[[ATP_PLAYER:{nm}]]")

    text = soup.get_text("\n", strip=True)
    lines = [normalize_space(x) for x in text.splitlines() if normalize_space(x)]

    # Tokens visibles.
    tokens: List[Dict[str, str]] = []
    for line in lines:
        low = line.lower()
        if "latest news" in low or "partners" in low or "subscribe" in low:
            break
        if "{{" in line or "}}" in line:
            continue

        pos = 0
        for m in re.finditer(r"\[\[ATP_PLAYER:(.*?)\]\]", line):
            before = normalize_space(line[pos:m.start()])
            if before:
                tokens.append({"type": "TEXT", "text": before})
            nm = clean_name(m.group(1))
            if nm and is_name_like(nm):
                tokens.append({"type": "PLAYER", "name": nm})
            pos = m.end()

        after = normalize_space(line[pos:])
        if after:
            tokens.append({"type": "TEXT", "text": after})

    def token_text(i: int) -> str:
        if i < 0 or i >= len(tokens):
            return ""
        return tokens[i].get("name") or tokens[i].get("text") or ""

    def is_status(text_: str) -> bool:
        t = normalize_space(text_).lower()
        return bool(re.fullmatch(r"(vs|v)", t) or re.search(r"\bvs\b|\bdefeats?\b|\bwalkover\b|\bretired\b|\bw/o\b", t))

    rows: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    tournament = tournament_from_atp_daily_url(url)
    surface = surface_from_text(text, tournament)

    i = 0
    while i < len(tokens):
        if tokens[i].get("type") != "PLAYER":
            i += 1
            continue

        player_a = tokens[i].get("name", "")
        status_idx = -1
        player_b_idx = -1

        # Cherche statut puis joueur B dans une fenêtre courte.
        for j in range(i + 1, min(len(tokens), i + 14)):
            if tokens[j].get("type") == "PLAYER":
                break
            if is_status(tokens[j].get("text", "")):
                status_idx = j
                break

        if status_idx >= 0:
            for k in range(status_idx + 1, min(len(tokens), status_idx + 18)):
                if tokens[k].get("type") == "PLAYER":
                    player_b_idx = k
                    break

        if player_b_idx < 0:
            i += 1
            continue

        player_b = tokens[player_b_idx].get("name", "")
        evidence = normalize_space(" | ".join(token_text(x) for x in range(i, min(len(tokens), player_b_idx + 6))))

        if is_doubles_marker(evidence) or is_wta_marker(evidence):
            i += 1
            continue

        if not is_name_like(player_a) or not is_name_like(player_b):
            i += 1
            continue

        key = unordered_pair_key(player_a, player_b)
        if key not in seen:
            seen.add(key)
            rows.append({
                "playerA": clean_name(player_a),
                "playerB": clean_name(player_b),
                "surface": surface,
                "tournament": tournament,
                "source": "ATP Daily Schedule",
                "sourceUrl": url,
                "evidence": evidence[:320],
            })

        i = player_b_idx + 1

    audit.append(f"[ATP PARSE] {tournament} | rows={len(rows)} | url={url}")
    return rows


def fetch_atp_daily_rows(session: requests.Session, target_day: date, include_challenger: bool, audit: List[str]) -> List[Dict[str, str]]:
    urls = discover_atp_daily_schedule_urls(session, include_challenger, audit)
    all_rows: List[Dict[str, str]] = []

    for url in urls:
        try:
            html = fetch_html(session, url)
            rows = extract_atp_pairs_from_html(html, url, target_day, audit)
            all_rows.extend(rows)
        except Exception as exc:
            audit.append(f"[ATP DAILY FAIL] {url} | {type(exc).__name__}: {exc}")

    # Dédoublonnage global.
    out: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str, str]] = set()
    for r in all_rows:
        a = r.get("playerA", "")
        b = r.get("playerB", "")
        tournament = r.get("tournament", "")
        key_pair = unordered_pair_key(a, b)
        key = (key_pair[0], key_pair[1], canonical_name(tournament))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)

    audit.append(f"atp_daily_rows_total={len(out)}")
    return out


def flash_header_is_atp_singles(header: str) -> bool:
    """
    V6.11C strict.

    On accepte uniquement un vrai header de tournoi ATP simple.
    Exemples acceptés :
      - ATP - SIMPLE: Rome (Italie), terre battue
      - ATP - SINGLES: Rome (Italy), clay

    Refusés :
      - header vide
      - ATP Singles générique
      - Challenger
      - WTA
      - doubles
      - ITF
    """
    h = normalize_space(header).lower()

    if not h:
        return False

    if is_wta_marker(h) or is_doubles_marker(h):
        return False

    if "challenger" in h or "itf" in h or "utr" in h:
        return False

    # Doit être une vraie compétition avec deux-points.
    if ":" not in h:
        return False

    # Doit commencer par ATP et indiquer simple/singles.
    left = h.split(":", 1)[0]
    right = h.split(":", 1)[1].strip()

    if "atp" not in left:
        return False

    if not ("simple" in left or "singles" in left):
        return False

    # Il faut un vrai nom de tournoi après les deux-points.
    if len(right) < 3:
        return False

    return True


def build_alias_map(points_map: Dict[str, int]) -> Dict[str, str]:
    """
    Construit les alias Flashscore depuis points_map.

    points_map doit maintenant contenir les noms en ordre normal :
      jannik sinner
      daniil medvedev

    Alias générés :
      sinner j. -> Jannik Sinner
      sinner j  -> Jannik Sinner
      j. sinner -> Jannik Sinner
    """
    aliases: Dict[str, str] = {}

    for key in points_map.keys():
        parts = key.split()
        if len(parts) < 2:
            continue

        first = parts[0]
        last = parts[-1]
        surname = " ".join(parts[1:])
        initial = first[:1]
        full_title = " ".join(p.capitalize() for p in parts)

        keys = {
            key,
            f"{surname} {initial}",
            f"{surname} {initial}.",
            f"{last} {initial}",
            f"{last} {initial}.",
            f"{initial} {surname}",
            f"{initial}. {surname}",
            f"{initial} {last}",
            f"{initial}. {last}",
        }

        for a in keys:
            aliases[canonical_name(a)] = full_title
            aliases[a] = full_title

    return aliases


def resolve_flash_name(raw: str, aliases: Dict[str, str]) -> str:
    name = clean_name(raw)
    key = canonical_name(name)

    if key in aliases:
        return aliases[key]
    if name in aliases:
        return aliases[name]

    # Flashscore fréquent : "Sinner J."
    m = re.match(r"^(.+?)\s+([A-Za-zÀ-ÿ])\.?$", name)
    if m:
        surname = canonical_name(m.group(1))
        initial = canonical_name(m.group(2))[:1]
        for k in (f"{surname} {initial}", f"{surname} {initial}."):
            if k in aliases:
                return aliases[k]
            ck = canonical_name(k)
            if ck in aliases:
                return aliases[ck]

    # Autre forme : "J. Sinner"
    m = re.match(r"^([A-Za-zÀ-ÿ])\.?\s+(.+)$", name)
    if m:
        initial = canonical_name(m.group(1))[:1]
        surname = canonical_name(m.group(2))
        for k in (f"{initial} {surname}", f"{initial}. {surname}"):
            if k in aliases:
                return aliases[k]
            ck = canonical_name(k)
            if ck in aliases:
                return aliases[ck]

    return name


def fetch_flashscore_rows(target_day: date, points_map: Dict[str, int], audit: List[str]) -> List[Dict[str, str]]:
    """
    Fallback final quand ATP est bloqué 403.

    V6.11C :
    - utilise seulement la page principale Flashscore tennis ;
    - extrait le vrai header de compétition autour de chaque match ;
    - garde uniquement les headers ATP - SIMPLE / ATP - SINGLES avec vrai tournoi ;
    - refuse header vide, ATP Singles générique, WTA, doubles, ITF, Challenger.
    """
    rows: List[Dict[str, str]] = []
    aliases = build_alias_map(points_map)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        audit.append(f"flashscore_status=playwright_missing | {type(exc).__name__}: {exc}")
        return rows

    js = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  function getHeaderForMatch(match) {
    // 1) remonter aux conteneurs parents et chercher le header précédent dans le même parent
    let node = match;
    for (let depth = 0; depth < 6 && node; depth++, node = node.parentElement) {
      let p = node.previousElementSibling;
      while (p) {
        const txt = clean(p.innerText || p.textContent || '');
        const cls = p.className ? String(p.className) : '';
        if (cls.includes('event__header') || txt.includes('ATP -') || txt.includes('WTA -')) {
          return txt;
        }
        p = p.previousElementSibling;
      }
    }

    // 2) fallback : parcours global avant le match
    const all = Array.from(document.querySelectorAll('.event__header, [class*="event__header"], .event__match, [id^="g_2_"], [id^="g_1_"]'));
    let lastHeader = '';
    for (const el of all) {
      if (el === match) return lastHeader;
      const cls = el.className ? String(el.className) : '';
      const txt = clean(el.innerText || el.textContent || '');
      if (cls.includes('event__header') || txt.includes('ATP -') || txt.includes('WTA -')) {
        lastHeader = txt;
      }
    }
    return '';
  }

  const out = [];
  const matches = Array.from(document.querySelectorAll('.event__match, [id^="g_2_"], [id^="g_1_"]'));

  for (const node of matches) {
    const cls = node.className ? String(node.className) : '';
    const id = node.id || '';
    const isMatch = cls.includes('event__match') || id.startsWith('g_2_') || id.startsWith('g_1_');
    if (!isMatch) continue;

    const homeEl = node.querySelector('[class*="event__participant--home"]');
    const awayEl = node.querySelector('[class*="event__participant--away"]');
    const home = clean(homeEl ? homeEl.textContent : '');
    const away = clean(awayEl ? awayEl.textContent : '');
    const raw = clean(node.innerText || node.textContent || '');
    const competition = getHeaderForMatch(node);

    if (home && away) {
      out.push({competition, playerA: home, playerB: away, raw});
    }
  }

  return out;
}
"""

    raw_rows: List[Dict[str, str]] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1365, "height": 2600},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
            )
            page = ctx.new_page()
            page.goto(FLASHSCORE_URL_FR, wait_until="domcontentloaded", timeout=50000)

            for label in ["J'accepte", "Tout refuser", "Accepter", "OK", "I accept"]:
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=1500)
                    break
                except Exception:
                    pass

            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            today = now_paris_date()
            delta = (target_day - today).days

            if delta == 1:
                for label in ["Jour suivant", "Demain", "Next day", "Tomorrow"]:
                    try:
                        page.get_by_title(label, exact=False).first.click(timeout=1500)
                        page.wait_for_timeout(1800)
                        audit.append(f"flashscore_day_click={label}")
                        break
                    except Exception:
                        pass
            elif delta == -1:
                for label in ["Jour précédent", "Hier", "Previous day", "Yesterday"]:
                    try:
                        page.get_by_title(label, exact=False).first.click(timeout=1500)
                        page.wait_for_timeout(1800)
                        audit.append(f"flashscore_day_click={label}")
                        break
                    except Exception:
                        pass
            elif delta != 0:
                audit.append(f"flashscore_day_delta_not_supported={delta}")

            # Scroll contrôlé pour charger les sections du jour.
            for _ in range(8):
                try:
                    page.mouse.wheel(0, 1600)
                    page.wait_for_timeout(450)
                except Exception:
                    pass

            raw_rows = page.evaluate(js)
            browser.close()

    except Exception as exc:
        audit.append(f"flashscore_status=failed | {type(exc).__name__}: {exc}")
        return rows

    seen: Set[Tuple[str, str, str]] = set()
    rejected = 0
    rejected_samples: List[str] = []

    for item in raw_rows or []:
        comp = normalize_space(str(item.get("competition", "")))
        raw_a = normalize_space(str(item.get("playerA", "")))
        raw_b = normalize_space(str(item.get("playerB", "")))
        raw = normalize_space(str(item.get("raw", "")))

        joined = normalize_space(f"{comp} {raw_a} {raw_b} {raw}")

        if not flash_header_is_atp_singles(comp):
            rejected += 1
            if len(rejected_samples) < 20:
                rejected_samples.append(f"header={comp!r} | {raw_a} vs {raw_b}")
            continue

        if is_wta_marker(joined) or is_doubles_marker(joined):
            rejected += 1
            if len(rejected_samples) < 20:
                rejected_samples.append(f"wta/double | header={comp!r} | {raw_a} vs {raw_b}")
            continue

        player_a = resolve_flash_name(raw_a, aliases)
        player_b = resolve_flash_name(raw_b, aliases)

        if not is_name_like(player_a) or not is_name_like(player_b):
            rejected += 1
            if len(rejected_samples) < 20:
                rejected_samples.append(f"bad_name | header={comp!r} | {raw_a} vs {raw_b}")
            continue

        pair = unordered_pair_key(player_a, player_b)
        key = (pair[0], pair[1], canonical_name(comp))
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "playerA": player_a,
            "playerB": player_b,
            "surface": surface_from_text(comp, comp),
            "tournament": comp,
            "source": "Flashscore Main ATP Singles Strict Header",
            "sourceUrl": FLASHSCORE_URL_FR,
            "evidence": normalize_space(f"{comp} | {raw}")[:320],
        })

    audit.append("flashscore_status=ok")
    audit.append(f"flashscore_scrape_url={FLASHSCORE_URL_FR}")
    audit.append(f"flashscore_raw_rows={len(raw_rows or [])}")
    audit.append(f"flashscore_atp_singles_rows={len(rows)}")
    audit.append(f"flashscore_rejected_rows={rejected}")
    audit.append("flashscore_filter=strict_real_atp_tournament_header_required")

    for s in rejected_samples:
        audit.append(f"[FLASH REJECT SAMPLE] {s}")

    for r in rows[:80]:
        audit.append(f"[FLASH ATP KEEP] {r['playerA']} vs {r['playerB']} | {r['tournament']}")

    return rows


def _parse_int_token(raw: str) -> int:
    try:
        return int((raw or "").replace(",", "").replace(" ", "").replace(".", ""))
    except Exception:
        return 0


def _line_numbers(line: str) -> List[int]:
    out: List[int] = []
    for raw_num in re.findall(r"\b\d{1,3}(?:[ ,.]\d{3})+\b|\b\d{1,5}\b", line or ""):
        n = _parse_int_token(raw_num)
        if 1 <= n <= 20000:
            out.append(n)
    return out


def _looks_like_country_code(line: str) -> bool:
    s = normalize_space(line).upper()
    return bool(re.fullmatch(r"[A-Z]{3}", s))


def _looks_like_ranking_status_line(line: str) -> bool:
    """
    Refuse les lignes de statut tournoi/ranking qui peuvent ressembler à du texte de nom.
    Exemple : "Défaite Rome QF", "Rome QF", "Rome DF".
    """
    ck = canonical_name(line)
    if not ck:
        return True

    status_words = {
        "defaite", "loss", "lost", "win", "wins", "won",
        "rome", "geneva", "hamburg", "roland", "garros", "stuttgart",
        "halle", "london", "wimbledon", "qf", "sf", "df", "final",
        "finale", "semifinal", "quarterfinal", "round", "r16", "r32", "r64",
        "bye", "live", "official", "ranking", "race", "draw", "draws",
    }

    parts = set(ck.split())
    if parts & status_words:
        return True

    if re.search(r"\b(?:qf|sf|df|r16|r32|r64)\b", ck):
        return True

    return False


def _strict_total_points_after_name(lines: List[str], start_index: int, window: int = 14) -> int:
    """
    Cherche le VRAI total de points après un nom de joueur.

    Correction V6.11E :
    - ne prend plus le plus grand nombre au hasard ;
    - ignore âge/rang/petites variations ;
    - prend le premier nombre >= 300 après le nom, car les lignes comme 150/170/188
      sont souvent des variations, classements ou points gagnés, pas le total ATP.
    """
    small_candidates: List[int] = []

    for j in range(start_index + 1, min(len(lines), start_index + window)):
        line = normalize_space(lines[j])
        if not line:
            continue

        # Nouveau nom rencontré => stop.
        if "," in line and re.search(r"[A-Za-zÀ-ÿ]", line):
            break

        # Si une ligne ressemble à un autre nom complet, stop aussi.
        if j > start_index + 1 and is_name_like(line) and not any(ch.isdigit() for ch in line):
            break

        nums = _line_numbers(line)
        if not nums:
            continue

        for n in nums:
            # Age/rang/variations courantes : on les ignore comme total.
            if n < 300:
                small_candidates.append(n)
                continue

            # Premier gros nombre rencontré = total ATP le plus fiable.
            return n

    # Fallback uniquement si aucun total crédible n'a été trouvé.
    # On garde le plus grand petit nombre pour ne pas mettre 0, mais l'audit le signalera.
    return max(small_candidates) if small_candidates else 0


def parse_live_tennis_points(text: str, audit: List[str]) -> Dict[str, int]:
    """
    Parse live-tennis.eu ATP live ranking.

    Format généralement lisible :
      Luciano Darderi
      24
      ITA
      2060
      +3
      +170
      Rome QF

    Règle stricte :
      nom -> ignore âge/pays/rang/variations -> premier nombre >= 300 = points ATP.
    """
    points: Dict[str, int] = {}
    lines = [normalize_space(x) for x in (text or "").splitlines() if normalize_space(x)]

    banned_words = {
        "live", "ranking", "rankings", "official", "race", "schedule", "scores",
        "player", "age", "ctry", "pts", "points", "next", "week", "tournament",
        "draws", "menu", "search", "privacy", "cookies",
    }

    for i, line in enumerate(lines):
        ck_line = canonical_name(line)

        if not is_name_like(line):
            continue

        if any(w in ck_line.split() for w in banned_words):
            continue

        # Live-tennis noms simples : "Karen Khachanov", pas "Khachanov, Karen".
        if "," in line:
            continue

        # Évite les lignes pays / statut tournoi.
        if _looks_like_country_code(line):
            continue

        if _looks_like_ranking_status_line(line):
            continue

        total = _strict_total_points_after_name(lines, i, window=14)
        if total >= 300:
            points[canonical_name(line)] = int(total)

    audit.append(f"live_tennis_points_parse_count={len(points)}")
    return points


def parse_points_from_text(text: str, audit: List[str]) -> Dict[str, int]:
    """
    Parse générique pour ATP officiel / autres pages.
    V6.11E : premier total crédible >= 300 après le nom, pas max aléatoire.
    """
    points: Dict[str, int] = {}
    lines = [normalize_space(x) for x in text.splitlines() if normalize_space(x)]
    banned = {
        "rank", "ranking", "rankings", "player", "age", "points", "tournament",
        "move", "official", "singles", "partners", "subscribe", "privacy",
    }

    for i, line in enumerate(lines):
        if not is_name_like(line):
            continue

        ck = canonical_name(line)
        if any(x in ck.split() for x in banned):
            continue

        total = _strict_total_points_after_name(lines, i, window=12)
        if total >= 300:
            points[ck] = int(total)

    audit.append(f"points_parse_text_count={len(points)}")
    return points


def parse_tennis_temple_points(text: str, audit: List[str]) -> Dict[str, int]:
    """
    Parse TennisTemple live ATP ranking.

    Format :
      Sinner, Jannik
      24
      13900 -450

    Correction V6.11E :
      on ne prend plus le plus grand nombre proche du nom.
      On prend le premier total crédible >= 300 après le nom.
      Ça évite les faux points comme 150, 170, 188.
    """
    points: Dict[str, int] = {}
    lines = [normalize_space(x) for x in (text or "").splitlines() if normalize_space(x)]

    name_re = re.compile(r"^([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’ .-]{1,50}),\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’ .-]{1,50})$")

    for i, line in enumerate(lines):
        m = name_re.match(line)
        if not m:
            continue

        last = clean_name(m.group(1).replace("’", "'"))
        first = clean_name(m.group(2).replace("’", "'"))

        if not first or not last:
            continue

        full = clean_name(f"{first} {last}")

        if not is_name_like(full):
            continue

        total = _strict_total_points_after_name(lines, i, window=12)

        if total >= 300:
            points[canonical_name(full)] = int(total)

    audit.append(f"tennistemple_points_parse_count={len(points)}")
    return points


def fetch_points_map(session: requests.Session, audit: List[str]) -> Dict[str, int]:
    """
    Sources points ATP.

    V6.11E :
    priorité aux sources live qui donnent un total directement exploitable.
    Le parser refuse les petites valeurs 150/170/188 comme total ATP.
    """
    # 1) live-tennis.eu : source live lisible quand accessible.
    for url in [LIVE_TENNIS_ATP_LIVE_URL_EN, LIVE_TENNIS_ATP_LIVE_URL_FR]:
        try:
            html = fetch_html(session, url)
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            body_text = soup.get_text("\n", strip=True)
            points = parse_live_tennis_points(body_text, audit)
            if len(points) >= 50:
                audit.append(f"points_source={url}")
                audit.append("points_policy=strict_first_total_ge_300")
                return points
            audit.append(f"points_source_too_small={url} | count={len(points)}")
        except Exception as exc:
            audit.append(f"points_live_tennis_failed={url} | {type(exc).__name__}: {exc}")

    # 2) TennisTemple live.
    for url in [TENNIS_TEMPLE_ATP_LIVE_URL_EN, TENNIS_TEMPLE_ATP_LIVE_URL]:
        try:
            html = fetch_html(session, url)
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            body_text = soup.get_text("\n", strip=True)
            points = parse_tennis_temple_points(body_text, audit)
            if len(points) >= 50:
                audit.append(f"points_source={url}")
                audit.append("points_policy=strict_first_total_ge_300")
                return points
            audit.append(f"points_source_too_small={url} | count={len(points)}")
        except Exception as exc:
            audit.append(f"points_tennistemple_failed={url} | {type(exc).__name__}: {exc}")

    # 3) ATP officiel en dernier, souvent bloqué 403 sur Railway.
    for url in [ATP_RANKINGS_LIVE_URL, ATP_RANKINGS_URL]:
        try:
            html = fetch_html(session, url)
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            points = parse_points_from_text(soup.get_text("\n", strip=True), audit)
            if len(points) >= 50:
                audit.append(f"points_source={url}")
                audit.append("points_policy=strict_first_total_ge_300")
                return points
            audit.append(f"points_source_too_small={url} | count={len(points)}")
        except Exception as exc:
            audit.append(f"points_fetch_failed={url} | {type(exc).__name__}: {exc}")

    audit.append("points_source=EMPTY")
    audit.append("points_policy=strict_first_total_ge_300")
    return {}


def points_for(name: str, points_map: Dict[str, int]) -> int:
    key = canonical_name(name)
    if key in points_map and points_map[key] > 0:
        return int(points_map[key])

    tokens = key.split()
    if len(tokens) >= 2:
        rev = " ".join(reversed(tokens))
        if rev in points_map and points_map[rev] > 0:
            return int(points_map[rev])

        # unique all-token match
        wanted = set(tokens)
        candidates = [k for k in points_map if wanted.issubset(set(k.split()))]
        if len(candidates) == 1:
            return int(points_map[candidates[0]])

        # surname + initial
        first, last = tokens[0], tokens[-1]
        initial = first[:1]
        cand = []
        for k in points_map:
            parts = k.split()
            if len(parts) >= 2 and parts[-1] == last and parts[0].startswith(initial):
                cand.append(k)
            elif len(parts) >= 2 and parts[0] == last and parts[1].startswith(initial):
                cand.append(k)
        if len(set(cand)) == 1:
            return int(points_map[cand[0]])

    return 1


def tournament_key_from_text(txt: str) -> str:
    """
    Clef courte et stable du tournoi.
    Exemples :
      "Rome (Italie), terre battue ATP - SINGLES: Tableau" -> "rome"
      "ATP - SIMPLE: Rome (Italie), terre battue" -> "rome"
    """
    s = normalize_space(txt)
    if not s:
        return ""

    s_low = strip_accents_light(s).lower()

    # Format "ATP - SIMPLE: Rome ..."
    if ":" in s_low and ("atp" in s_low.split(":", 1)[0]):
        right = s_low.split(":", 1)[1].strip()
        right = re.sub(r"\b(tableau|draw|qualification|qualifications)\b", " ", right)
        right = normalize_space(right)
        if right:
            s_low = right

    # Format Flashscore FR : "Rome (Italie), terre battue ATP - SINGLES: Tableau"
    s_low = re.sub(r"\batp\s*-\s*(simple|simples|singles|doubles)\b.*$", " ", s_low)
    s_low = re.sub(r"\bwta\s*-\s*(simple|simples|singles|doubles)\b.*$", " ", s_low)
    s_low = re.sub(r"\b(challenger|itf)\b.*$", " ", s_low)
    s_low = re.sub(r"\([^)]*\)", " ", s_low)
    s_low = s_low.split(",", 1)[0]
    s_low = normalize_space(s_low)

    # Garde seulement les mots utiles du nom du tournoi.
    parts = [p for p in s_low.split() if len(p) >= 2]
    return canonical_name(" ".join(parts[:3]))


def fetch_flashscore_prior_wins(
    target_day: date,
    rows: List[Dict[str, str]],
    points_map: Dict[str, int],
    audit: List[str],
    days_back: int = 10,
) -> Dict[Tuple[str, str], int]:
    """
    Compte les victoires déjà obtenues dans le tournoi avant le jour demandé.

    Important :
    - ne change pas la liste des matchs ;
    - ne change pas les points ATP ;
    - ne touche pas aux cotes ;
    - sert seulement à remplir player_b_tournament_wins.

    Méthode :
    - ouvre Flashscore tennis ;
    - se place sur le jour demandé ;
    - remonte les jours précédents ;
    - garde seulement les matchs terminés avec vrai header ATP SIMPLE/SINGLES ;
    - compte les gagnants par tournoi.
    """
    out: Dict[Tuple[str, str], int] = {}

    tournament_keys_needed = {
        tournament_key_from_text(r.get("tournament", ""))
        for r in rows
        if tournament_key_from_text(r.get("tournament", ""))
    }

    if not tournament_keys_needed:
        audit.append("prior_wins_status=skipped_no_tournament_key")
        return out

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        audit.append(f"prior_wins_status=playwright_missing | {type(exc).__name__}: {exc}")
        return out

    aliases = build_alias_map(points_map)

    js = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  function getHeaderForMatch(match) {
    let node = match;
    for (let depth = 0; depth < 6 && node; depth++, node = node.parentElement) {
      let p = node.previousElementSibling;
      while (p) {
        const txt = clean(p.innerText || p.textContent || '');
        const cls = p.className ? String(p.className) : '';
        if (cls.includes('event__header') || txt.includes('ATP -') || txt.includes('WTA -')) return txt;
        p = p.previousElementSibling;
      }
    }

    const all = Array.from(document.querySelectorAll('.event__header, [class*="event__header"], .event__match, [id^="g_2_"], [id^="g_1_"]'));
    let lastHeader = '';
    for (const el of all) {
      if (el === match) return lastHeader;
      const cls = el.className ? String(el.className) : '';
      const txt = clean(el.innerText || el.textContent || '');
      if (cls.includes('event__header') || txt.includes('ATP -') || txt.includes('WTA -')) lastHeader = txt;
    }
    return '';
  }

  const out = [];
  const matches = Array.from(document.querySelectorAll('.event__match, [id^="g_2_"], [id^="g_1_"]'));

  for (const node of matches) {
    const cls = node.className ? String(node.className) : '';
    const id = node.id || '';
    const isMatch = cls.includes('event__match') || id.startsWith('g_2_') || id.startsWith('g_1_');
    if (!isMatch) continue;

    const homeEl = node.querySelector('[class*="event__participant--home"]');
    const awayEl = node.querySelector('[class*="event__participant--away"]');
    const home = clean(homeEl ? homeEl.textContent : '');
    const away = clean(awayEl ? awayEl.textContent : '');
    if (!home || !away) continue;

    const homeCls = homeEl && homeEl.className ? String(homeEl.className) : '';
    const awayCls = awayEl && awayEl.className ? String(awayEl.className) : '';

    const homeWinner = homeCls.includes('winner');
    const awayWinner = awayCls.includes('winner');

    // Match terminé : Flashscore marque le gagnant avec participant--winner.
    if (!homeWinner && !awayWinner) continue;

    const competition = getHeaderForMatch(node);
    const raw = clean(node.innerText || node.textContent || '');
    const winner = homeWinner ? home : away;

    out.push({competition, playerA: home, playerB: away, winner, raw});
  }

  return out;
}
"""

    raw_total = 0
    kept_total = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1365, "height": 2600},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
            )
            page = ctx.new_page()
            page.goto(FLASHSCORE_URL_FR, wait_until="domcontentloaded", timeout=50000)

            for label in ["J'accepte", "Tout refuser", "Accepter", "OK", "I accept"]:
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=1500)
                    break
                except Exception:
                    pass

            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            today = now_paris_date()
            delta = (target_day - today).days

            # Se placer sur target_day si today/tomorrow/past proche.
            if delta > 0:
                for _ in range(min(delta, 7)):
                    clicked = False
                    for label in ["Jour suivant", "Demain", "Next day", "Tomorrow"]:
                        try:
                            page.get_by_title(label, exact=False).first.click(timeout=1500)
                            page.wait_for_timeout(900)
                            clicked = True
                            break
                        except Exception:
                            pass
                    if not clicked:
                        break
            elif delta < 0:
                for _ in range(min(abs(delta), 14)):
                    clicked = False
                    for label in ["Jour précédent", "Hier", "Previous day", "Yesterday"]:
                        try:
                            page.get_by_title(label, exact=False).first.click(timeout=1500)
                            page.wait_for_timeout(900)
                            clicked = True
                            break
                        except Exception:
                            pass
                    if not clicked:
                        break

            def click_previous_day() -> bool:
                """
                Flashscore change souvent l'attribut title.
                V6.11G utilise plusieurs sélecteurs CSS + JS au lieu de dépendre
                uniquement de get_by_title().
                """
                selectors = [
                    ".calendar__navigation--yesterday",
                    "button.calendar__navigation--yesterday",
                    "[class*='calendar__navigation--yesterday']",
                    "[class*='calendar'][class*='yesterday']",
                    "[title*='Jour précédent']",
                    "[aria-label*='Jour précédent']",
                    "[title*='Yesterday']",
                    "[aria-label*='Yesterday']",
                    "[title*='Previous']",
                    "[aria-label*='Previous']",
                    "[data-testid*='calendar'][data-testid*='previous']",
                    "[data-testid*='previous']",
                ]

                for sel in selectors:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            loc.click(timeout=1500)
                            page.wait_for_timeout(1500)
                            return True
                    except Exception:
                        pass

                # Fallback JS : cherche un élément cliquable dont class/title/aria évoque hier/précédent.
                try:
                    ok = page.evaluate("""
() => {
  const needles = ['yesterday', 'previous', 'prev', 'precedent', 'précédent', 'hier'];
  const els = Array.from(document.querySelectorAll('button, a, div, span'));
  for (const el of els) {
    const blob = [
      el.className ? String(el.className) : '',
      el.getAttribute('title') || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('data-testid') || '',
      el.textContent || ''
    ].join(' ').toLowerCase();

    if (needles.some(n => blob.includes(n))) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        el.click();
        return true;
      }
    }
  }
  return false;
}
""")
                    if ok:
                        page.wait_for_timeout(1500)
                        return True
                except Exception:
                    pass

                # Dernier fallback : touche flèche gauche, parfois acceptée par le calendrier.
                try:
                    page.keyboard.press("ArrowLeft")
                    page.wait_for_timeout(1500)
                    return True
                except Exception:
                    return False

            # Maintenant on remonte les jours AVANT target_day.
            for day_offset in range(1, days_back + 1):
                clicked_prev = click_previous_day()

                if not clicked_prev:
                    audit.append(f"prior_wins_prev_click_failed_at_offset={day_offset}")
                    break

                for _ in range(7):
                    try:
                        page.mouse.wheel(0, 1600)
                        page.wait_for_timeout(350)
                    except Exception:
                        pass

                raw_rows = page.evaluate(js) or []
                raw_total += len(raw_rows)

                for item in raw_rows:
                    comp = normalize_space(str(item.get("competition", "")))
                    raw_a = normalize_space(str(item.get("playerA", "")))
                    raw_b = normalize_space(str(item.get("playerB", "")))
                    winner_raw = normalize_space(str(item.get("winner", "")))
                    raw = normalize_space(str(item.get("raw", "")))

                    joined = normalize_space(f"{comp} {raw_a} {raw_b} {winner_raw} {raw}")

                    if not flash_header_is_atp_singles(comp):
                        continue
                    if is_wta_marker(joined) or is_doubles_marker(joined):
                        continue

                    tkey = tournament_key_from_text(comp)
                    if tkey not in tournament_keys_needed:
                        continue

                    winner = resolve_flash_name(winner_raw, aliases)
                    if not is_name_like(winner):
                        continue

                    out[(tkey, canonical_name(winner))] = out.get((tkey, canonical_name(winner)), 0) + 1
                    kept_total += 1

            browser.close()

    except Exception as exc:
        audit.append(f"prior_wins_status=failed | {type(exc).__name__}: {exc}")
        return out

    audit.append("prior_wins_status=ok")
    audit.append(f"prior_wins_days_back={days_back}")
    audit.append(f"prior_wins_raw_completed_rows={raw_total}")
    audit.append(f"prior_wins_kept_completed_atp_rows={kept_total}")

    for (tkey, player), wins in sorted(out.items())[:80]:
        audit.append(f"[PRIOR WIN] tournament={tkey} player={player} wins={wins}")

    return out



def build_payload(rows: List[Dict[str, str]], points_map: Dict[str, int], audit: List[str], prior_wins: Optional[Dict[Tuple[str, str], int]] = None) -> List[PayloadItem]:
    payload: List[PayloadItem] = []
    seen: Set[Tuple[str, str, str]] = set()
    missing = 0
    prior_wins = prior_wins or {}

    for row in rows:
        a = clean_name(row.get("playerA", ""))
        b = clean_name(row.get("playerB", ""))
        if not is_name_like(a) or not is_name_like(b):
            continue
        if is_wta_marker(f"{a} {b} {row.get('evidence', '')}") or is_doubles_marker(f"{a} {b} {row.get('evidence', '')}"):
            continue

        tournament = row.get("tournament", "ATP") or "ATP"
        key_pair = unordered_pair_key(a, b)
        key = (key_pair[0], key_pair[1], canonical_name(tournament))
        if key in seen:
            continue
        seen.add(key)

        pa_raw = points_map.get(canonical_name(a), 0)
        pb_raw = points_map.get(canonical_name(b), 0)
        pa = points_for(a, points_map)
        pb = points_for(b, points_map)
        if pa_raw <= 0 or pb_raw <= 0:
            missing += 1
            audit.append(f"[POINTS FALLBACK 1 POSSIBLE] {a}={pa} {b}={pb}")

        evidence = row.get("evidence", "")
        bq = bool(re.search(rf"\(Q\).{{0,80}}{re.escape(b)}|{re.escape(b)}.{{0,80}}\(Q\)", evidence, flags=re.I))

        aq = bool(re.search(rf"\(Q\).{{0,80}}{re.escape(a)}|{re.escape(a)}.{{0,80}}\(Q\)", evidence, flags=re.I))

        tkey = tournament_key_from_text(tournament)
        a_wins = int(prior_wins.get((tkey, canonical_name(a)), 0))
        b_wins = int(prior_wins.get((tkey, canonical_name(b)), 0))
        audit.append(f"[A CONTEXT] {a} | tournament={tkey} | prior_wins={a_wins} | qualifier={aq}")
        audit.append(f"[B CONTEXT] {b} | tournament={tkey} | prior_wins={b_wins} | qualifier={bq}")

        payload.append(PayloadItem(
            playerA=a,
            playerB=b,
            surface=row.get("surface") or surface_from_text(row.get("evidence", ""), tournament),
            playerAPoints=int(pa),
            playerBPoints=int(pb),
            player_a_is_qualifier=aq,
            player_b_is_qualifier=bq,
            player_a_tournament_wins=a_wins,
            player_b_tournament_wins=b_wins,
            tournament=tournament,
            source=row.get("source", "ATP Singles Daily"),
        ))

    audit.append(f"payload_count={len(payload)}")
    audit.append(f"points_missing_or_alias_rows={missing}")
    return payload


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_day", help="today | tomorrow | YYYY-MM-DD")
    parser.add_argument("--include-challenger", action="store_true")
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument("--unsafe-assume-no-veto", action="store_true")
    parser.add_argument("--minimal-context-only", action="store_true")
    parser.add_argument("--no-send-backend", action="store_true")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    target_day = parse_target_day(args.target_day)
    session = build_session()
    audit: List[str] = [
        f"mode={MODE}",
        f"target_day={target_day.isoformat()}",
        f"target_label={args.target_day}",
        "source_policy=ATP_DAILY_THEN_FLASHSCORE_ATP_SINGLES",
        "wta_policy=reject",
        "doubles_policy=reject",
        "missing_points_policy=keep_match_replace_with_1",
    ]

    points_map = fetch_points_map(session, audit)
    audit.append(f"points_map_size={len(points_map)}")

    rows = fetch_atp_daily_rows(session, target_day, args.include_challenger, audit)
    source_final = "ATP Daily Schedule"

    if not rows:
        audit.append("atp_daily_returned_zero=true")
        rows = fetch_flashscore_rows(target_day, points_map, audit)
        source_final = "Flashscore ATP Singles Fallback" if rows else "EMPTY"
    else:
        audit.append("atp_daily_returned_zero=false")

    audit.append(f"source_final={source_final}")
    audit.append(f"source_final_rows={len(rows)}")

    prior_wins = fetch_flashscore_prior_wins(target_day, rows, points_map, audit) if rows else {}
    payload_items = build_payload(rows, points_map, audit, prior_wins=prior_wins)

    stamp = target_day.isoformat()
    lines_path = OUT_DIR / f"lines_{stamp}.txt"
    audit_path = OUT_DIR / f"audit_{stamp}.txt"
    payload_path = OUT_DIR / f"payload_{stamp}.json"
    result_json_path = OUT_DIR / f"result_{stamp}.json"
    result_txt_path = OUT_DIR / f"result_{stamp}.txt"

    lines = [
        f"{x.playerA};{x.playerB};{x.surface};{x.playerAPoints};{x.playerBPoints};{str(x.player_b_is_qualifier).lower()};{x.player_b_tournament_wins}"
        for x in payload_items
    ]

    payload_serialized = [asdict(x) for x in payload_items]

    write_text(lines_path, "\n".join(lines))
    write_text(audit_path, "\n".join(audit))
    write_json(payload_path, payload_serialized)
    write_json(OUT_DIR / "payload_latest.json", payload_serialized)
    write_text(OUT_DIR / "unity_input.txt", "\n".join(lines))
    write_text(OUT_DIR / "lines_latest.txt", "\n".join(lines))
    write_text(OUT_DIR / "audit_latest.txt", "\n".join(audit))

    result = {
        "status": "payload_only" if payload_items else "empty_payload",
        "mode": MODE,
        "targetDay": target_day.isoformat(),
        "sourceFinal": source_final,
        "payloadCount": len(payload_items),
        "payloadPath": str(payload_path),
        "message": "PAYLOAD_ONLY - app.py calculera le moteur directement." if payload_items else "Aucun match ATP simple trouvé.",
    }
    write_json(result_json_path, result)
    write_text(result_txt_path, result["message"])
    write_text(OUT_DIR / "result_latest.txt", result["message"])

    print(f"UNITY_INPUT : {OUT_DIR / 'unity_input.txt'}")
    print(f"LINES       : {lines_path}")
    print(f"AUDIT       : {audit_path}")
    print(f"PAYLOAD     : {payload_path}")
    print(f"RESULT_JSON : {result_json_path}")
    print(f"RESULT_TXT  : {result_txt_path}")
    print(f"LINES_LATEST: {OUT_DIR / 'lines_latest.txt'}")
    print(f"AUDIT_LATEST: {OUT_DIR / 'audit_latest.txt'}")
    print(f"RESULT_LATEST: {OUT_DIR / 'result_latest.txt'}")
    print(f"COUNT       : {len(lines)}")
    print(result["message"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
