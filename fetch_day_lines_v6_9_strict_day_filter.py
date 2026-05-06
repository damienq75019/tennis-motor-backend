#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - Fetch daily lines V6.11 ATP DAILY SCHEDULE SCROLL FIX

But :
- garder le moteur / veto existant de la V6.7 ;
- empêcher today et tomorrow de reprendre la même page ATP globale ;
- utiliser les paires d'article uniquement si elles sont dans une section de date
  correspondant réellement à target_day ;
- si aucune section datée fiable n'est trouvée, ne pas inventer de matchs.

Correction importante :
- suppression du fallback dangereux qui faisait des paires avec des noms consécutifs.
- le script accepte seulement les vrais matchs détectés avec "vs" / "v.".
- si les noms sont en lignes séparées autour de "Vs", le script reconstruit la paire proprement.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup


BASE_MODULE_CANDIDATES = [
    "fetch_day_lines_v6_7_results_context_fixed_safe_clamped",
    "fetch_day_lines_v6_6_results_context_fixed_safe",
    "fetch_day_lines_v6_5_results_context_safe",
]

MODE = "V6_11_ATP_DAILY_SCHEDULE_SCROLL_FIX"
PAYLOAD_LATEST_PATH = Path("output") / "payload_latest.json"


def load_base_module():
    last_error: Optional[BaseException] = None
    for name in BASE_MODULE_CANDIDATES:
        try:
            return importlib.import_module(name)
        except BaseException as exc:
            last_error = exc
    raise RuntimeError(
        "Impossible d'importer le script de base. "
        "Vérifie que fetch_day_lines_v6_7_results_context_fixed_safe_clamped.py "
        "est bien dans le même dossier."
    ) from last_error


base = load_base_module()


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _norm_ascii(text: str) -> str:
    return base.normalize_space(base.ascii_simplify(text or "").lower())


def _no_punct(text: str) -> str:
    return base.normalize_space(re.sub(r"[^a-z0-9]+", " ", _norm_ascii(text)))


def _month_names() -> List[str]:
    return [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]


def _weekday_names() -> List[str]:
    return ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def target_day_tokens(target_day: date) -> Dict[str, Any]:
    weekday = target_day.strftime("%A").lower()
    month = target_day.strftime("%B").lower()
    day = target_day.day
    year = target_day.year

    return {
        "weekday": weekday,
        "month": month,
        "day": day,
        "year": year,
        "iso": target_day.isoformat(),
        "human_candidates": [
            f"{weekday} {day} {month} {year}",
            f"{weekday}, {month} {day}",
            f"{weekday} {month} {day}",
            f"{month} {day}",
            f"{day} {month}",
            f"{target_day.strftime('%d')} {month}",
            f"{month} {target_day.strftime('%d')}",
            f"{day}/{target_day.month}",
            f"{target_day.strftime('%d/%m')}",
            f"{target_day.month}/{day}",
            f"{target_day.strftime('%m/%d')}",
            target_day.isoformat(),
        ],
    }


def line_mentions_target_date(line: str, target_day: date) -> bool:
    ln = _norm_ascii(line)
    data = target_day_tokens(target_day)
    weekday = data["weekday"]
    month = data["month"]
    day = str(data["day"])
    day2 = f"{data['day']:02d}"
    year = str(data["year"])

    if data["iso"] in ln:
        return True

    has_weekday = re.search(rf"\b{re.escape(weekday)}\b", ln) is not None
    has_month = re.search(rf"\b{re.escape(month)}\b", ln) is not None
    has_day = re.search(rf"\b({day}|{day2})(st|nd|rd|th)?\b", ln) is not None
    has_year = year in ln

    if has_month and has_day:
        return True
    if has_weekday and (has_month or has_day or has_year):
        return True

    if has_weekday and re.search(r"\b(schedule|order of play|play|matches|draw|court|session)\b", ln):
        return True

    return False


def line_mentions_other_date(line: str, target_day: date) -> bool:
    ln = _norm_ascii(line)
    target = target_day_tokens(target_day)
    target_weekday = target["weekday"]
    target_month = target["month"]
    target_day_num = str(target["day"])
    target_day_num2 = f"{target['day']:02d}"

    weekdays = [x for x in _weekday_names() if x != target_weekday]
    if any(re.search(rf"\b{re.escape(w)}\b", ln) for w in weekdays):
        if re.search(r"\b(schedule|order of play|play|matches|draw|court|session)\b", ln):
            return True
        if any(m in ln for m in _month_names()):
            return True

    for month in _month_names():
        if month not in ln:
            continue
        days = re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", ln)
        for d in days:
            if month == target_month and d in {target_day_num, target_day_num2}:
                continue
            if 1 <= int(d) <= 31:
                return True

    return False


def extract_article_lines(html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return [base.title_name(x) for x in text.splitlines() if base.title_name(x)]


def build_name_alias_index(display_map: Dict[str, str], valid_player_keys: Set[str]) -> Dict[str, List[str]]:
    """
    alias -> liste de noms affichés.
    Si un alias court correspond à plusieurs joueurs, il ne sera pas utilisé.
    """
    tmp: Dict[str, Set[str]] = {}

    for key, display in display_map.items():
        if key not in valid_player_keys or not display:
            continue

        display_clean = base.clean_candidate_name(display)
        if not base.is_name_like(display_clean):
            continue

        parts = _no_punct(display_clean).split()
        aliases = {
            _no_punct(display_clean),
            _no_punct(display_clean).replace(" ", ""),
        }

        if len(parts) >= 2:
            aliases.add(parts[-1])
            aliases.add(f"{parts[0]} {parts[-1]}")
            aliases.add(f"{parts[0][0]} {parts[-1]}")

        for alias in aliases:
            if len(alias) >= 3:
                tmp.setdefault(alias, set()).add(display_clean)

    out: Dict[str, List[str]] = {}
    for alias, names in tmp.items():
        if len(names) == 1:
            out[alias] = list(names)

    return out


def find_player_in_fragment(
    fragment: str,
    alias_index: Dict[str, List[str]],
    valid_player_keys: Set[str],
) -> Optional[str]:
    raw = base.clean_candidate_name(fragment)
    key = base.canonical_name(raw)
    if key in valid_player_keys and base.is_name_like(raw):
        return raw

    norm = _no_punct(fragment)
    if not norm:
        return None

    for alias in sorted(alias_index.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", norm):
            return alias_index[alias][0]

    return None


def extract_pair_from_vs_line(
    line: str,
    alias_index: Dict[str, List[str]],
    valid_player_keys: Set[str],
) -> Optional[Tuple[str, str]]:
    cleaned = base.normalize_space(line)
    cleaned = re.sub(
        r"\b(Court|Stadium|Manolo Santana|Arantxa Sanchez|Not Before|NB|Starts at).*$",
        "",
        cleaned,
        flags=re.I,
    )

    if not re.search(r"\b(vs|v)\.?\b", cleaned, flags=re.I):
        return None

    parts = re.split(r"\b(?:vs|v)\.?\b", cleaned, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None

    left, right = parts[0], parts[1]

    if "/" in left or "/" in right:
        return None

    a = find_player_in_fragment(left, alias_index, valid_player_keys)
    b = find_player_in_fragment(right, alias_index, valid_player_keys)

    if not a or not b:
        return None
    if base.canonical_name(a) == base.canonical_name(b):
        return None

    return (a, b)


def extract_pairs_from_lines(
    lines: Sequence[str],
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
) -> List[Tuple[str, str]]:
    """
    Extraction sécurisée des paires de matchs.

    Correction V6.9 :
    - On ne fait plus de pairing automatique avec des noms consécutifs.
    - On accepte seulement :
      1) une ligne complète "Joueur A vs Joueur B" ;
      2) le format ATP officiel en plusieurs lignes :
         Joueur A
         Vs
         Joueur B
    """
    alias_index = build_name_alias_index(display_map, valid_player_keys)
    pairs: List[Tuple[str, str]] = []

    # 1) Cas simple : "Joueur A vs Joueur B" sur une même ligne.
    for ln in lines:
        pair = extract_pair_from_vs_line(ln, alias_index, valid_player_keys)
        if pair:
            pairs.append(pair)

    # 2) Cas ATP officiel : noms sur plusieurs lignes autour de "Vs".
    for i, ln in enumerate(lines):
        clean = base.normalize_space(ln)

        if not re.fullmatch(r"(?i)(vs|v\.?)", clean):
            continue

        player_a: Optional[str] = None
        player_b: Optional[str] = None

        # Chercher le joueur A dans les lignes précédentes.
        for j in range(i - 1, max(-1, i - 10), -1):
            candidate = find_player_in_fragment(lines[j], alias_index, valid_player_keys)
            if candidate:
                player_a = candidate
                break

        # Chercher le joueur B dans les lignes suivantes.
        for j in range(i + 1, min(len(lines), i + 10)):
            candidate = find_player_in_fragment(lines[j], alias_index, valid_player_keys)
            if candidate:
                player_b = candidate
                break

        if not player_a or not player_b:
            continue

        if base.canonical_name(player_a) == base.canonical_name(player_b):
            continue

        pairs.append((player_a, player_b))

    # 3) Déduplication stable.
    uniq: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for a, b in pairs:
        key = (base.canonical_name(a), base.canonical_name(b))
        uniq[key] = (a, b)

    return list(uniq.values())



def atp_daily_schedule_url_from_draw_url(draw_url: str) -> Optional[str]:
    """
    Transforme une URL ATP draw en URL ATP daily schedule.
    Exemple :
    /en/scores/current/rome/416/draws -> /en/scores/current/rome/416/daily-schedule
    """
    try:
        parsed = urlparse(draw_url)
        path = parsed.path or draw_url
        m = re.search(r"/scores/current/([^/]+)/([^/]+)", path)
        if not m:
            return None
        slug = m.group(1)
        event_id = m.group(2)
        return f"https://www.atptour.com/en/scores/current/{slug}/{event_id}/daily-schedule"
    except Exception:
        return None


def fetch_atp_daily_schedule_html(session, schedule_url: str) -> Tuple[str, List[str]]:
    """
    Récupère le HTML ATP Daily Schedule.

    Ordre volontaire :
    1) HTTP direct avec gros User-Agent : la page ATP daily-schedule expose souvent
       le programme dans le HTML déjà rendu. C'est plus stable sur Railway.
    2) Playwright + scroll si disponible.
    3) base.fetch_html en dernier secours.
    """
    audit: List[str] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.atptour.com/en/scores/current",
    }

    urls_to_try = [schedule_url]
    if "?" not in schedule_url:
        urls_to_try.append(schedule_url + "?matchType=Singles")

    # 1) HTTP direct avec la session existante.
    for url in urls_to_try:
        try:
            resp = session.get(url, headers=headers, timeout=45)
            text = resp.text or ""
            audit.append(f"daily_schedule_fetch=http_status:{getattr(resp, 'status_code', 'unknown')} len={len(text)} url={url}")
            if resp.status_code == 200 and len(text) > 5000 and ("Vs" in text or "Defeats" in text or "daily-schedule" in text):
                audit.append("daily_schedule_fetch=http_direct_ok")
                return text, audit
        except Exception as exc:
            audit.append(f"daily_schedule_fetch=http_direct_failed:{type(exc).__name__}:{exc}")

    # 2) Playwright + scroll, utile si ATP charge une partie du programme après rendu JS.
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(user_agent=headers["User-Agent"], viewport={"width": 1400, "height": 2400})
            page.goto(schedule_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            for selector in [
                "#onetrust-accept-btn-handler",
                "button:has-text('Accept')",
                "button:has-text('I Accept')",
                "button:has-text('Agree')",
                "button:has-text('OK')",
            ]:
                try:
                    page.locator(selector).first.click(timeout=1500)
                    page.wait_for_timeout(500)
                    break
                except Exception:
                    pass

            last_height = -1
            for _ in range(18):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(850)
                height = page.evaluate("document.body.scrollHeight")
                if height == last_height:
                    break
                last_height = height

            html = page.content()
            browser.close()
            audit.append(f"daily_schedule_fetch=playwright_scroll_ok len={len(html)}")
            return html, audit
    except Exception as exc:
        audit.append(f"daily_schedule_fetch=playwright_failed:{type(exc).__name__}:{exc}")

    # 3) Secours léger : fonction existante.
    try:
        html = base.fetch_html(session, schedule_url)
        audit.append(f"daily_schedule_fetch=base_fetch_html_ok len={len(html)}")
        return html, audit
    except Exception as exc:
        audit.append(f"daily_schedule_fetch=base_fetch_html_failed:{type(exc).__name__}:{exc}")
        return "", audit


def extract_schedule_pair_from_line(
    line: str,
    alias_index: Dict[str, List[str]],
    valid_player_keys: Set[str],
) -> Optional[Tuple[str, str]]:
    """
    Détecte une paire dans une ligne ATP si la ligne contient déjà :
    Joueur A vs Joueur B / Joueur A defeats Joueur B.
    """
    cleaned = base.normalize_space(line)
    marker = r"(?:vs|v\.?|defeats|def\.?|walkover|retired|retires)"
    if not re.search(rf"\b{marker}\b", cleaned, flags=re.I):
        return None

    parts = re.split(rf"\b{marker}\b", cleaned, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None

    left, right = parts[0], parts[1]
    if "/" in left or "/" in right:
        return None

    a = find_player_in_fragment(left, alias_index, valid_player_keys)
    b = find_player_in_fragment(right, alias_index, valid_player_keys)
    if not a or not b:
        return None
    if base.canonical_name(a) == base.canonical_name(b):
        return None
    return (a, b)


def _is_noise_schedule_line(line: str) -> bool:
    x = base.normalize_space(line or "")
    n = _norm_ascii(x)
    if not x:
        return True
    if n in {"image player photo", "player photo", "h2h", "wta", "atp", "followed by"}:
        return True
    if re.fullmatch(r"r\d+", n):
        return True
    if re.fullmatch(r"(q|wc|pr|ll|se)", n):
        return True
    if re.fullmatch(r"\(?\s*(q|wc|pr|ll|se)\s*\)?", n):
        return True
    if re.search(r"\b(starts at|not before|court|campo|arena|pietrangeli|centrale|refresh|print|use local time zone)\b", n):
        return True
    # Scores : 63, 76^{8}, 06 64 76 etc.
    if re.fullmatch(r"[0-9\s\^{}()]+", n):
        return True
    return False


def _find_near_schedule_player(
    lines: Sequence[str],
    start: int,
    stop: int,
    step: int,
    alias_index: Dict[str, List[str]],
    valid_player_keys: Set[str],
) -> Optional[str]:
    j = start
    while (j > stop if step < 0 else j < stop):
        ln = lines[j]
        if not _is_noise_schedule_line(ln) and "/" not in ln:
            candidate = find_player_in_fragment(ln, alias_index, valid_player_keys)
            if candidate:
                return candidate
        j += step
    return None


def extract_pairs_from_atp_daily_schedule_lines(
    lines: Sequence[str],
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
) -> List[Tuple[str, str]]:
    """
    Extraction officielle depuis ATP Daily Schedule.

    Sécurité :
    - aucune paire par noms consécutifs globaux ;
    - paire acceptée seulement autour d'un marqueur officiel ATP : Vs / Defeats ;
    - WTA ignoré automatiquement car les joueuses ne sont pas dans le points_map ATP ;
    - support des formats ATP en lignes séparées : Initiale. Nom / Vs / Initiale. Nom.
    """
    alias_index = build_name_alias_index(display_map, valid_player_keys)
    pairs: List[Tuple[str, str]] = []
    marker_line = re.compile(r"(?i)^(vs|v\.?|defeats|def\.?|walkover|retired|retires)$")

    # Cas 1 : tout sur la même ligne.
    for ln in lines:
        pair = extract_schedule_pair_from_line(ln, alias_index, valid_player_keys)
        if pair:
            pairs.append(pair)

    # Cas 2 : ATP en lignes séparées : joueur A / Vs|Defeats / joueur B.
    for i, ln in enumerate(lines):
        clean = base.normalize_space(ln)
        if not marker_line.fullmatch(clean):
            continue

        player_a = _find_near_schedule_player(
            lines,
            start=i - 1,
            stop=max(-1, i - 14),
            step=-1,
            alias_index=alias_index,
            valid_player_keys=valid_player_keys,
        )
        player_b = _find_near_schedule_player(
            lines,
            start=i + 1,
            stop=min(len(lines), i + 14),
            step=1,
            alias_index=alias_index,
            valid_player_keys=valid_player_keys,
        )

        if player_a and player_b and base.canonical_name(player_a) != base.canonical_name(player_b):
            pairs.append((player_a, player_b))

    uniq: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for a, b in pairs:
        key = (base.canonical_name(a), base.canonical_name(b))
        uniq[key] = (a, b)
    return list(uniq.values())


def parse_atp_daily_schedule_pairs(
    html: str,
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
    target_day: date,
) -> Tuple[List[Tuple[str, str]], List[str]]:
    lines = extract_article_lines(html)
    audit: List[str] = [f"daily_schedule_lines={len(lines)}"]

    if not lines:
        audit.append("daily_schedule_pairs=0")
        return [], audit

    sections, section_audit = split_target_sections(lines, target_day)
    audit.extend(section_audit)

    candidate_sections: List[List[str]] = []
    if sections:
        candidate_sections = sections
        audit.append(f"daily_schedule_date_sections_used={len(sections)}")
    else:
        # Beaucoup de pages ATP daily-schedule sont déjà filtrées sur le jour affiché.
        candidate_sections = [list(lines)]
        audit.append("daily_schedule_no_date_section_using_full_page=true")

    all_pairs: List[Tuple[str, str]] = []
    for section in candidate_sections:
        pairs = extract_pairs_from_atp_daily_schedule_lines(section, display_map, valid_player_keys)
        audit.append(f"daily_schedule_section_pairs={len(pairs)}")
        all_pairs.extend(pairs)

    uniq: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for a, b in all_pairs:
        key = (base.canonical_name(a), base.canonical_name(b))
        uniq[key] = (a, b)

    final_pairs = list(uniq.values())
    audit.append(f"daily_schedule_pairs={len(final_pairs)}")
    return final_pairs, audit


def split_target_sections(lines: Sequence[str], target_day: date) -> Tuple[List[List[str]], List[str]]:
    audit: List[str] = []
    sections: List[List[str]] = []

    target_indices = [i for i, ln in enumerate(lines) if line_mentions_target_date(ln, target_day)]
    audit.append(f"strict_date_headings={len(target_indices)}")

    for start in target_indices:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if line_mentions_other_date(lines[j], target_day):
                end = j
                break

        section = list(lines[start:end])
        if len(section) > 120:
            section = section[:120]
            audit.append("strict_section_clamped=120_lines")

        sections.append(section)
        audit.append(f"strict_section_start={start} lines={len(section)} heading={lines[start][:90]}")

    return sections, audit


def parse_article_pairs_strict_day(
    html: str,
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
    target_day: date,
    allow_undated_fallback: bool = False,
) -> Tuple[List[Tuple[str, str]], List[str]]:
    lines = extract_article_lines(html)
    audit: List[str] = [f"article_lines={len(lines)}"]

    sections, section_audit = split_target_sections(lines, target_day)
    audit.extend(section_audit)

    all_pairs: List[Tuple[str, str]] = []
    for section in sections:
        pairs = extract_pairs_from_lines(section, display_map, valid_player_keys)
        audit.append(f"strict_section_pairs={len(pairs)}")
        all_pairs.extend(pairs)

    if not all_pairs and allow_undated_fallback:
        pairs = extract_pairs_from_lines(lines, display_map, valid_player_keys)
        audit.append(f"undated_fallback_pairs={len(pairs)}")
        all_pairs.extend(pairs)

    uniq: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for a, b in all_pairs:
        key = (base.canonical_name(a), base.canonical_name(b))
        uniq[key] = (a, b)

    final_pairs = list(uniq.values())
    audit.append(f"strict_article_pairs={len(final_pairs)}")
    return final_pairs, audit


def parse_tournament_context_strict(
    session,
    draw_url: str,
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
    target_day: date,
    allow_undated_article_fallback: bool = False,
) -> Tuple[Any, List[str]]:
    ctx = base.parse_tournament_context(
        session=session,
        draw_url=draw_url,
        display_map=display_map,
        valid_player_keys=valid_player_keys,
        target_day=target_day,
    )

    audit: List[str] = []
    strict_pairs: List[Tuple[str, str]] = []

    # Source prioritaire : ATP Daily Schedule officiel, avec scroll Playwright si disponible.
    schedule_url = atp_daily_schedule_url_from_draw_url(draw_url)
    if schedule_url:
        audit.append(f"[ATP DAILY SCHEDULE] {ctx.tournament_name} | url={schedule_url}")
        schedule_html, fetch_audit = fetch_atp_daily_schedule_html(session, schedule_url)
        audit.extend(f"  {x}" for x in fetch_audit)
        schedule_pairs, schedule_parse_audit = parse_atp_daily_schedule_pairs(
            schedule_html,
            display_map,
            valid_player_keys,
            target_day,
        )
        audit.extend(f"  {x}" for x in schedule_parse_audit)
        if schedule_pairs:
            ctx.article_pairs = schedule_pairs
            audit.append(f"[ATP DAILY SCHEDULE OK] {ctx.tournament_name} | pairs={len(schedule_pairs)}")
            return ctx, audit

    # Fallback : articles ATP strictement datés. Ne sert que si daily schedule ne donne rien.
    article_urls = base.discover_schedule_article_urls(session, ctx.slug, target_day.year)

    audit.append(f"[STRICT ARTICLE FALLBACK] {ctx.tournament_name} | urls={len(article_urls)}")

    for article_url in article_urls:
        try:
            article_html = base.fetch_html(session, article_url)
            pairs, paudit = parse_article_pairs_strict_day(
                article_html,
                display_map,
                valid_player_keys,
                target_day,
                allow_undated_fallback=allow_undated_article_fallback,
            )

            audit.append(
                f"[STRICT ARTICLE URL] {ctx.tournament_name} | pairs={len(pairs)} | url={article_url}"
            )
            audit.extend(f"  {x}" for x in paudit)

            if pairs:
                strict_pairs = pairs
                break
        except Exception as exc:
            audit.append(f"[STRICT ARTICLE FAIL] {ctx.tournament_name} | {article_url} | {exc}")

    ctx.article_pairs = strict_pairs
    return ctx, audit


def build_tournament_contexts_strict(
    session,
    include_challenger: bool,
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
    target_day: date,
    allow_undated_article_fallback: bool = False,
) -> Tuple[List[Any], List[str]]:
    audit: List[str] = []
    draw_urls = base.discover_draw_urls(session, include_challenger=include_challenger)
    audit.append(f"draw_urls_found={len(draw_urls)}")

    contexts: List[Any] = []

    for draw_url in draw_urls:
        try:
            ctx, strict_audit = parse_tournament_context_strict(
                session=session,
                draw_url=draw_url,
                display_map=display_map,
                valid_player_keys=valid_player_keys,
                target_day=target_day,
                allow_undated_article_fallback=allow_undated_article_fallback,
            )
            contexts.append(ctx)
            audit.append(
                f"[CTX] {ctx.tournament_name} | players={len(ctx.player_keys)} | "
                f"strict_article_pairs={len(ctx.article_pairs)} | pending_pairs={len(ctx.pending_pairs)} | "
                f"completed_pairs={len(ctx.completed_pairs)} | qualifiers={len(ctx.qualifier_keys)} | "
                f"result_winners={ctx.result_winner_event_count} | result_qualifiers={ctx.result_qualifier_count} | "
                f"result_status={ctx.result_context_status} | surface={ctx.surface or 'None'}"
            )
            audit.extend(strict_audit)
        except Exception as exc:
            audit.append(f"[CTX FAIL] {draw_url} | {exc}")

    return contexts, audit


def build_day_matches_from_contexts_strict(
    contexts: List[Any],
    target_day: date,
    allow_draw_fallback: bool = False,
) -> Tuple[List[Any], List[str]]:
    audit: List[str] = []
    matches: List[Any] = []

    for ctx in contexts:
        if ctx.article_pairs:
            for a, b in ctx.article_pairs:
                matches.append(
                    base.DayMatch(
                        source="ATP News Schedule StrictDate",
                        tournament_name=ctx.tournament_name,
                        player_a=a,
                        player_b=b,
                        event_date=target_day.isoformat(),
                    )
                )
            audit.append(f"[DAYMATCH] {ctx.tournament_name} | source=strict_article | count={len(ctx.article_pairs)}")
            continue

        if allow_draw_fallback and ctx.pending_pairs:
            for a, b in ctx.pending_pairs:
                matches.append(
                    base.DayMatch(
                        source="ATP Draw Pending Fallback",
                        tournament_name=ctx.tournament_name,
                        player_a=a,
                        player_b=b,
                        event_date=target_day.isoformat(),
                    )
                )
            audit.append(f"[DAYMATCH] {ctx.tournament_name} | source=draw_pending_fallback | count={len(ctx.pending_pairs)}")
            continue

        audit.append(f"[DAYMATCH] {ctx.tournament_name} | source=strict_none | count=0")

    uniq: Dict[Tuple[str, str, str], Any] = {}
    for m in matches:
        key = (base.canonical_name(m.player_a), base.canonical_name(m.player_b), base.canonical_name(m.tournament_name))
        uniq[key] = m

    final_matches = list(uniq.values())
    audit.append(f"day_matches={len(final_matches)}")
    return final_matches, audit


def backend_send_is_disabled(backend_url: str, force_no_send: bool) -> bool:
    if force_no_send:
        return True
    try:
        parsed = urlparse(backend_url)
        if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 9:
            return True
    except Exception:
        pass
    return False


def write_outputs(
    target_day: date,
    audit: List[str],
    lines: List[str],
    payload_items: List[Any],
) -> Tuple[Path, Path, Path, Path, Path]:
    stamp = target_day.isoformat()

    lines_path = base.OUT_DIR / f"lines_{stamp}.txt"
    audit_path = base.OUT_DIR / f"audit_{stamp}.txt"
    payload_path = base.OUT_DIR / f"payload_{stamp}.json"
    result_json_path = base.OUT_DIR / f"result_{stamp}.json"
    result_txt_path = base.OUT_DIR / f"result_{stamp}.txt"

    unity_text = "\n".join(lines)

    base.write_text(lines_path, unity_text)
    base.write_text(audit_path, "\n".join(audit))
    base.write_text(base.UNITY_OUT_PATH, unity_text)
    base.write_text(base.LINES_LATEST_PATH, unity_text)
    base.write_text(base.AUDIT_LATEST_PATH, "\n".join(audit))

    payload_serialized = [asdict(x) for x in payload_items]
    base.write_json(payload_path, payload_serialized)
    base.write_json(PAYLOAD_LATEST_PATH, payload_serialized)

    return lines_path, audit_path, payload_path, result_json_path, result_txt_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_day", help="today | tomorrow | YYYY-MM-DD")
    parser.add_argument("--include-challenger", action="store_true")
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument(
        "--unsafe-assume-no-veto",
        action="store_true",
        help="Mode non recommandé : garde qualifier=false/wins=0 si contexte introuvable.",
    )
    parser.add_argument(
        "--allow-draw-fallback",
        action="store_true",
        help="Autorise le fallback draw pending si aucune section d'article datée n'est trouvée. Par défaut: désactivé.",
    )
    parser.add_argument(
        "--allow-undated-article-fallback",
        action="store_true",
        help="Autorise l'utilisation de tout l'article si aucune section datée n'est trouvée. Par défaut: désactivé.",
    )
    parser.add_argument(
        "--no-send-backend",
        action="store_true",
        help="Écrit seulement payload/audit/lines sans POST /calculate.",
    )
    parser.add_argument(
        "--backend-url",
        default="http://127.0.0.1:8000",
        help="URL du backend calculate. Port 9 = mode payload only anti-deadlock.",
    )
    args = parser.parse_args()

    if hasattr(base, "PLAYWRIGHT_HEADLESS"):
        base.PLAYWRIGHT_HEADLESS = not args.show_browser

    target_day = base.parse_target_day(args.target_day)
    session = base.build_session()
    strict_unknown_veto = not args.unsafe_assume_no_veto

    audit: List[str] = []
    audit.append(f"target_day={target_day.isoformat()}")
    audit.append(f"target_label={args.target_day}")
    audit.append(f"backend_url={args.backend_url}")
    audit.append(f"mode={MODE}")
    audit.append(f"strict_unknown_veto={str(strict_unknown_veto).lower()}")
    audit.append(f"allow_draw_fallback={str(args.allow_draw_fallback).lower()}")
    audit.append(f"allow_undated_article_fallback={str(args.allow_undated_article_fallback).lower()}")

    points_map, display_map = base.fetch_live_points_map(session)
    valid_player_keys = set(points_map.keys())
    audit.append(f"points_map_size={len(points_map)}")

    contexts, ctx_audit = build_tournament_contexts_strict(
        session=session,
        include_challenger=args.include_challenger,
        display_map=display_map,
        valid_player_keys=valid_player_keys,
        target_day=target_day,
        allow_undated_article_fallback=args.allow_undated_article_fallback,
    )
    audit.extend(ctx_audit)
    audit.append(f"contexts={len(contexts)}")

    day_matches, day_audit = build_day_matches_from_contexts_strict(
        contexts=contexts,
        target_day=target_day,
        allow_draw_fallback=args.allow_draw_fallback,
    )
    audit.extend(day_audit)

    try:
        lines, build_audit, payload_items = base.build_payload_items(
            day_matches=day_matches,
            contexts=contexts,
            points_map=points_map,
            strict_unknown_veto=strict_unknown_veto,
        )
    except TypeError:
        lines, build_audit, payload_items = base.build_payload_items(
            day_matches=day_matches,
            contexts=contexts,
            points_map=points_map,
        )

    audit.extend(build_audit)

    lines_path, audit_path, payload_path, result_json_path, result_txt_path = write_outputs(
        target_day=target_day,
        audit=audit,
        lines=lines,
        payload_items=payload_items,
    )

    if not payload_items:
        base.write_text(base.RESULT_LATEST_PATH, "Aucun match exploitable avec filtre strict par date.")
        base.write_text(result_txt_path, "Aucun match exploitable avec filtre strict par date.")
        base.write_json(result_json_path, {"matches": [], "summary": {"totalRows": 0, "validRows": 0}})
        print(f"UNITY_INPUT : {base.UNITY_OUT_PATH}")
        print(f"LINES       : {lines_path}")
        print(f"AUDIT       : {audit_path}")
        print(f"PAYLOAD     : {payload_path}")
        print(f"LINES_LATEST: {base.LINES_LATEST_PATH}")
        print(f"AUDIT_LATEST: {base.AUDIT_LATEST_PATH}")
        print("COUNT       : 0")
        print("Aucun match exploitable avec filtre strict par date.")
        return 0

    if backend_send_is_disabled(args.backend_url, args.no_send_backend):
        payload_only = {
            "status": "payload_only",
            "mode": MODE,
            "targetDay": target_day.isoformat(),
            "payloadCount": len(payload_items),
            "payloadPath": str(payload_path),
        }
        base.write_json(result_json_path, payload_only)
        base.write_text(result_txt_path, "PAYLOAD_ONLY - app.py calculera le moteur directement.")
        base.write_text(base.RESULT_LATEST_PATH, "PAYLOAD_ONLY - app.py calculera le moteur directement.")

        print(f"UNITY_INPUT : {base.UNITY_OUT_PATH}")
        print(f"LINES       : {lines_path}")
        print(f"AUDIT       : {audit_path}")
        print(f"PAYLOAD     : {payload_path}")
        print(f"RESULT_JSON : {result_json_path}")
        print(f"RESULT_TXT  : {result_txt_path}")
        print(f"LINES_LATEST: {base.LINES_LATEST_PATH}")
        print(f"AUDIT_LATEST: {base.AUDIT_LATEST_PATH}")
        print(f"RESULT_LATEST: {base.RESULT_LATEST_PATH}")
        print(f"COUNT       : {len(lines)}")
        print("PAYLOAD_ONLY")
        return 0

    result = base.send_to_backend(args.backend_url, payload_items)
    result_text = base.render_backend_result(result)

    base.write_json(result_json_path, result)
    base.write_text(result_txt_path, result_text)
    base.write_text(base.RESULT_LATEST_PATH, result_text)

    safe_result_text = result_text.replace("✅", "[JOUABLE]").replace("❌", "[PAS JOUABLE]")

    print(f"UNITY_INPUT : {base.UNITY_OUT_PATH}")
    print(f"LINES       : {lines_path}")
    print(f"AUDIT       : {audit_path}")
    print(f"PAYLOAD     : {payload_path}")
    print(f"RESULT_JSON : {result_json_path}")
    print(f"RESULT_TXT  : {result_txt_path}")
    print(f"LINES_LATEST: {base.LINES_LATEST_PATH}")
    print(f"AUDIT_LATEST: {base.AUDIT_LATEST_PATH}")
    print(f"RESULT_LATEST: {base.RESULT_LATEST_PATH}")
    print(f"COUNT       : {len(lines)}")
    print()
    print("--- RESULT PREVIEW ---")
    print(safe_result_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
