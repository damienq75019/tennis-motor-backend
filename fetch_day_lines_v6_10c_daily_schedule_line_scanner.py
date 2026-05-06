#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - Fetch daily lines V6.10C DAILY SCHEDULE LINE SCANNER

Objectif V6.10C :
- Source officielle UNIQUE des matchs du jour = pages ATP daily-schedule.
- Ne plus fabriquer la liste du jour depuis les draws pending ou les articles ATP.
- Exclure doubles / blocs parasites / anciennes paires.
- Garder le moteur existant inchangé.
- Garder la récupération points ATP existante de la V6.7.
- Garder le contexte draw/results seulement pour surface + veto Q/wins, jamais pour créer les matchs.

Utilisation :
    py fetch_day_lines_v6_10c_daily_schedule_line_scanner.py today --backend-url http://127.0.0.1:8000
    py fetch_day_lines_v6_10c_daily_schedule_line_scanner.py tomorrow --backend-url http://127.0.0.1:8000
    py fetch_day_lines_v6_10c_daily_schedule_line_scanner.py today --backend-url http://127.0.0.1:9

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

MODE = "V6_10C_DAILY_SCHEDULE_LINE_SCANNER"
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
            "evidence": normalize_space(evidence)[:260],
        }
    )
    return True


def _is_inside_player_link(node: Any) -> bool:
    parent = getattr(node, "parent", None)
    while parent is not None:
        if isinstance(parent, Tag):
            href = parent.get("href", "") if parent.name == "a" else ""
            if href and "/players/" in href:
                return True
        parent = getattr(parent, "parent", None)
    return False


def _text_to_small_lines(text: str) -> List[str]:
    raw = (text or "").replace("\xa0", " ")
    lines: List[str] = []

    for chunk in re.split(r"[\n\r\t]+", raw):
        chunk = normalize_space(chunk)
        if not chunk:
            continue

        # Les pages ATP mettent parfois plusieurs petits labels sur la même ligne.
        # On garde le texte original, puis on découpe seulement les séparateurs très nets.
        pieces = [normalize_space(x) for x in re.split(r"\s{2,}", chunk) if normalize_space(x)]

        if pieces:
            lines.extend(pieces)
        else:
            lines.append(chunk)

    return lines


def _status_from_text(text: str) -> str:
    t = normalize_space(text).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = normalize_space(t)

    if re.fullmatch(r"(vs|v)", t):
        return "Vs"

    if re.search(r"\bvs\b", t):
        return "Vs"

    if re.search(r"\bdefeats?\b", t):
        return "Defeats"

    if re.search(r"\bwalkover\b|\bw\/o\b|\bretired\b|\bret\b", t):
        return "Defeats"

    return ""


def _looks_like_score_or_noise(text: str) -> bool:
    t = normalize_space(text).lower()

    if not t:
        return True

    if block_has_bad_noise(t):
        return True

    # scores : 6-4, 7 6, 67 76 62, etc.
    scoreish = re.sub(r"[^0-9 ]+", " ", t)
    nums = re.findall(r"\b\d{1,2}\b", scoreish)
    if len(nums) >= 2 and not re.search(r"[a-z]", t):
        return True

    return False


def _is_wta_marker(text: str) -> bool:
    t = normalize_space(text).lower()
    return bool(re.search(r"\bwta\b|women|women's|femmes", t))


def _is_doubles_marker(text: str) -> bool:
    # Doubles : équipe avec slash, ou label doubles.
    t = normalize_space(text).lower()
    if re.search(r"[a-zà-ÿ]\s*/\s*[a-zà-ÿ]", t, flags=re.I):
        return True
    return bool(re.search(r"\bdoubles?\b", t))


def _collect_visible_tokens(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """
    Transforme la page ATP daily-schedule en flux visible :
    PLAYER, TEXT, PLAYER, TEXT...
    On ne garde les joueurs que depuis les liens ATP /players/.
    """
    tokens: List[Dict[str, str]] = []

    root = soup.body or soup

    for node in root.descendants:
        if isinstance(node, Tag):
            if node.name != "a":
                continue

            href = node.get("href", "") or ""
            if "/players/" not in href:
                continue

            name = full_name_from_href(href, node.get_text(" ", strip=True))
            name = clean_name(name)

            if not name or not is_name_like(name):
                continue

            # Evite les répétitions immédiates du même joueur.
            if tokens and tokens[-1].get("type") == "PLAYER" and canonical_name(tokens[-1].get("name", "")) == canonical_name(name):
                continue

            tokens.append(
                {
                    "type": "PLAYER",
                    "name": name,
                    "href": href,
                    "text": normalize_space(node.get_text(" ", strip=True)),
                }
            )
            continue

        if isinstance(node, NavigableString):
            if _is_inside_player_link(node):
                continue

            text = str(node or "")
            for line in _text_to_small_lines(text):
                if not line:
                    continue

                # Evite textes purement invisibles / caractères seuls.
                if len(line) == 1 and not re.search(r"[A-Za-z0-9]", line):
                    continue

                if tokens and tokens[-1].get("type") == "TEXT" and tokens[-1].get("text") == line:
                    continue

                tokens.append({"type": "TEXT", "text": line})

    return tokens


def _window_text(tokens: List[Dict[str, str]], start: int, end: int) -> str:
    parts: List[str] = []

    for token in tokens[max(0, start):min(len(tokens), end)]:
        if token.get("type") == "PLAYER":
            parts.append(token.get("name", ""))
        else:
            parts.append(token.get("text", ""))

    return normalize_space(" | ".join(x for x in parts if x))


def _find_next_status(tokens: List[Dict[str, str]], start: int, limit: int = 18) -> Tuple[int, str]:
    max_i = min(len(tokens), start + limit)

    for i in range(start, max_i):
        token = tokens[i]

        if token.get("type") == "PLAYER":
            # Si on tombe sur un autre joueur avant Vs/Defeats, ce n'était pas le bon départ.
            return -1, ""

        text = token.get("text", "")

        if _is_doubles_marker(text):
            return -1, ""

        status = _status_from_text(text)
        if status:
            return i, status

    return -1, ""


def _find_next_player(tokens: List[Dict[str, str]], start: int, limit: int = 22) -> int:
    max_i = min(len(tokens), start + limit)

    for i in range(start, max_i):
        token = tokens[i]

        if token.get("type") == "PLAYER":
            return i

        text = token.get("text", "")
        if _is_wta_marker(text) or _is_doubles_marker(text):
            return -1

    return -1


def extract_pairs_line_scanner(html: str, source_url: str) -> Tuple[List[Dict[str, str]], List[str]]:
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    tokens = _collect_visible_tokens(soup)

    audit: List[str] = []
    pairs: List[Dict[str, str]] = []
    seen_unordered: Set[Tuple[str, str]] = set()

    audit.append(f"visible_tokens={len(tokens)}")
    audit.append(f"visible_player_tokens={sum(1 for t in tokens if t.get('type') == 'PLAYER')}")

    added = 0

    for i, token in enumerate(tokens):
        if token.get("type") != "PLAYER":
            continue

        player_a = token.get("name", "")
        status_index, status = _find_next_status(tokens, i + 1)

        if status_index < 0:
            continue

        player_b_index = _find_next_player(tokens, status_index + 1)

        if player_b_index < 0:
            continue

        player_b = tokens[player_b_index].get("name", "")

        evidence = _window_text(tokens, i, player_b_index + 6)

        if _is_wta_marker(evidence) or _is_doubles_marker(evidence):
            continue

        if append_pair_if_valid(
            pairs,
            seen_unordered,
            player_a,
            player_b,
            f"ATP Daily Schedule Line Scanner {status}",
            source_url,
            evidence,
        ):
            added += 1

    audit.append(f"line_scanner_added={added}")
    audit.append(f"line_scanner_pairs={len(pairs)}")

    return pairs, audit


def extract_pairs_from_daily_schedule_html(html: str, source_url: str) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    V6.10C :
    Source unique = ATP daily-schedule.
    Extraction correcte : flux visible ligne par ligne.

    Motif accepté :
    ATP player link
    puis Vs / Defeats / Walkover / Retired
    puis ATP player link

    Exclusions :
    - WTA
    - doubles
    - blocs parasites
    """
    pairs, audit = extract_pairs_line_scanner(html, source_url)

    # Fallback de sécurité minimal : si aucun motif ligne n'est trouvé, on tente les petits blocs exacts.
    if pairs:
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

        if _is_wta_marker(text) or _is_doubles_marker(text) or block_has_bad_noise(text):
            continue

        if not re.search(r"\b(vs|v|defeats?|walkover|retired|ret)\b", text, flags=re.I):
            continue

        if append_pair_if_valid(
            fallback_pairs,
            seen_unordered,
            links[0][0],
            links[1][0],
            "ATP Daily Schedule Exact Block Fallback",
            source_url,
            text,
        ):
            exact_added += 1

    audit.append(f"exact_block_fallback_added={exact_added}")
    audit.append(f"daily_schedule_singles_pairs={len(fallback_pairs)}")

    return fallback_pairs, audit

def build_daily_schedule_matches(session, target_day: date, include_challenger: bool) -> Tuple[List[Any], List[str], List[Dict[str, str]], Dict[str, Optional[str]]]:
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

            rows, row_audit = extract_pairs_from_daily_schedule_html(html, url)
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
    audit.append("source_policy=ATP_DAILY_SCHEDULE_LINE_SCANNER_ONLY")
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
    )
    audit.extend(daily_audit)

    contexts, ctx_audit = build_contexts_for_daily_urls(
        session=session,
        schedule_rows=schedule_rows,
        display_map=display_map,
        valid_player_keys=valid_player_keys,
        surfaces_by_tournament=surfaces_by_tournament,
        strict_context=not args.minimal_context_only,
    )
    audit.extend(ctx_audit)
    audit.append(f"contexts_for_veto_only={len(contexts)}")

    # Construction payload officielle : la liste vient UNIQUEMENT de day_matches daily-schedule.
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
