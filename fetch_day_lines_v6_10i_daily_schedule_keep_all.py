#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - Fetch daily lines V6.10I DAILY SCHEDULE KEEP ALL

Objectif V6.10I :
- Source officielle UNIQUE des matchs du jour = pages ATP daily-schedule.
- Ne plus fabriquer la liste du jour depuis les draws pending ou les articles ATP.
- Exclure doubles / blocs parasites / anciennes paires.
- Garder le moteur existant inchangé.
- Garder la récupération points ATP existante de la V6.7.
- Garder le contexte draw/results seulement pour surface + veto Q/wins, jamais pour créer les matchs.

Utilisation :
    py fetch_day_lines_v6_10i_daily_schedule_keep_all.py today --backend-url http://127.0.0.1:8000
    py fetch_day_lines_v6_10i_daily_schedule_keep_all.py tomorrow --backend-url http://127.0.0.1:8000
    py fetch_day_lines_v6_10i_daily_schedule_keep_all.py today --backend-url http://127.0.0.1:9

Sorties :
    output/lines_YYYY-MM-DD.txt
    output/audit_YYYY-MM-DD.txt
    output/payload_YYYY-MM-DD.json
    output/payload_latest.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag


BASE_MODULE_CANDIDATES = [
    "fetch_day_lines_v6_7_results_context_fixed_safe_clamped",
    "fetch_day_lines_v6_6_results_context_fixed_safe",
    "fetch_day_lines_v6_5_results_context_safe",
]

MODE = "V6_10I_DAILY_SCHEDULE_KEEP_ALL"
PAYLOAD_LATEST_PATH = Path("output") / "payload_latest.json"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_base_module():
    last_error: Optional[BaseException] = None
    for name in BASE_MODULE_CANDIDATES:
        try:
            return importlib.import_module(name)
        except BaseException as exc:
            last_error = exc

    raise RuntimeError(
        "Impossible d'importer le script de base. "
        "Mets fetch_day_lines_v6_7_results_context_fixed_safe_clamped.py "
        "dans le même dossier que ce script."
    ) from last_error


base = load_base_module()


# ---------------------------------------------------------------------------
# Normalisation / clés de paires
# ---------------------------------------------------------------------------


def normalize_space(text: str) -> str:
    try:
        return base.normalize_space(text or "")
    except Exception:
        return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def clean_name(name: str) -> str:
    try:
        return base.clean_candidate_name(name or "")
    except Exception:
        name = normalize_space(name or "")
        name = re.sub(r"\s+\((Q|WC|LL|SE|PR)\)$", "", name, flags=re.I)
        return normalize_space(name)


def canonical_name(name: str) -> str:
    try:
        return base.canonical_name(name or "")
    except Exception:
        v = (name or "").lower()
        v = re.sub(r"[^a-z0-9]+", " ", v)
        return normalize_space(v)


def is_name_like(name: str) -> bool:
    try:
        return base.is_name_like(name)
    except Exception:
        parts = (name or "").split()
        return 2 <= len(parts) <= 5 and not any(ch.isdigit() for ch in name)


def unordered_pair_key(a: str, b: str) -> Tuple[str, str]:
    aa = canonical_name(a)
    bb = canonical_name(b)
    return tuple(sorted([aa, bb]))  # type: ignore[return-value]


def full_name_from_href(href: str, fallback: str = "") -> str:
    try:
        return clean_name(base.full_name_from_player_href(href, fallback))
    except Exception:
        m = re.search(r"/players/([^/]+)/", href or "")
        if m:
            slug = m.group(1).replace("-", " ")
            return clean_name(" ".join(part.capitalize() for part in slug.split()))
        return clean_name(fallback)


def tournament_slug_from_daily_url(url: str) -> str:
    m = re.search(r"/scores/current(?:-challenger)?/([^/]+)/", url or "", flags=re.I)
    if m:
        return m.group(1).strip().lower()

    m = re.search(r"/scores/archive/([^/]+)/", url or "", flags=re.I)
    if m:
        return m.group(1).strip().lower()

    return ""


def tournament_name_from_daily_url(url: str) -> str:
    slug = tournament_slug_from_daily_url(url)
    if not slug:
        return "ATP"
    try:
        return base.title_name(slug.replace("-", " "))
    except Exception:
        return normalize_space(slug.replace("-", " ")).title()


def daily_url_to_draw_url(url: str) -> str:
    out = url
    out = out.replace("/en/scores/current-challenger/", "/en/scores/archive/")
    out = out.replace("/en/scores/current/", "/en/scores/archive/")
    out = re.sub(r"/(daily-schedule|live-scores|results)(?:\?[^\s\"'<>]*)?$", "/draws", out)
    if not out.endswith("/draws"):
        out = re.sub(r"/(draws)(?:\?[^\s\"'<>]*)?$", "/draws", out)
    return out


def surface_from_text(text: str) -> Optional[str]:
    try:
        return base.maybe_surface_from_text(text or "")
    except Exception:
        t = (text or "").lower()
        if "clay" in t:
            return "Clay"
        if "grass" in t:
            return "Grass"
        if "hard" in t:
            return "Hard"
        return None


# ---------------------------------------------------------------------------
# Découverte des pages ATP daily-schedule
# ---------------------------------------------------------------------------


def discover_daily_schedule_urls(session, include_challenger: bool = False) -> List[str]:
    urls_to_scan = [base.ATP_CURRENT_URL]

    if include_challenger and hasattr(base, "ATP_CHALLENGER_URL"):
        urls_to_scan.append(base.ATP_CHALLENGER_URL)

    found_urls: List[str] = []

    patterns = [
        r"https://www\.atptour\.com/en/scores/current(?:-challenger)?/[^\"'\s<>]+/\d+/daily-schedule",
        r"/en/scores/current(?:-challenger)?/[^\"'\s<>]+/\d+/daily-schedule",
        r"https://www\.atptour\.com/en/scores/current(?:-challenger)?/[^\"'\s<>]+/\d+/(?:draws|results|live-scores)",
        r"/en/scores/current(?:-challenger)?/[^\"'\s<>]+/\d+/(?:draws|results|live-scores)",
    ]

    for url in urls_to_scan:
        html = base.fetch_html(session, url)

        for pat in patterns:
            for raw in re.findall(pat, html, flags=re.I):
                if raw.startswith("/"):
                    raw = "https://www.atptour.com" + raw

                raw = re.sub(
                    r"/(draws|results|live-scores)(?:\?[^\"'\s<>]*)?$",
                    "/daily-schedule",
                    raw,
                    flags=re.I,
                )

                if raw not in found_urls:
                    found_urls.append(raw)

    return found_urls


# ---------------------------------------------------------------------------
# Extraction DAILY-SCHEDULE uniquement
# ---------------------------------------------------------------------------


def extract_player_links_from_element(el) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    for a in el.find_all("a", href=True):
        href = a.get("href", "") or ""
        if "/players/" not in href:
            continue

        name = full_name_from_href(href, a.get_text(" ", strip=True))
        key = canonical_name(name)

        if not key or not is_name_like(name):
            continue

        if key in seen:
            continue

        seen.add(key)
        out.append((name, href))

    return out


def block_has_bad_noise(text: str) -> bool:
    t = normalize_space(text).lower()

    banned = [
        "privacy",
        "cookies",
        "tickets",
        "news",
        "highlights",
        "stats centre",
        "player stats",
        "head2head stats",
        "shop",
        "partners",
        "terms",
        "media",
        "official tennis player",
        "subscribe",
        "newsletter",
        "advertisement",
    ]

    return any(x in t for x in banned)


def append_pair_if_valid(
    pairs: List[Dict[str, str]],
    seen_unordered: Set[Tuple[str, str]],
    player_a: str,
    player_b: str,
    source: str,
    source_url: str,
    evidence: str,
) -> bool:
    a = clean_name(player_a)
    b = clean_name(player_b)

    if not a or not b:
        return False

    if not is_name_like(a) or not is_name_like(b):
        return False

    if canonical_name(a) == canonical_name(b):
        return False

    key = unordered_pair_key(a, b)

    if key in seen_unordered:
        return False

    seen_unordered.add(key)
    pairs.append(
        {
            "playerA": a,
            "playerB": b,
            "source": source,
            "sourceUrl": source_url,
            "evidence": normalize_space(evidence)[:320],
        }
    )
    return True


def _marker_name(name: str) -> str:
    name = clean_name(name)
    name = name.replace("[[ATP_PLAYER:", "").replace("]]", "")
    return name


def _replace_player_links_with_markers(soup: BeautifulSoup) -> None:
    """
    Remplace seulement les liens ATP /players/ par un marqueur texte.
    Important : les joueuses WTA de la page daily-schedule ne sont pas des liens ATP /players/,
    donc elles ne deviennent jamais des tokens PLAYER.
    """
    for a in soup.find_all("a", href=True):
        href = a.get("href", "") or ""

        if "/players/" not in href:
            continue

        name = full_name_from_href(href, a.get_text(" ", strip=True))
        name = clean_name(name)

        if not name or not is_name_like(name):
            continue

        a.clear()
        a.append(f"[[ATP_PLAYER:{name}]]")


def _line_to_tokens(line: str) -> List[Dict[str, str]]:
    """
    Convertit une ligne visible en tokens.
    Une ligne peut contenir un marqueur joueur + du texte autour.
    """
    tokens: List[Dict[str, str]] = []
    raw = normalize_space(line)

    if not raw:
        return tokens

    pattern = re.compile(r"\[\[ATP_PLAYER:(.*?)\]\]")

    pos = 0
    for m in pattern.finditer(raw):
        before = normalize_space(raw[pos:m.start()])
        if before:
            tokens.append({"type": "TEXT", "text": before})

        name = _marker_name(m.group(1))
        if name and is_name_like(name):
            tokens.append({"type": "PLAYER", "name": name})

        pos = m.end()

    after = normalize_space(raw[pos:])
    if after:
        tokens.append({"type": "TEXT", "text": after})

    if not tokens:
        tokens.append({"type": "TEXT", "text": raw})

    return tokens


def _visible_tokens_from_marked_soup(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """
    Méthode V6.10C corrigée :
    - on marque les liens ATP avant extraction texte ;
    - on lit la page ligne par ligne comme elle apparaît ;
    - on s'arrête à Latest news pour ne pas capter les widgets H2H / stats.
    """
    _replace_player_links_with_markers(soup)

    text = soup.get_text("\n", strip=True)

    tokens: List[Dict[str, str]] = []
    previous_player_key = ""

    for raw_line in text.splitlines():
        line = normalize_space(raw_line)

        if not line:
            continue

        low = line.lower()

        if "latest news" in low:
            break

        if "{{" in line or "}}" in line:
            continue

        for token in _line_to_tokens(line):
            if token.get("type") == "PLAYER":
                key = canonical_name(token.get("name", ""))

                # Supprime doublon immédiat seulement.
                if key and key == previous_player_key:
                    continue

                previous_player_key = key
                tokens.append(token)
            else:
                previous_player_key = ""
                tokens.append(token)

    return tokens


def _status_from_text(text: str) -> str:
    t = normalize_space(text).lower()
    t = re.sub(r"[^a-z0-9 /.-]+", " ", t)
    t = normalize_space(t)

    if re.fullmatch(r"(vs|v)", t):
        return "Vs"

    if re.search(r"\bvs\b", t):
        return "Vs"

    if re.search(r"\bdefeats?\b", t):
        return "Defeats"

    if re.search(r"\bwalkover\b|\bw/o\b|\bretired\b|\bret\b", t):
        return "Defeats"

    return ""


def _is_ignorable_between_player_and_status(text: str) -> bool:
    t = normalize_space(text).lower()

    if not t:
        return True

    if block_has_bad_noise(t):
        return False

    # Labels fréquents ATP entre joueur et statut.
    if re.fullmatch(r"\(?\s*(wc|q|ll|pr|se)\s*\)?", t, flags=re.I):
        return True

    if re.fullmatch(r"r\d{1,3}", t, flags=re.I):
        return True

    if re.fullmatch(r"(not before|followed by|starts at)\s*.*", t, flags=re.I):
        return True

    if re.fullmatch(r"(court|campo|stadium|arena|pietrangeli|centrale|supertennis).*", t, flags=re.I):
        return True

    # Score / H2H après le second joueur : ignoré par le scan.
    if re.search(r"\bh2h\b", t):
        return True

    if re.fullmatch(r"[0-9\s{}^.,-]+", t):
        return True

    return True


def _is_doubles_marker(text: str) -> bool:
    t = normalize_space(text).lower()

    if re.search(r"[a-zà-ÿ]\s*/\s*[a-zà-ÿ]", t, flags=re.I):
        return True

    return bool(re.search(r"\bdoubles?\b", t))


def _is_wta_marker(text: str) -> bool:
    t = normalize_space(text).lower()
    return bool(re.search(r"\bwta\b|women|women's|femmes", t))


def _token_text(token: Dict[str, str]) -> str:
    if token.get("type") == "PLAYER":
        return token.get("name", "")
    return token.get("text", "")


def _window_text(tokens: List[Dict[str, str]], start: int, end: int) -> str:
    return normalize_space(" | ".join(_token_text(t) for t in tokens[max(0, start):min(len(tokens), end)] if _token_text(t)))


def _find_status_after_player(tokens: List[Dict[str, str]], start: int, max_scan: int = 10) -> Tuple[int, str]:
    """
    Cherche Vs/Defeats après joueur A.
    Contrairement à l'ancienne version, on tolère (WC), (Q), R128, courts, etc.
    """
    end = min(len(tokens), start + max_scan)

    for i in range(start, end):
        token = tokens[i]

        if token.get("type") == "PLAYER":
            return -1, ""

        text = token.get("text", "")

        if _is_wta_marker(text) or _is_doubles_marker(text):
            return -1, ""

        status = _status_from_text(text)
        if status:
            return i, status

        if not _is_ignorable_between_player_and_status(text):
            return -1, ""

    return -1, ""


def _find_player_after_status(tokens: List[Dict[str, str]], start: int, max_scan: int = 14) -> int:
    """
    Cherche joueur B après Vs/Defeats.
    On tolère les marqueurs (Q), (PR), images, R128 et scores.
    """
    end = min(len(tokens), start + max_scan)

    for i in range(start, end):
        token = tokens[i]

        if token.get("type") == "PLAYER":
            return i

        text = token.get("text", "")

        if _is_wta_marker(text) or _is_doubles_marker(text):
            return -1

        # sinon on continue, même si c'est (Q)/(PR)/score/H2H
        continue

    return -1


def extract_pairs_line_scanner(html: str, source_url: str) -> Tuple[List[Dict[str, str]], List[str]]:
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    tokens = _visible_tokens_from_marked_soup(soup)

    audit: List[str] = []
    pairs: List[Dict[str, str]] = []
    seen_unordered: Set[Tuple[str, str]] = set()

    audit.append(f"visible_tokens={len(tokens)}")
    audit.append(f"visible_player_tokens={sum(1 for t in tokens if t.get('type') == 'PLAYER')}")

    added = 0
    i = 0

    while i < len(tokens):
        token = tokens[i]

        if token.get("type") != "PLAYER":
            i += 1
            continue

        player_a = token.get("name", "")
        status_index, status = _find_status_after_player(tokens, i + 1)

        if status_index < 0:
            i += 1
            continue

        player_b_index = _find_player_after_status(tokens, status_index + 1)

        if player_b_index < 0:
            i += 1
            continue

        player_b = tokens[player_b_index].get("name", "")
        evidence = _window_text(tokens, i, player_b_index + 5)

        # Sécurité doubles. Ne pas rejeter juste parce que la prochaine ligne WTA arrive après le match ATP.
        if _is_doubles_marker(evidence):
            i += 1
            continue

        if append_pair_if_valid(
            pairs,
            seen_unordered,
            player_a,
            player_b,
            f"ATP Daily Schedule Line Scanner Fixed {status}",
            source_url,
            evidence,
        ):
            added += 1
            i = player_b_index + 1
            continue

        i += 1

    audit.append(f"line_scanner_fixed_added={added}")
    audit.append(f"line_scanner_fixed_pairs={len(pairs)}")

    return pairs, audit



def _build_initial_alias_map(display_map: Dict[str, str]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}

    for full in (display_map or {}).values():
        full_name = clean_name(str(full or ""))
        if not full_name or not is_name_like(full_name):
            continue

        parts = full_name.split()
        if len(parts) < 2:
            continue

        first = canonical_name(parts[0])
        if not first:
            continue

        initial = first[0]
        surname = canonical_name(" ".join(parts[1:]))

        if not surname:
            continue

        alias_keys = {
            canonical_name(full_name),
            f"{initial} {surname}",
            f"{initial}. {surname}",
        }

        # Cas parfois affichés sans particules ou avec dernier mot seulement.
        last_word = canonical_name(parts[-1])
        if len(last_word) >= 4:
            alias_keys.add(f"{initial} {last_word}")
            alias_keys.add(f"{initial}. {last_word}")

        for key in alias_keys:
            if key and key not in aliases:
                aliases[key] = full_name

    return aliases


def _clean_visible_player_line(line: str) -> str:
    line = normalize_space(line)
    line = re.sub(r"^\(?\s*(Q|WC|LL|PR|SE)\s*\)?\s+", "", line, flags=re.I)
    line = re.sub(r"\s+\(?\s*(Q|WC|LL|PR|SE)\s*\)?$", "", line, flags=re.I)
    line = normalize_space(line)
    return line


def _resolve_visible_player_line(line: str, alias_map: Dict[str, str]) -> str:
    line = _clean_visible_player_line(line)

    if not line:
        return ""

    # Retire les scores / labels collés.
    if _status_from_text(line):
        return ""

    if _is_wta_marker(line) or _is_doubles_marker(line) or block_has_bad_noise(line):
        return ""

    low = normalize_space(line).lower()
    banned_exact = {
        "r128", "r64", "r32", "r16", "q", "wc", "pr", "ll", "se",
        "h2h", "image", "player photo", "followed by", "not before",
        "starts at", "court", "stats", "serve", "return", "pressure",
    }
    if low in banned_exact:
        return ""

    if re.fullmatch(r"[0-9\\s{}^.,-]+", low):
        return ""

    key = canonical_name(line)

    if key in alias_map:
        return alias_map[key]

    m = re.match(r"^([A-Za-zÀ-ÿ])\\.?\s+(.+)$", line)
    if m:
        initial = canonical_name(m.group(1))[:1]
        surname = canonical_name(m.group(2))
        for candidate in (f"{initial} {surname}", f"{initial}. {surname}"):
            if candidate in alias_map:
                return alias_map[candidate]

    return ""


def extract_pairs_text_fallback(
    html: str,
    source_url: str,
    display_map: Dict[str, str],
) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Fallback V6.10F :
    Lit le texte visible ATP ligne par ligne et résout les noms abrégés
    du type "F. Cina", "B. van de Zandschulp", "G. Mpetshi Perricard"
    grâce au display_map construit depuis les points ATP live.

    Cette méthode ne dépend pas des liens /players/ et sert à récupérer
    les matchs que le HTML de Railway ne donne pas sous forme de liens propres.
    """
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    alias_map = _build_initial_alias_map(display_map)
    audit: List[str] = []
    pairs: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    raw_text = soup.get_text("\n", strip=True)
    raw_lines = [normalize_space(x) for x in raw_text.splitlines() if normalize_space(x)]

    tokens: List[Dict[str, str]] = []

    for line in raw_lines:
        low = line.lower()

        if "latest news" in low:
            break

        if "{{" in line or "}}" in line:
            continue

        resolved = _resolve_visible_player_line(line, alias_map)
        if resolved:
            # Supprime doublons immédiats.
            if tokens and tokens[-1].get("type") == "PLAYER" and canonical_name(tokens[-1].get("name", "")) == canonical_name(resolved):
                continue
            tokens.append({"type": "PLAYER", "name": resolved, "raw": line})
            continue

        tokens.append({"type": "TEXT", "text": line})

    audit.append(f"text_fallback_aliases={len(alias_map)}")
    audit.append(f"text_fallback_tokens={len(tokens)}")
    audit.append(f"text_fallback_player_tokens={sum(1 for t in tokens if t.get('type') == 'PLAYER')}")

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token.get("type") != "PLAYER":
            i += 1
            continue

        player_a = token.get("name", "")
        status_index, status = _find_status_after_player(tokens, i + 1, max_scan=16)

        if status_index < 0:
            i += 1
            continue

        player_b_index = _find_player_after_status(tokens, status_index + 1, max_scan=18)

        if player_b_index < 0:
            i += 1
            continue

        player_b = tokens[player_b_index].get("name", "")
        evidence = _window_text(tokens, i, player_b_index + 5)

        if _is_doubles_marker(evidence):
            i += 1
            continue

        if append_pair_if_valid(
            pairs,
            seen,
            player_a,
            player_b,
            "ATP Daily Schedule Text Fallback",
            source_url,
            evidence,
        ):
            i = player_b_index + 1
            continue

        i += 1

    audit.append(f"text_fallback_pairs={len(pairs)}")

    for row in pairs[:40]:
        audit.append(f"[TEXT PAIR] {row.get('playerA')} vs {row.get('playerB')} | {row.get('evidence', '')}")

    return pairs, audit



def extract_pairs_from_daily_schedule_html(html: str, source_url: str, display_map: Optional[Dict[str, str]] = None) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    V6.10C FIXED :
    Source unique = ATP daily-schedule.
    Extraction par texte visible marqué avec les liens ATP /players/.

    Motif accepté :
    ATP_PLAYER A
    puis Vs / Defeats / Walkover / Retired
    puis ATP_PLAYER B

    Cette version ne rejette plus un match ATP parce qu'une ligne WTA arrive après.
    Elle tolère (WC), (Q), (PR), R128, H2H et les scores.
    """
    pairs, audit = extract_pairs_line_scanner(html, source_url)

    # V6.10F :
    # Même si le scanner par liens trouve déjà des paires, on complète avec
    # un fallback texte. Sur ATP daily-schedule certains matchs sont visibles
    # dans le texte rendu mais ne sortent pas proprement via les liens /players/.
    text_pairs, text_audit = extract_pairs_text_fallback(html, source_url, display_map or {})
    audit.extend(text_audit)

    seen_existing = {unordered_pair_key(x.get("playerA", ""), x.get("playerB", "")) for x in pairs}
    added_text = 0
    for row in text_pairs:
        key = unordered_pair_key(row.get("playerA", ""), row.get("playerB", ""))
        if key not in seen_existing:
            pairs.append(row)
            seen_existing.add(key)
            added_text += 1

    audit.append(f"text_fallback_added_to_pairs={added_text}")

    if pairs:
        audit.append(f"daily_schedule_singles_pairs={len(pairs)}")
        return pairs, audit

    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    seen_unordered: Set[Tuple[str, str]] = set()
    fallback_pairs: List[Dict[str, str]] = []
    exact_added = 0

    candidates = soup.find_all(["article", "li", "tr", "section", "div"])

    for el in sorted(candidates, key=lambda x: len(normalize_space(x.get_text(" ", strip=True)))):
        links = extract_player_links_from_element(el)
        text = normalize_space(el.get_text(" ", strip=True))

        if len(links) != 2:
            continue

        if _is_doubles_marker(text) or block_has_bad_noise(text):
            continue

        if not re.search(r"\b(vs|v|defeats?|walkover|retired|ret)\b", text, flags=re.I):
            continue

        if append_pair_if_valid(
            fallback_pairs,
            seen_unordered,
            links[0][0],
            links[1][0],
            "ATP Daily Schedule Exact Block Fallback Fixed",
            source_url,
            text,
        ):
            exact_added += 1

    audit.append(f"exact_block_fallback_added={exact_added}")
    audit.append(f"daily_schedule_singles_pairs={len(fallback_pairs)}")

    return fallback_pairs, audit

def build_daily_schedule_matches(session, target_day: date, include_challenger: bool, display_map: Optional[Dict[str, str]] = None) -> Tuple[List[Any], List[str], List[Dict[str, str]], Dict[str, Optional[str]]]:
    audit: List[str] = []
    schedule_urls = discover_daily_schedule_urls(session, include_challenger=include_challenger)
    audit.append(f"daily_schedule_urls_found={len(schedule_urls)}")

    all_rows: List[Dict[str, str]] = []
    surfaces_by_tournament: Dict[str, Optional[str]] = {}

    for url in schedule_urls:
        tournament_name = tournament_name_from_daily_url(url)
        try:
            html = base.fetch_html(session, url)
            page_surface = surface_from_text(html)
            surfaces_by_tournament[canonical_name(tournament_name)] = page_surface

            rows, row_audit = extract_pairs_from_daily_schedule_html(html, url, display_map or {})
            audit.append(f"[DAILY URL] {tournament_name} | pairs={len(rows)} | surface={page_surface or 'None'} | {url}")
            audit.extend(f"  {x}" for x in row_audit)

            for row in rows:
                row["tournament"] = tournament_name
                row["surface"] = page_surface or ""
                all_rows.append(row)
        except Exception as exc:
            audit.append(f"[DAILY URL FAIL] {tournament_name} | {url} | {exc}")

    # Dédoublonnage stable : paire + tournoi.
    seen: Set[Tuple[str, str, str]] = set()
    clean_rows: List[Dict[str, str]] = []

    for row in all_rows:
        a = row.get("playerA", "")
        b = row.get("playerB", "")
        tournament = row.get("tournament", "ATP")
        key_pair = unordered_pair_key(a, b)
        key = (key_pair[0], key_pair[1], canonical_name(tournament))

        if not key_pair[0] or not key_pair[1]:
            continue

        if key in seen:
            continue

        seen.add(key)
        clean_rows.append(row)

    day_matches: List[Any] = []

    for row in clean_rows:
        day_matches.append(
            base.DayMatch(
                source=row.get("source", "ATP Daily Schedule Only"),
                tournament_name=row.get("tournament", "ATP"),
                player_a=row.get("playerA", ""),
                player_b=row.get("playerB", ""),
                event_date=target_day.isoformat(),
            )
        )

    audit.append(f"day_matches_from_daily_schedule_only={len(day_matches)}")
    return day_matches, audit, clean_rows, surfaces_by_tournament


# ---------------------------------------------------------------------------
# Contexte surface + veto, jamais pour construire la liste du jour
# ---------------------------------------------------------------------------


def make_minimal_context(tournament_name: str, surface: Optional[str]) -> Any:
    return base.TournamentContext(
        tournament_name=tournament_name,
        slug=canonical_name(tournament_name).replace(" ", "-"),
        draw_url="",
        results_url="",
        surface=surface or "Clay",
        player_keys=set(),
        pending_pairs=[],
        article_pairs=[],
        completed_pairs=[],
        qualifier_keys=set(),
        qualifier_evidence={},
        result_wins_by_key={},
        result_qualifier_keys=set(),
        result_context_url="",
        result_context_status="daily_schedule_only_minimal_context",
        result_winner_event_count=0,
        result_qualifier_count=0,
    )


def build_contexts_for_daily_urls(
    session,
    schedule_rows: List[Dict[str, str]],
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
    surfaces_by_tournament: Dict[str, Optional[str]],
    strict_context: bool,
) -> Tuple[List[Any], List[str]]:
    audit: List[str] = []
    contexts: List[Any] = []
    seen_tournaments: Set[str] = set()

    # URLs uniques issues du daily-schedule.
    by_tournament: Dict[str, Dict[str, str]] = {}
    for row in schedule_rows:
        tournament = row.get("tournament", "ATP") or "ATP"
        key = canonical_name(tournament)
        if key not in by_tournament:
            by_tournament[key] = row

    for tournament_key, row in by_tournament.items():
        tournament_name = row.get("tournament", "ATP") or "ATP"
        source_url = row.get("sourceUrl", "") or ""
        surface = surfaces_by_tournament.get(tournament_key) or row.get("surface") or None

        if tournament_key in seen_tournaments:
            continue
        seen_tournaments.add(tournament_key)

        if not strict_context or not source_url:
            contexts.append(make_minimal_context(tournament_name, surface))
            audit.append(f"[CTX MINIMAL] {tournament_name} | surface={surface or 'Clay'}")
            continue

        draw_url = daily_url_to_draw_url(source_url)

        try:
            ctx = base.parse_tournament_context(
                session=session,
                draw_url=draw_url,
                display_map=display_map,
                valid_player_keys=valid_player_keys,
                target_day=base.parse_target_day("today") if False else date.today(),
            )

            # Correction du nom/surface si nécessaire.
            if not getattr(ctx, "tournament_name", ""):
                ctx.tournament_name = tournament_name
            if not getattr(ctx, "surface", None) and surface:
                ctx.surface = surface

            contexts.append(ctx)
            audit.append(
                f"[CTX] {tournament_name} | draw_url={draw_url} | "
                f"players={len(ctx.player_keys)} | completed={len(ctx.completed_pairs)} | "
                f"qualifiers={len(ctx.qualifier_keys)} | result_status={ctx.result_context_status} | "
                f"surface={ctx.surface or surface or 'None'}"
            )
        except Exception as exc:
            contexts.append(make_minimal_context(tournament_name, surface))
            audit.append(f"[CTX FALLBACK MINIMAL] {tournament_name} | {draw_url} | {exc}")

    if not contexts and schedule_rows:
        contexts.append(make_minimal_context("ATP", "Clay"))
        audit.append("[CTX MINIMAL] ATP | aucun contexte, fallback unique")

    return contexts, audit



# ---------------------------------------------------------------------------
# V6.10D - Construction payload directe depuis daily-schedule
# ---------------------------------------------------------------------------


def _points_for_name(name: str, points_map: Dict[str, int]) -> int:
    key = canonical_name(name)
    value = points_map.get(key, 0)

    try:
        return int(value or 0)
    except Exception:
        return 0


def _player_b_is_q_from_evidence(player_b: str, evidence: str) -> bool:
    """
    Détection simple et prudente du Q uniquement dans la fenêtre du match.
    """
    if not evidence:
        return False

    ev = normalize_space(evidence)
    b = re.escape(clean_name(player_b))

    # Cas : (Q) juste avant le joueur B.
    if re.search(rf"\(Q\).{{0,80}}{b}", ev, flags=re.I):
        return True

    # Cas : joueur B puis (Q).
    if re.search(rf"{b}.{{0,80}}\(Q\)", ev, flags=re.I):
        return True

    return False


def _surface_for_row(row: Dict[str, str], surfaces_by_tournament: Dict[str, Optional[str]]) -> str:
    tournament = row.get("tournament", "ATP") or "ATP"
    key = canonical_name(tournament)

    surface = row.get("surface") or surfaces_by_tournament.get(key) or ""

    if surface:
        sf = surface_from_text(surface) or surface
        if sf in {"Clay", "Hard", "Grass"}:
            return sf

    # Rome / Madrid / Monte-Carlo / Barcelona : terre battue.
    t = tournament.lower()
    if any(x in t for x in ["rome", "madrid", "monte", "barcelona", "munich", "geneva", "hamburg"]):
        return "Clay"

    return "Clay"


def build_payload_items_direct_from_schedule_rows(
    schedule_rows: List[Dict[str, str]],
    points_map: Dict[str, int],
    surfaces_by_tournament: Dict[str, Optional[str]],
    strict_unknown_veto: bool,
) -> Tuple[List[Any], List[str]]:
    """
    V6.10D :
    La liste du jour vient de ATP daily-schedule uniquement.
    On ne laisse plus base.build_payload_items supprimer des matchs parce qu'un contexte draw/results est incomplet.

    Filtre dur :
    - garde seulement les matchs où les deux joueurs ont des points ATP > 0 ;
    - si points manquants, audit clair mais pas de faux match inventé.
    """
    audit: List[str] = []
    payload_items: List[Any] = []

    missing_points: List[str] = []
    seen: Set[Tuple[str, str, str]] = set()

    for row in schedule_rows:
        player_a = clean_name(row.get("playerA", ""))
        player_b = clean_name(row.get("playerB", ""))
        tournament = row.get("tournament", "ATP") or "ATP"
        evidence = row.get("evidence", "") or ""

        if not player_a or not player_b:
            continue

        key_pair = unordered_pair_key(player_a, player_b)
        key = (key_pair[0], key_pair[1], canonical_name(tournament))

        if key in seen:
            continue

        seen.add(key)

        points_a = _points_for_name(player_a, points_map)
        points_b = _points_for_name(player_b, points_map)

        if points_a <= 0 or points_b <= 0:
            missing_points.append(
                f"{player_a} vs {player_b} | A_points={points_a} | B_points={points_b} | tournament={tournament}"
            )
            # V6.10I :
            # NE PLUS SUPPRIMER le match si un point ATP manque.
            # On garde les 16 matchs ATP daily-schedule.
            # Sécurité : un point manquant est remplacé par 1 pour éviter une division/logit impossible.
            # L'audit garde la ligne [MISSING POINTS] pour correction ultérieure des alias.
            if points_a <= 0:
                points_a = 1
            if points_b <= 0:
                points_b = 1

        surface = _surface_for_row(row, surfaces_by_tournament)

        player_b_is_qualifier = _player_b_is_q_from_evidence(player_b, evidence)

        # Sécurité officielle maison :
        # si surface Clay et contexte wins inconnu, on force wins B à 2 uniquement en mode strict.
        # Cela évite une validation verte artificielle contre un profil potentiellement qualifié/dynamique.
        if strict_unknown_veto and surface == "Clay":
            player_b_tournament_wins = 2
        else:
            player_b_tournament_wins = 0

        item = base.UnityPayloadItem(
            playerA=player_a,
            playerB=player_b,
            surface=surface,
            playerAPoints=points_a,
            playerBPoints=points_b,
            player_b_is_qualifier=bool(player_b_is_qualifier),
            player_b_tournament_wins=int(player_b_tournament_wins),
            tournament=tournament,
            source=row.get("source", "ATP Daily Schedule Full Payload"),
        )

        payload_items.append(item)

    audit.append(f"daily_schedule_rows_detected={len(schedule_rows)}")
    audit.append(f"payload_items_direct={len(payload_items)}")
    audit.append(f"missing_points_rows={len(missing_points)}")

    for line in missing_points[:80]:
        audit.append(f"[MISSING POINTS] {line}")

    return payload_items, audit


# ---------------------------------------------------------------------------
# Sorties / backend
# ---------------------------------------------------------------------------


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


def write_outputs(target_day: date, audit: List[str], lines: List[str], payload_items: List[Any]) -> Tuple[Path, Path, Path, Path, Path]:
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


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_day", help="today | tomorrow | YYYY-MM-DD")
    parser.add_argument("--include-challenger", action="store_true", help="Inclut ATP Challenger si demandé.")
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument(
        "--unsafe-assume-no-veto",
        action="store_true",
        help="Mode non recommandé : garde qualifier=false/wins=0 si contexte introuvable.",
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
    parser.add_argument(
        "--minimal-context-only",
        action="store_true",
        help="Ne parse pas les draws/results pour le veto. Plus rapide, mais force plus souvent le veto de sécurité sur terre.",
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
    audit.append("source_policy=ATP_DAILY_SCHEDULE_KEEP_ALL_ONLY")
    audit.append("missing_points_policy=keep_match_replace_missing_points_with_1")
    audit.append("no_draw_pending_for_day_matches=true")
    audit.append("no_article_schedule_for_day_matches=true")
    audit.append(f"strict_unknown_veto={str(strict_unknown_veto).lower()}")
    audit.append(f"include_challenger={str(args.include_challenger).lower()}")
    audit.append(f"minimal_context_only={str(args.minimal_context_only).lower()}")

    points_map, display_map = base.fetch_live_points_map(session)
    valid_player_keys = set(points_map.keys())
    audit.append(f"points_map_size={len(points_map)}")

    day_matches, daily_audit, schedule_rows, surfaces_by_tournament = build_daily_schedule_matches(
        session=session,
        target_day=target_day,
        include_challenger=args.include_challenger,
        display_map=display_map,
    )
    audit.extend(daily_audit)

    # V6.10D :
    # On ne construit plus le payload via base.build_payload_items,
    # car ce chemin peut supprimer des matchs lorsque le contexte draw/results est incomplet.
    # La liste vient uniquement de schedule_rows extrait de ATP daily-schedule.
    payload_items, build_audit = build_payload_items_direct_from_schedule_rows(
        schedule_rows=schedule_rows,
        points_map=points_map,
        surfaces_by_tournament=surfaces_by_tournament,
        strict_unknown_veto=strict_unknown_veto,
    )
    audit.extend(build_audit)

    lines: List[str] = []
    for item in payload_items:
        lines.append(
            f"{item.playerA};{item.playerB};{item.surface};"
            f"{item.playerAPoints};{item.playerBPoints};"
            f"{str(item.player_b_is_qualifier).lower()};{item.player_b_tournament_wins}"
        )

    audit.append(f"daily_matches_detected={len(day_matches)}")
    audit.append(f"payload_items={len(payload_items)}")

    lines_path, audit_path, payload_path, result_json_path, result_txt_path = write_outputs(
        target_day=target_day,
        audit=audit,
        lines=lines,
        payload_items=payload_items,
    )

    if not payload_items:
        result_empty = {
            "matches": [],
            "summary": {
                "totalRows": 0,
                "validRows": 0,
                "errorRows": 0,
                "nonAnalyzedRows": 0,
                "over80": 0,
                "vetoCount": 0,
                "jouables": 0,
            },
            "meta": {
                "mode": MODE,
                "targetDay": target_day.isoformat(),
                "message": "Aucun match ATP simple exploitable trouvé via ATP daily-schedule.",
                "dailySchedulePairs": len(day_matches),
                "payloadItems": 0,
            },
        }
        base.write_json(result_json_path, result_empty)
        base.write_text(result_txt_path, "Aucun match ATP simple exploitable trouvé via ATP daily-schedule.")
        base.write_text(base.RESULT_LATEST_PATH, "Aucun match ATP simple exploitable trouvé via ATP daily-schedule.")

        print(f"UNITY_INPUT : {base.UNITY_OUT_PATH}")
        print(f"LINES       : {lines_path}")
        print(f"AUDIT       : {audit_path}")
        print(f"PAYLOAD     : {payload_path}")
        print(f"RESULT_JSON : {result_json_path}")
        print(f"RESULT_TXT  : {result_txt_path}")
        print(f"LINES_LATEST: {base.LINES_LATEST_PATH}")
        print(f"AUDIT_LATEST: {base.AUDIT_LATEST_PATH}")
        print(f"RESULT_LATEST: {base.RESULT_LATEST_PATH}")
        print("COUNT       : 0")
        print("Aucun match ATP simple exploitable trouvé via ATP daily-schedule.")
        return 0

    if backend_send_is_disabled(args.backend_url, args.no_send_backend):
        payload_only = {
            "status": "payload_only",
            "mode": MODE,
            "targetDay": target_day.isoformat(),
            "dailySchedulePairs": len(day_matches),
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
