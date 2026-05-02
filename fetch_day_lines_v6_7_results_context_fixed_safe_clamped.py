#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import html as html_module
import json
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ATP_CURRENT_URL = "https://www.atptour.com/en/scores/current"
ATP_CHALLENGER_URL = "https://www.atptour.com/en/scores/current-challenger"
ATP_LIVE_RANKINGS_URL = "https://www.atptour.com/en/rankings/singles/live"
ATP_OFFICIAL_RANKINGS_URL = "https://www.atptour.com/en/rankings/singles?rankRange=0-5000"
ATP_LIVE_RANKINGS_FULL_URL = "https://www.atptour.com/en/rankings/singles/live?rankRange=0-5000"


OUT_DIR = Path("output")
CACHE_DIR = Path("cache")
OUT_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

UNITY_OUT_PATH = OUT_DIR / "unity_input.txt"
LINES_LATEST_PATH = OUT_DIR / "lines_latest.txt"
AUDIT_LATEST_PATH = OUT_DIR / "audit_latest.txt"
RESULT_LATEST_PATH = OUT_DIR / "result_latest.txt"
LIVE_POINTS_CACHE_JSON = CACHE_DIR / "live_points_map_latest.json"


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
SLEEP_BETWEEN_CALLS = 0.4
PLAYWRIGHT_HEADLESS = True


@dataclass
class DayMatch:
    source: str
    tournament_name: str
    player_a: str
    player_b: str
    event_date: str


@dataclass
class TournamentContext:
    tournament_name: str
    slug: str
    draw_url: str
    results_url: str
    surface: Optional[str]
    player_keys: Set[str]
    pending_pairs: List[Tuple[str, str]]
    article_pairs: List[Tuple[str, str]]
    completed_pairs: List[Tuple[str, str]]
    qualifier_keys: Set[str]
    qualifier_evidence: Dict[str, str]
    result_wins_by_key: Dict[str, int]
    result_qualifier_keys: Set[str]
    result_context_url: str
    result_context_status: str
    result_winner_event_count: int
    result_qualifier_count: int


@dataclass
class UnityPayloadItem:
    playerA: str
    playerB: str
    surface: str
    playerAPoints: int
    playerBPoints: int
    player_b_is_qualifier: bool
    player_b_tournament_wins: int
    tournament: str
    source: str


def canonical_name(name: str) -> str:
    v = (name or "").strip().lower()
    v = unicodedata.normalize("NFKD", v)
    v = "".join(ch for ch in v if not unicodedata.combining(ch))
    v = re.sub(r"[^a-z0-9\s\-']", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v


def ascii_simplify(text: str) -> str:
    v = unicodedata.normalize("NFKD", text or "")
    v = "".join(ch for ch in v if not unicodedata.combining(ch))
    return v


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def title_name(name: str) -> str:
    return normalize_space(name or "")


def parse_target_day(raw: str) -> date:
    raw = raw.strip().lower()
    today = date.today()
    if raw == "today":
        return today
    if raw == "tomorrow":
        return today + timedelta(days=1)
    return datetime.strptime(raw, "%Y-%m-%d").date()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    ensure_parent(path)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_key(url: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", url).strip("_")
    return CACHE_DIR / f"{safe}.html"


def distinct_keep_order(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def normalize_surface(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().lower()
    if "clay" in v:
        return "Clay"
    if "hard" in v:
        return "Hard"
    if "grass" in v:
        return "Grass"
    if "carpet" in v:
        return "Hard"
    return None


def maybe_surface_from_text(text: str) -> Optional[str]:
    for token in ("Clay", "Hard", "Grass", "Carpet"):
        if re.search(rf"\b{re.escape(token)}\b", text, flags=re.IGNORECASE):
            return normalize_surface(token)
    return None


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def tournament_slug_from_url(draw_url: str) -> str:
    parts = [p for p in draw_url.split("/") if p]
    for i, p in enumerate(parts):
        if p == "archive" and i + 1 < len(parts):
            return parts[i + 1].strip().lower()
    return ""


def tournament_name_from_url(draw_url: str) -> str:
    slug = tournament_slug_from_url(draw_url)
    return title_name(slug.replace("-", " "))


def clean_candidate_name(name: str) -> str:
    name = title_name(name)
    name = re.sub(r"\s+\(Q\)$", "", name, flags=re.I)
    name = re.sub(r"\s+\(WC\)$", "", name, flags=re.I)
    name = re.sub(r"\s+\(LL\)$", "", name, flags=re.I)
    name = re.sub(r"\s+\(SE\)$", "", name, flags=re.I)
    name = re.sub(r"\s+\(PR\)$", "", name, flags=re.I)
    return title_name(name)


def is_name_like(text: str) -> bool:
    text = clean_candidate_name(text)
    if not text:
        return False
    if len(text.split()) < 2:
        return False
    if len(text) > 60:
        return False
    if re.search(r"\d", text):
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    if re.search(
        r"\b(schedule|draw|h2h|stats|news|live|results|scores|court|order|play|qualifying|doubles|singles|cookies|privacy|terms|media|tour|radio|guide|official|club|partners|community|consent|search|filter|button|cookie)\b",
        text,
        flags=re.I,
    ):
        return False
    return True


def full_name_from_player_href(href: str, fallback_text: str) -> str:
    href = href or ""
    m = re.search(r"/players/([^/]+)/", href)
    if m:
        slug = m.group(1).strip().replace("-", " ")
        slug = re.sub(r"\s+", " ", slug).strip()
        parts = []
        for part in slug.split():
            if part.lower() in {"de", "da", "del", "van", "von", "di", "la", "le"}:
                parts.append(part.lower())
            else:
                parts.append(part.capitalize())
        nm = " ".join(parts).strip()
        if len(nm.split()) >= 2:
            return nm
    return title_name(fallback_text)


def fetch_html_via_playwright(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=PLAYWRIGHT_HEADLESS)
        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 2600},
        )

        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3500)

        for label in ("Accept", "I Accept", "Agree", "Tout accepter", "Accepter", "Allow All"):
            try:
                btn = page.get_by_role("button", name=label)
                if btn.count() > 0:
                    btn.first.click(timeout=1500)
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                pass

        for _ in range(6):
            try:
                page.mouse.wheel(0, 2400)
            except Exception:
                pass
            page.wait_for_timeout(900)

        try:
            page.mouse.wheel(0, -3000)
        except Exception:
            pass

        page.wait_for_timeout(1800)

        html = page.content()
        browser.close()
        return html


def fetch_html(session: requests.Session, url: str, use_cache: bool = True) -> str:
    cpath = cache_key(url)
    is_atp = "atptour.com" in url.lower()

    if use_cache and cpath.exists() and not is_atp:
        return cpath.read_text(encoding="utf-8", errors="ignore")

    if is_atp:
        html = fetch_html_via_playwright(url)
    else:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        html = resp.text

    if use_cache:
        write_text(cpath, html)

    time.sleep(SLEEP_BETWEEN_CALLS)
    return html


def name_to_slug_candidates(display_name: str) -> List[str]:
    raw = ascii_simplify(display_name).lower()
    raw = raw.replace("'", "")
    raw = raw.replace(".", "")
    raw = re.sub(r"[^a-z0-9\s\-]", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()

    cands = set()
    if raw:
        cands.add(raw.replace(" ", "-"))
        cands.add(raw.replace(" ", ""))
        cands.add(raw.replace(" ", "_"))
        cands.add(raw.replace(" ", "-").replace("--", "-"))

    return [x for x in cands if x]


def parse_ints(text: str) -> List[int]:
    return [int(x.replace(",", "")) for x in re.findall(r"\b\d{1,6}(?:,\d{3})*\b", text or "")]


def select_live_points_from_cells(headers: List[str], cells: List[str], row_text: str) -> Optional[int]:
    headers_norm = [normalize_space(ascii_simplify(h).lower()) for h in headers]
    cells_norm = [normalize_space(c) for c in cells]

    preferred_indices: List[int] = []

    for i, h in enumerate(headers_norm):
        if re.search(r"\blive\s*(points|pts)?\b", h):
            preferred_indices.append(i)

    for i, h in enumerate(headers_norm):
        if i not in preferred_indices and re.search(r"\b(points|pts)\b", h):
            preferred_indices.append(i)

    for idx in preferred_indices:
        if 0 <= idx < len(cells_norm):
            ints = [x for x in parse_ints(cells_norm[idx]) if 30 <= x <= 30000]
            if ints:
                return max(ints)

    tail_cells = cells_norm[-5:] if len(cells_norm) >= 5 else cells_norm
    tail_candidates: List[int] = []
    for cell in tail_cells:
        ints = [x for x in parse_ints(cell) if 30 <= x <= 30000]
        if ints:
            tail_candidates.extend(ints)

    if tail_candidates:
        return max(tail_candidates)

    row_candidates = [x for x in parse_ints(row_text) if 30 <= x <= 30000]
    if row_candidates:
        return max(row_candidates)

    return None


def extract_rankings_rows_from_page(page) -> Dict[str, Any]:
    data = page.evaluate(
        """
        () => {
          const clean = (s) => (s || "").replace(/\\u00a0/g, " ").replace(/\\s+/g, " ").trim();

          const tableCandidates = Array.from(document.querySelectorAll("table"));
          for (const table of tableCandidates) {
            const hasPlayer = !!table.querySelector("a[href*='/players/']");
            if (!hasPlayer) continue;

            const headers = Array.from(table.querySelectorAll("thead th, thead td, tr th"))
              .map(x => clean(x.innerText || x.textContent || ""))
              .filter(Boolean);

            const hasPointsHeader = headers.some(h => /points|pts|live/i.test(h));
            const rowNodes = Array.from(table.querySelectorAll("tbody tr")).length
              ? Array.from(table.querySelectorAll("tbody tr"))
              : Array.from(table.querySelectorAll("tr"));

            const rows = rowNodes.map(row => ({
              text: clean(row.innerText || row.textContent || ""),
              cells: Array.from(row.querySelectorAll("th,td"))
                .map(c => clean(c.innerText || c.textContent || ""))
                .filter(Boolean),
              links: Array.from(row.querySelectorAll("a[href*='/players/']"))
                .map(a => ({
                  href: a.getAttribute("href") || "",
                  text: clean(a.textContent || "")
                }))
                .filter(x => x.href)
            })).filter(r => r.links.length > 0);

            if (rows.length >= 20 || hasPointsHeader) {
              return {
                source: "table",
                headers,
                rows
              };
            }
          }

          const selectors = [
            "tr",
            "[role='row']",
            ".mega-table__row",
            ".rankings-table__row",
            ".player-row",
            ".table-rankings-wrapper tr"
          ];

          for (const sel of selectors) {
            const rowNodes = Array.from(document.querySelectorAll(sel))
              .filter(row => row.querySelector("a[href*='/players/']"));

            if (!rowNodes.length) continue;

            const rows = rowNodes.map(row => ({
              text: clean(row.innerText || row.textContent || ""),
              cells: Array.from(row.querySelectorAll("th,td,div,span"))
                .map(c => clean(c.innerText || c.textContent || ""))
                .filter(Boolean)
                .slice(0, 25),
              links: Array.from(row.querySelectorAll("a[href*='/players/']"))
                .map(a => ({
                  href: a.getAttribute("href") || "",
                  text: clean(a.textContent || "")
                }))
                .filter(x => x.href)
            })).filter(r => r.links.length > 0);

            if (rows.length >= 20) {
              return {
                source: "fallback_rows",
                headers: [],
                rows
              };
            }
          }

          return {
            source: "none",
            headers: [],
            rows: []
          };
        }
        """
    )
    return data if isinstance(data, dict) else {"source": "none", "headers": [], "rows": []}


def save_live_points_cache(points_map: Dict[str, int], display_map: Dict[str, str], source_url: str) -> None:
    """Sauvegarde la dernière table de points ATP exploitable.

    Rôle : si ATP bloque temporairement la page quelques heures plus tard,
    le backend peut continuer à utiliser la dernière table validée au lieu de
    mettre tous les joueurs à 0.
    """
    if not points_map:
        return

    payload = {
        "savedAtUnix": int(time.time()),
        "savedAt": datetime.now().isoformat(timespec="seconds"),
        "sourceUrl": source_url,
        "pointsMapSize": len(points_map),
        "pointsMap": points_map,
        "displayMap": display_map,
    }
    write_json(LIVE_POINTS_CACHE_JSON, payload)


def load_live_points_cache(max_age_hours: int = 72) -> Tuple[Dict[str, int], Dict[str, str]]:
    """Recharge le dernier cache points ATP si récent.

    Le cache est un filet de sécurité, pas une source prioritaire.
    """
    if not LIVE_POINTS_CACHE_JSON.exists():
        return {}, {}

    try:
        data = json.loads(LIVE_POINTS_CACHE_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}

    saved_at = int(data.get("savedAtUnix", 0) or 0)
    if saved_at <= 0:
        return {}, {}

    age_seconds = time.time() - saved_at
    if age_seconds > max_age_hours * 3600:
        return {}, {}

    raw_points = data.get("pointsMap", {})
    raw_display = data.get("displayMap", {})

    if not isinstance(raw_points, dict) or not isinstance(raw_display, dict):
        return {}, {}

    points_map: Dict[str, int] = {}
    display_map: Dict[str, str] = {}

    for key, value in raw_points.items():
        try:
            points = int(value)
        except Exception:
            continue
        if points > 0:
            points_map[str(key)] = points

    for key, value in raw_display.items():
        if isinstance(value, str) and value.strip():
            display_map[str(key)] = value.strip()

    if len(points_map) < 20:
        return {}, {}

    return points_map, display_map


def extract_points_map_from_rankings_url(url: str) -> Tuple[Dict[str, int], Dict[str, str], Dict[str, Any]]:
    points_map: Dict[str, int] = {}
    display_map: Dict[str, str] = {}
    debug_rows: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=PLAYWRIGHT_HEADLESS)
        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 3000},
        )

        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)

        for label in ("Accept", "I Accept", "Agree", "Tout accepter", "Accepter", "Allow All"):
            try:
                btn = page.get_by_role("button", name=label)
                if btn.count() > 0:
                    btn.first.click(timeout=1500)
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                pass

        deadline = time.time() + 42
        extracted = {"source": "none", "headers": [], "rows": []}
        best_extracted = extracted

        # Important : certaines pages ATP chargent les lignes progressivement.
        # On scrolle, mais on garde aussi le meilleur résultat observé.
        while time.time() < deadline:
            try:
                page.mouse.wheel(0, 2800)
            except Exception:
                pass
            page.wait_for_timeout(1100)

            extracted = extract_rankings_rows_from_page(page)
            if len(extracted.get("rows", [])) > len(best_extracted.get("rows", [])):
                best_extracted = extracted

            if len(best_extracted.get("rows", [])) >= 100:
                break

        headers = best_extracted.get("headers", []) or []
        rows = best_extracted.get("rows", []) or []
        seen = set()

        for row in rows:
            links = row.get("links", []) or []
            if not links:
                continue

            href = ""
            fallback = ""
            name = ""

            for link in links:
                href = link.get("href", "") or ""
                fallback = link.get("text", "") or ""
                name = clean_candidate_name(full_name_from_player_href(href, fallback))
                key = canonical_name(name)
                if key and is_name_like(name):
                    break
            else:
                continue

            key = canonical_name(name)

            if not key or key in seen:
                continue
            if not is_name_like(name):
                continue

            cells = [normalize_space(x) for x in (row.get("cells", []) or [])]
            row_text = normalize_space(row.get("text", "") or "")
            points = select_live_points_from_cells(headers, cells, row_text)

            debug_rows.append(
                {
                    "name": name,
                    "href": href,
                    "headers": headers,
                    "cells": cells,
                    "row_text": row_text,
                    "best_points_guess": points,
                }
            )

            if points is not None:
                points_map[key] = points
                display_map[key] = name

            seen.add(key)

        browser.close()

    debug = {
        "url": url,
        "extract_source": best_extracted.get("source", "none"),
        "rows_count": len(debug_rows),
        "points_map_size": len(points_map),
        "rows": debug_rows[:250],
    }
    return points_map, display_map, debug


def fetch_live_points_map(session: requests.Session) -> Tuple[Dict[str, int], Dict[str, str]]:
    del session

    # Source 1 : live rankings ATP.
    # Source 2 : official rankings ATP avec rankRange large.
    # Source 3 : live rankings ATP avec rankRange large.
    # Filet de sécurité : dernier cache local récent.
    urls = [
        ATP_LIVE_RANKINGS_URL,
        ATP_OFFICIAL_RANKINGS_URL,
        ATP_LIVE_RANKINGS_FULL_URL,
    ]

    debug_all: List[Dict[str, Any]] = []
    errors: List[str] = []
    best_points: Dict[str, int] = {}
    best_display: Dict[str, str] = {}
    best_source = ""

    for url in urls:
        try:
            points_map, display_map, debug = extract_points_map_from_rankings_url(url)
            debug_all.append(debug)

            if len(points_map) > len(best_points):
                best_points = points_map
                best_display = display_map
                best_source = url

            # Suffisant pour couvrir la majorité du tableau ATP.
            # Si la page 0-5000 fonctionne, on dépasse normalement très largement 100.
            if len(points_map) >= 100:
                save_live_points_cache(points_map, display_map, url)
                write_json(
                    OUT_DIR / "live_points_debug_noctx_daily.json",
                    {
                        "selected_url": url,
                        "points_map_size": len(points_map),
                        "debug_sources": debug_all,
                        "cache_used": False,
                    },
                )
                return points_map, display_map
        except Exception as exc:
            errors.append(f"{url} -> {exc}")

    if best_points:
        save_live_points_cache(best_points, best_display, best_source)
        write_json(
            OUT_DIR / "live_points_debug_noctx_daily.json",
            {
                "selected_url": best_source,
                "points_map_size": len(best_points),
                "debug_sources": debug_all,
                "errors": errors,
                "cache_used": False,
                "warning": "points_map partiel mais non vide",
            },
        )
        return best_points, best_display

    cached_points, cached_display = load_live_points_cache(max_age_hours=72)
    if cached_points:
        write_json(
            OUT_DIR / "live_points_debug_noctx_daily.json",
            {
                "selected_url": "cache/live_points_map_latest.json",
                "points_map_size": len(cached_points),
                "debug_sources": debug_all,
                "errors": errors,
                "cache_used": True,
                "warning": "ATP rankings indisponible temporairement : utilisation du dernier cache récent",
            },
        )
        return cached_points, cached_display

    write_json(
        OUT_DIR / "live_points_debug_noctx_daily.json",
        {
            "selected_url": "none",
            "points_map_size": 0,
            "debug_sources": debug_all,
            "errors": errors,
            "cache_used": False,
        },
    )

    raise RuntimeError("Aucun point ATP live extrait et aucun cache récent disponible.")

def discover_draw_urls(session: requests.Session, include_challenger: bool) -> List[str]:
    urls = [ATP_CURRENT_URL]
    if include_challenger:
        urls.append(ATP_CHALLENGER_URL)

    draw_urls: List[str] = []

    pattern = re.compile(r'https://www\.atptour\.com/en/scores/archive/[^"\']+/draws(?:\?[^"\']*)?')
    rel_pattern = re.compile(r'/en/scores/archive/[^"\']+/draws(?:\?[^"\']*)?')
    current_pat = re.compile(r'https://www\.atptour\.com/en/scores/current(?:-challenger)?/[^"\']+/draws(?:\?[^"\']*)?')
    rel_current_pat = re.compile(r'/en/scores/current(?:-challenger)?/[^"\']+/draws(?:\?[^"\']*)?')

    for url in urls:
        html = fetch_html(session, url)
        found = pattern.findall(html)
        found += ["https://www.atptour.com" + p for p in rel_pattern.findall(html)]
        found += current_pat.findall(html)
        found += ["https://www.atptour.com" + p for p in rel_current_pat.findall(html)]
        draw_urls.extend(found)

    clean = []
    for u in draw_urls:
        u = u.replace("/en/scores/current/", "/en/scores/archive/")
        u = u.replace("/en/scores/current-challenger/", "/en/scores/archive/")
        u = re.sub(r"/draws.*$", "/draws", u)
        clean.append(u)

    return distinct_keep_order(clean)


def extract_names_from_player_links(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    names: List[str] = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/players/" not in href:
            continue
        nm = full_name_from_player_href(href, a.get_text(" ", strip=True))
        nm = clean_candidate_name(nm)
        if is_name_like(nm):
            names.append(nm)

    if names:
        return names

    hrefs = re.findall(r'href="([^"]*/players/[^"]+)"', html, flags=re.I)
    hrefs += re.findall(r"href='([^']*/players/[^']+)'", html, flags=re.I)
    for href in hrefs:
        nm = full_name_from_player_href(href, "")
        nm = clean_candidate_name(nm)
        if is_name_like(nm):
            names.append(nm)

    return names



def text_has_qualifier_marker_for_name(text: str, display_name: str) -> bool:
    """
    Détection prudente du statut Q dans un bloc de draw ATP.

    On cherche surtout les marqueurs courts "(Q)", "[Q]" ou une cellule/zone
    explicitement libellée "Qualifier". On évite de valider seulement sur le mot
    "qualifying", trop présent dans la navigation des pages ATP.
    """
    if not text or not display_name:
        return False

    raw = normalize_space(text)
    raw_ascii = ascii_simplify(raw)
    name_ascii = ascii_simplify(clean_candidate_name(display_name))

    # Cas le plus fiable : le marqueur est directement dans le même bloc que le nom.
    direct_marker = re.compile(
        rf"({re.escape(name_ascii)}.{{0,80}}(\(Q\)|\[Q\]|\bQ\b))|"
        rf"((\(Q\)|\[Q\]|\bQ\b).{{0,80}}{re.escape(name_ascii)})",
        flags=re.I,
    )
    if direct_marker.search(raw_ascii):
        return True

    # Cas possible dans certains DOM : classe/label/aria contient qualifier.
    if re.search(r"\bqualifier\b", raw_ascii, flags=re.I) and name_ascii.lower() in raw_ascii.lower():
        return True

    return False


def extract_qualifier_keys_from_draw_html(
    html: str,
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
) -> Tuple[Set[str], Dict[str, str]]:
    """
    Extrait les joueurs marqués Q dans le draw.

    Retour :
    - qualifier_keys : clés canoniques des joueurs Q
    - qualifier_evidence : court texte d'audit pour vérifier pourquoi le joueur a été classé Q
    """
    soup = BeautifulSoup(html or "", "html.parser")
    qualifier_keys: Set[str] = set()
    evidence: Dict[str, str] = {}

    # 1) Méthode principale : autour des liens joueur ATP.
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/players/" not in href:
            continue

        name = clean_candidate_name(full_name_from_player_href(href, a.get_text(" ", strip=True)))
        key = canonical_name(name)

        if key not in valid_player_keys:
            continue

        blocks: List[str] = []

        # Texte du lien + ses attributs.
        attrs_text = " ".join(
            str(v) for v in a.attrs.values()
            if isinstance(v, (str, int, float)) or isinstance(v, list)
        )
        blocks.append(normalize_space(a.get_text(" ", strip=True) + " " + attrs_text))

        # Remonter quelques parents pour capter les badges proches.
        parent = a.parent
        for _ in range(5):
            if parent is None:
                break
            parent_text = parent.get_text(" ", strip=True)
            parent_attrs = " ".join(
                str(v) for v in getattr(parent, "attrs", {}).values()
                if isinstance(v, (str, int, float)) or isinstance(v, list)
            )
            blocks.append(normalize_space(parent_text + " " + parent_attrs))
            parent = parent.parent

        for block in blocks:
            if text_has_qualifier_marker_for_name(block, name):
                qualifier_keys.add(key)
                evidence[key] = block[:220]
                break

    # 2) Fallback : scan ligne par ligne du texte visible.
    text_visible = soup.get_text("\n", strip=True)
    lines = [normalize_space(x) for x in text_visible.splitlines() if normalize_space(x)]

    for key in valid_player_keys:
        if key in qualifier_keys:
            continue

        display = display_map.get(key)
        if not display:
            continue

        for line in lines:
            if text_has_qualifier_marker_for_name(line, display):
                qualifier_keys.add(key)
                evidence[key] = line[:220]
                break

    return qualifier_keys, evidence


def count_completed_wins_for_player(player_name: str, completed_pairs: List[Tuple[str, str]]) -> int:
    """
    Compte les matchs déjà terminés du tournoi impliquant ce joueur.

    Pour un joueur encore programmé dans le tableau, chaque match terminé dans
    lequel il apparaît est considéré comme une victoire déjà acquise dans le tournoi.
    C'est volontairement simple et auditable : si le joueur avait perdu, il ne devrait
    normalement plus être dans un match futur du même tableau.
    """
    key = canonical_name(player_name)
    if not key:
        return 0

    count = 0
    seen_pairs: Set[str] = set()

    for a, b in completed_pairs:
        a_key = canonical_name(a)
        b_key = canonical_name(b)
        pair_key = "|||".join(sorted([a_key, b_key]))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        if key == a_key or key == b_key:
            count += 1

    return count





# ---------------------------------------------------------------------------
# V6.5 - Fallback contexte Results ATP inspiré de debug_atp_context_mini_v2.py
# But : quand le draw dynamique ne donne aucun joueur, on tente de lire la page
# /current/<slug>/<id>/results pour détecter les vainqueurs déjà passés et les Q.
# ---------------------------------------------------------------------------

def context_name_aliases(display_name: str) -> List[str]:
    raw = ascii_simplify(clean_candidate_name(display_name)).lower()
    raw = raw.replace(".", " ")
    raw = raw.replace("-", " ")
    raw = raw.replace("'", " ")
    raw = normalize_space(raw)

    aliases = {raw, raw.replace(" ", "")}
    out: List[str] = []
    for alias in aliases:
        alias = normalize_space(alias)
        if alias and alias not in out:
            out.append(alias)
    return out


def discover_current_results_url(session: requests.Session, slug: str, fallback_results_url: str = "") -> str:
    candidates: List[str] = []

    if fallback_results_url:
        candidates.append(fallback_results_url.replace("/archive/", "/current/"))
        candidates.append(fallback_results_url)

    try:
        current_html = fetch_html(session, ATP_CURRENT_URL)
        patterns = [
            rf"https://www\.atptour\.com/en/scores/current/{re.escape(slug)}/\d+/results",
            rf"/en/scores/current/{re.escape(slug)}/\d+/results",
            rf"https://www\.atptour\.com/en/scores/current/{re.escape(slug)}/\d+/draws",
            rf"/en/scores/current/{re.escape(slug)}/\d+/draws",
            rf"https://www\.atptour\.com/en/scores/current/{re.escape(slug)}/\d+/live-scores",
            rf"/en/scores/current/{re.escape(slug)}/\d+/live-scores",
        ]
        for pat in patterns:
            for url in re.findall(pat, current_html, flags=re.I):
                if url.startswith("/"):
                    url = "https://www.atptour.com" + url
                url = url.replace("/draws", "/results").replace("/live-scores", "/results")
                candidates.append(url)
    except Exception:
        pass

    clean: List[str] = []
    for url in candidates:
        if url and url not in clean:
            clean.append(url)

    if not clean:
        raise RuntimeError(f"Impossible de trouver une URL results pour slug={slug}")

    return clean[0]


def collect_result_context_qualifiers(raw_html: str, visible_text: str, candidate_display_map: Dict[str, str]) -> Tuple[Set[str], Dict[str, str]]:
    qualifier_keys: Set[str] = set()
    evidence: Dict[str, str] = {}

    combined = html_module.unescape((raw_html or "") + "\n" + (visible_text or ""))

    pattern = re.compile(r"([A-ZÀ-ÿ][A-Za-zÀ-ÿ'’\-. ]{2,60}?)\s*\(\s*Q\s*\)", flags=re.I)
    for m in pattern.finditer(combined):
        name = clean_candidate_name(m.group(1))
        key = canonical_name(name)
        if key in candidate_display_map and is_name_like(name):
            qualifier_keys.add(key)
            evidence[key] = normalize_space(m.group(0))[:220]

    visible_simple = ascii_simplify(visible_text or "").lower()
    html_simple = ascii_simplify(html_module.unescape(raw_html or "")).lower()

    for key, display in candidate_display_map.items():
        if key in qualifier_keys:
            continue
        for alias in context_name_aliases(display):
            pat = re.compile(rf"\b{re.escape(alias)}\b\s*\(\s*q\s*\)", flags=re.I)
            if pat.search(visible_simple) or pat.search(html_simple):
                qualifier_keys.add(key)
                evidence[key] = f"targeted_q_alias={alias}"
                break

    return qualifier_keys, evidence


def extract_result_context_winner_events(
    visible_text: str,
    candidate_display_map: Dict[str, str],
) -> Tuple[Dict[str, int], List[Dict[str, str]]]:
    wins_map: Dict[str, int] = {}
    debug_events: List[Dict[str, str]] = []
    seen_event_keys: Set[str] = set()

    patterns = [
        re.compile(r"Game Set and Match\s+([^.\n]{2,80})\.", flags=re.I),
        re.compile(r"([A-ZÀ-ÿ][A-Za-zÀ-ÿ'’\-. ]{2,80}?)\s+wins the match", flags=re.I),
    ]

    for pat in patterns:
        for m in pat.finditer(visible_text or ""):
            winner_raw = clean_candidate_name(m.group(1))
            if not is_name_like(winner_raw):
                continue

            winner_key = canonical_name(winner_raw)
            matched_key = winner_key if winner_key in candidate_display_map else ""

            if not matched_key:
                winner_simple = ascii_simplify(winner_raw).lower()
                winner_simple = normalize_space(re.sub(r"[^a-z0-9\s]", " ", winner_simple))
                for key, display in candidate_display_map.items():
                    if winner_simple in context_name_aliases(display):
                        matched_key = key
                        break

            if not matched_key:
                continue

            start, end = m.span()
            left = max(0, start - 260)
            right = min(len(visible_text), end + 260)
            window = visible_text[left:right]

            event_key = matched_key + "||" + normalize_space(window)[:180]
            if event_key in seen_event_keys:
                continue
            seen_event_keys.add(event_key)

            wins_map[matched_key] = wins_map.get(matched_key, 0) + 1
            debug_events.append(
                {
                    "winner": candidate_display_map.get(matched_key, winner_raw),
                    "winner_key": matched_key,
                    "window_preview": normalize_space(window)[:240],
                }
            )

    for key in list(wins_map.keys()):
        # Le moteur veto ne distingue pas 2, 3, 4 ou 5 : dès que wins >= 2, veto.
        # On borne donc à 2 pour éviter d'afficher/envoyer des valeurs inutiles dans Unity.
        wins_map[key] = max(0, min(2, wins_map[key]))

    return wins_map, debug_events


def fetch_result_context_from_current_results(
    session: requests.Session,
    slug: str,
    fallback_results_url: str,
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
) -> Dict[str, Any]:
    candidate_display_map = {
        key: clean_candidate_name(display)
        for key, display in display_map.items()
        if key in valid_player_keys and display
    }

    if not slug or not candidate_display_map:
        return {
            "status": "skip_no_slug_or_candidates",
            "url": "",
            "wins_by_key": {},
            "qualifier_keys": set(),
            "qualifier_evidence": {},
            "winner_event_count": 0,
            "qualifier_count": 0,
            "debug_events": [],
        }

    try:
        results_url = discover_current_results_url(session, slug, fallback_results_url)
        raw_html = fetch_html(session, results_url)
        visible_text = BeautifulSoup(raw_html or "", "html.parser").get_text("\n", strip=True)

        qualifier_keys, qualifier_evidence = collect_result_context_qualifiers(
            raw_html=raw_html,
            visible_text=visible_text,
            candidate_display_map=candidate_display_map,
        )
        wins_by_key, debug_events = extract_result_context_winner_events(
            visible_text=visible_text,
            candidate_display_map=candidate_display_map,
        )

        return {
            "status": "ok",
            "url": results_url,
            "wins_by_key": wins_by_key,
            "qualifier_keys": qualifier_keys,
            "qualifier_evidence": qualifier_evidence,
            "winner_event_count": len(debug_events),
            "qualifier_count": len(qualifier_keys),
            "debug_events": debug_events[:25],
        }
    except Exception as exc:
        return {
            "status": f"fail:{exc}",
            "url": "",
            "wins_by_key": {},
            "qualifier_keys": set(),
            "qualifier_evidence": {},
            "winner_event_count": 0,
            "qualifier_count": 0,
            "debug_events": [],
        }


def extract_names_from_slug_scan(html: str, display_map: Dict[str, str], valid_player_keys: Set[str]) -> List[str]:
    normalized_html = ascii_simplify(html).lower()
    hits: List[Tuple[int, str]] = []

    for key in valid_player_keys:
        display = display_map.get(key)
        if not display:
            continue

        best_pos = None
        for slug in name_to_slug_candidates(display):
            pos = normalized_html.find(slug)
            if pos >= 0 and (best_pos is None or pos < best_pos):
                best_pos = pos

        if best_pos is not None:
            hits.append((best_pos, display))

    hits.sort(key=lambda x: x[0])
    return [clean_candidate_name(name) for _, name in hits]


def extract_names_from_visible_text(html: str, display_map: Dict[str, str], valid_player_keys: Set[str]) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    text = ascii_simplify(soup.get_text("\n", strip=True)).lower()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    canon_to_display = {k: clean_candidate_name(v) for k, v in display_map.items() if k in valid_player_keys}
    display_ascii = {k: ascii_simplify(v).lower() for k, v in canon_to_display.items()}

    found: List[str] = []

    for ln in lines:
        for key, disp in display_ascii.items():
            if disp in ln:
                found.append(canon_to_display[key])

    return found


def extract_ordered_valid_names(html: str, display_map: Dict[str, str], valid_player_keys: Set[str]) -> List[str]:
    merged: List[str] = []
    merged.extend(extract_names_from_player_links(html))
    merged.extend(extract_names_from_slug_scan(html, display_map, valid_player_keys))
    merged.extend(extract_names_from_visible_text(html, display_map, valid_player_keys))

    out: List[str] = []
    for nm in merged:
        nm = clean_candidate_name(nm)
        key = canonical_name(nm)
        if key in valid_player_keys and is_name_like(nm):
            out.append(nm)

    return distinct_keep_order(out)


def pair_consecutive_names(names: List[str]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    i = 0
    while i + 1 < len(names):
        a = names[i]
        b = names[i + 1]
        if canonical_name(a) != canonical_name(b):
            pairs.append((a, b))
        i += 2

    uniq = distinct_keep_order([f"{a}|||{b}" for a, b in pairs])
    out: List[Tuple[str, str]] = []
    for item in uniq:
        a, b = item.split("|||", 1)
        out.append((a, b))
    return out


def discover_schedule_article_urls(session: requests.Session, slug: str, year: int) -> List[str]:
    urls: List[str] = []

    if slug:
        urls.append(f"https://www.atptour.com/en/news/{slug}-{year}-schedule")

    try:
        current_html = fetch_html(session, ATP_CURRENT_URL)
        pattern = re.compile(r'href="([^"]*/en/news/[^"]*schedule[^"]*)"', flags=re.I)
        rels = pattern.findall(current_html)
        for rel in rels:
            if slug in rel.lower():
                if rel.startswith("http"):
                    urls.append(rel)
                else:
                    urls.append("https://www.atptour.com" + rel)
    except Exception:
        pass

    clean = []
    for u in urls:
        u = re.sub(r"#.*$", "", u)
        clean.append(u)

    return distinct_keep_order(clean)


def parse_article_pairs(html: str, display_map: Dict[str, str], valid_player_keys: Set[str]) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [title_name(x) for x in text.splitlines() if x.strip()]

    pairs: List[Tuple[str, str]] = []

    for ln in lines:
        if re.search(r"\bvs\b", ln, flags=re.I):
            parts = re.split(r"\bvs\b", ln, flags=re.I)
            if len(parts) == 2:
                a = clean_candidate_name(parts[0])
                b = clean_candidate_name(parts[1])
                if canonical_name(a) in valid_player_keys and canonical_name(b) in valid_player_keys:
                    pairs.append((a, b))

    if pairs:
        return pair_consecutive_names([x for pair in pairs for x in pair])

    ordered_names = extract_ordered_valid_names(html, display_map, valid_player_keys)
    return pair_consecutive_names(ordered_names)


def parse_tournament_context(
    session: requests.Session,
    draw_url: str,
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
    target_day: date,
) -> TournamentContext:
    slug = tournament_slug_from_url(draw_url)
    results_url = draw_url.replace("/draws", "/results")

    draw_html = fetch_html(session, draw_url)
    try:
        results_html = fetch_html(session, results_url)
    except Exception:
        results_html = ""

    draw_names = extract_ordered_valid_names(draw_html, display_map, valid_player_keys)
    draw_pairs = pair_consecutive_names(draw_names)

    completed_pair_keys: Set[frozenset] = set()
    completed_pairs: List[Tuple[str, str]] = []

    if results_html:
        result_names = extract_ordered_valid_names(results_html, display_map, valid_player_keys)
        result_pairs = pair_consecutive_names(result_names)
        completed_pairs = result_pairs
        for a, b in result_pairs:
            completed_pair_keys.add(frozenset({canonical_name(a), canonical_name(b)}))

    qualifier_keys, qualifier_evidence = extract_qualifier_keys_from_draw_html(
        draw_html,
        display_map,
        valid_player_keys,
    )

    result_ctx = fetch_result_context_from_current_results(
        session=session,
        slug=slug,
        fallback_results_url=results_url,
        display_map=display_map,
        valid_player_keys=valid_player_keys,
    )

    pending_pairs: List[Tuple[str, str]] = []
    for a, b in draw_pairs:
        pair_key = frozenset({canonical_name(a), canonical_name(b)})
        if pair_key not in completed_pair_keys:
            pending_pairs.append((a, b))

    pending_pairs = pair_consecutive_names([x for pair in pending_pairs for x in pair])

    full_text = BeautifulSoup(draw_html, "html.parser").get_text("\n", strip=True)
    if results_html:
        full_text += "\n" + BeautifulSoup(results_html, "html.parser").get_text("\n", strip=True)

    surface = maybe_surface_from_text(full_text)
    player_keys = set(canonical_name(x) for x in draw_names)

    article_pairs: List[Tuple[str, str]] = []
    for article_url in discover_schedule_article_urls(session, slug, target_day.year):
        try:
            article_html = fetch_html(session, article_url)
            pairs = parse_article_pairs(article_html, display_map, valid_player_keys)
            if pairs:
                article_pairs = pairs
                break
        except Exception:
            continue

    return TournamentContext(
        tournament_name=tournament_name_from_url(draw_url),
        slug=slug,
        draw_url=draw_url,
        results_url=results_url,
        surface=surface,
        player_keys=player_keys,
        pending_pairs=pending_pairs,
        article_pairs=article_pairs,
        completed_pairs=completed_pairs,
        qualifier_keys=qualifier_keys,
        qualifier_evidence=qualifier_evidence,
        result_wins_by_key=result_ctx.get("wins_by_key", {}),
        result_qualifier_keys=result_ctx.get("qualifier_keys", set()),
        result_context_url=str(result_ctx.get("url", "")),
        result_context_status=str(result_ctx.get("status", "")),
        result_winner_event_count=int(result_ctx.get("winner_event_count", 0)),
        result_qualifier_count=int(result_ctx.get("qualifier_count", 0)),
    )


def build_tournament_contexts(
    session: requests.Session,
    include_challenger: bool,
    display_map: Dict[str, str],
    valid_player_keys: Set[str],
    target_day: date,
) -> Tuple[List[TournamentContext], List[str]]:
    audit: List[str] = []
    draw_urls = discover_draw_urls(session, include_challenger=include_challenger)
    audit.append(f"draw_urls_found={len(draw_urls)}")

    contexts: List[TournamentContext] = []
    for draw_url in draw_urls:
        try:
            ctx = parse_tournament_context(
                session=session,
                draw_url=draw_url,
                display_map=display_map,
                valid_player_keys=valid_player_keys,
                target_day=target_day,
            )
            contexts.append(ctx)
            audit.append(
                f"[CTX] {ctx.tournament_name} | players={len(ctx.player_keys)} | "
                f"article_pairs={len(ctx.article_pairs)} | pending_pairs={len(ctx.pending_pairs)} | "
                f"completed_pairs={len(ctx.completed_pairs)} | qualifiers={len(ctx.qualifier_keys)} | "
                f"result_winners={ctx.result_winner_event_count} | result_qualifiers={ctx.result_qualifier_count} | "
                f"result_status={ctx.result_context_status} | surface={ctx.surface or 'None'}"
            )
        except Exception as e:
            audit.append(f"[CTX FAIL] {draw_url} | {e}")

    return contexts, audit


def build_day_matches_from_contexts(contexts: List[TournamentContext], target_day: date) -> Tuple[List[DayMatch], List[str]]:
    audit: List[str] = []
    matches: List[DayMatch] = []

    for ctx in contexts:
        if ctx.article_pairs:
            for a, b in ctx.article_pairs:
                matches.append(
                    DayMatch(
                        source="ATP News Schedule",
                        tournament_name=ctx.tournament_name,
                        player_a=a,
                        player_b=b,
                        event_date=target_day.isoformat(),
                    )
                )
            audit.append(f"[DAYMATCH] {ctx.tournament_name} | source=article | count={len(ctx.article_pairs)}")
            continue

        if ctx.pending_pairs:
            for a, b in ctx.pending_pairs:
                matches.append(
                    DayMatch(
                        source="ATP Draw Pending",
                        tournament_name=ctx.tournament_name,
                        player_a=a,
                        player_b=b,
                        event_date=target_day.isoformat(),
                    )
                )
            audit.append(f"[DAYMATCH] {ctx.tournament_name} | source=draw_pending | count={len(ctx.pending_pairs)}")
            continue

        audit.append(f"[DAYMATCH] {ctx.tournament_name} | source=none | count=0")

    uniq: Dict[Tuple[str, str, str], DayMatch] = {}
    for m in matches:
        key = (canonical_name(m.player_a), canonical_name(m.player_b), canonical_name(m.tournament_name))
        uniq[key] = m

    final_matches = list(uniq.values())
    audit.append(f"day_matches={len(final_matches)}")
    return final_matches, audit


def find_context_for_match(match: DayMatch, contexts: List[TournamentContext]) -> Optional[TournamentContext]:
    a_key = canonical_name(match.player_a)
    b_key = canonical_name(match.player_b)

    exact = []
    for ctx in contexts:
        if a_key in ctx.player_keys and b_key in ctx.player_keys:
            exact.append(ctx)

    if len(exact) == 1:
        return exact[0]

    if len(exact) > 1:
        league_key = canonical_name(match.tournament_name)
        for ctx in exact:
            if league_key and league_key in canonical_name(ctx.tournament_name):
                return ctx
        return exact[0]

    # Fallback utile quand l'ATP donne les matchs via un article schedule,
    # mais que le draw dynamique n'expose aucun joueur dans le HTML recupere.
    league_key = canonical_name(match.tournament_name)
    if league_key:
        same_tournament = [ctx for ctx in contexts if league_key in canonical_name(ctx.tournament_name)]
        if len(same_tournament) == 1:
            return same_tournament[0]

    if len(contexts) == 1:
        return contexts[0]

    return None

def build_payload_items(
    day_matches: List[DayMatch],
    contexts: List[TournamentContext],
    points_map: Dict[str, int],
    strict_unknown_veto: bool = True,
) -> Tuple[List[str], List[str], List[UnityPayloadItem]]:
    audit: List[str] = []
    lines: List[str] = []
    payload_items: List[UnityPayloadItem] = []

    for match in day_matches:
        a_key = canonical_name(match.player_a)
        b_key = canonical_name(match.player_b)

        pa = points_map.get(a_key)
        pb = points_map.get(b_key)

        if pa is None or pb is None:
            audit.append(f"[SKIP points] {match.player_a} vs {match.player_b} | points introuvables")
            continue

        ctx = find_context_for_match(match, contexts)

        surface = "Clay"
        if ctx is not None and ctx.surface:
            surface = ctx.surface

        q_b = False
        wins_b = 0
        q_evidence = ""
        veto_context_status = "ctx_missing"

        if ctx is not None:
            b_key_for_ctx = canonical_name(match.player_b)
            has_draw_context = bool(ctx.player_keys or ctx.completed_pairs or ctx.qualifier_keys)
            has_result_context = (
                ctx.result_context_status == "ok"
                and (ctx.result_winner_event_count > 0 or ctx.result_qualifier_count > 0)
            )

            if has_draw_context:
                q_b = b_key_for_ctx in ctx.qualifier_keys
                wins_b = count_completed_wins_for_player(match.player_b, ctx.completed_pairs)
                q_evidence = ctx.qualifier_evidence.get(b_key_for_ctx, "")
                veto_context_status = "ctx_draw_ok"
            elif has_result_context:
                q_b = b_key_for_ctx in ctx.result_qualifier_keys
                wins_b = int(ctx.result_wins_by_key.get(b_key_for_ctx, 0))
                wins_b = max(0, min(2, wins_b))
                q_evidence = "RESULTS_CONTEXT"
                if q_b:
                    q_evidence = "RESULTS_CONTEXT_Q"
                veto_context_status = f"ctx_results_ok events={ctx.result_winner_event_count} url={ctx.result_context_url}"
            else:
                veto_context_status = f"ctx_no_draw_players_no_results result_status={ctx.result_context_status}"

        # Regle de securite : sur Clay, si le contexte Q/wins est introuvable,
        # on ne doit pas fabriquer un faux false;0 qui pourrait valider un vert.
        # On force wins=2 uniquement si aucun contexte draw/results exploitable n'a ete trouve.
        # Important : ctx_draw_ok et ctx_results_ok sont des contextes connus ; ils ne doivent pas etre forces.
        context_is_known = (
            veto_context_status.startswith("ctx_draw_ok")
            or veto_context_status.startswith("ctx_results_ok")
        )
        if strict_unknown_veto and normalize_surface(surface) == "Clay" and not context_is_known:
            q_b = False
            wins_b = 2
            q_evidence = "SAFE_UNKNOWN_VETO_FORCED_WINS_2"
            veto_context_status += " -> safe_forced_wins_2"

        # Borne finale : 0, 1 ou 2 seulement. Pour le veto, 2 signifie "2 ou plus".
        wins_b = max(0, min(2, int(wins_b)))

        audit.append(
            f"[VETOCTX APPLY] {match.player_a} vs {match.player_b} | "
            f"qualifier={str(q_b).lower()} | wins={wins_b} | surface={surface} | "
            f"veto_context={veto_context_status} | q_evidence={q_evidence}"
        )

        line = (
            f"{title_name(match.player_a)};"
            f"{title_name(match.player_b)};"
            f"{surface};"
            f"{pa};"
            f"{pb};"
            f"{str(q_b).lower()};"
            f"{wins_b}"
        )
        lines.append(line)

        payload_items.append(
            UnityPayloadItem(
                playerA=title_name(match.player_a),
                playerB=title_name(match.player_b),
                surface=surface,
                playerAPoints=pa,
                playerBPoints=pb,
                player_b_is_qualifier=q_b,
                player_b_tournament_wins=wins_b,
                tournament=match.tournament_name,
                source=match.source,
            )
        )

    return lines, audit, payload_items


def render_backend_result(result: Dict[str, Any]) -> str:
    summary = result.get("summary", {}) or {}
    matches = result.get("matches", []) or []
    engine = result.get("engine", {}) or {}

    lines: List[str] = []
    lines.append("Résumé")
    lines.append(f"- Lignes totales : {summary.get('totalRows', 0)}")
    lines.append(f"- Lignes valides : {summary.get('validRows', 0)}")
    lines.append(f"- Lignes en erreur : {summary.get('errorRows', 0)}")
    lines.append(f"- Premium > 80% : {summary.get('over80', 0)}")
    lines.append(f"- Veto : {summary.get('vetoCount', 0)}")
    lines.append(f"- Jouables : {summary.get('jouables', 0)}")
    lines.append("")
    lines.append("Résultats")
    lines.append("")

    for row in matches:
        if "error" in row:
            lines.append(f"{row.get('playerA', '')} vs {row.get('playerB', '')} ({row.get('surface', '')})")
            lines.append(f"Erreur : {row.get('error', '')}")
            lines.append("")
            continue

        lines.append(f"{row.get('playerA', '')} vs {row.get('playerB', '')} ({row.get('surface', '')})")
        if "playerAPoints" in row and "playerBPoints" in row:
            lines.append(f"Points ATP : {row.get('playerAPoints')} vs {row.get('playerBPoints')}")
        if "player_b_is_qualifier" in row and "player_b_tournament_wins" in row:
            lines.append(
                f"Qualifier B : {row.get('player_b_is_qualifier')} | "
                f"Wins tournoi B : {row.get('player_b_tournament_wins')}"
            )
        if "sweA" in row and "sweB" in row:
            lines.append(f"SWE : {row.get('sweA')} vs {row.get('sweB')}")
        if "pSwe" in row and "pAtp" in row:
            lines.append(f"pSwe : {row.get('pSwe')} | pAtp : {row.get('pAtp')}")
        if "pRank" in row:
            extra = [f"pRank : {row.get('pRank')}"]
            if "pForm5" in row:
                extra.append(f"pForm5 : {row.get('pForm5')}")
            if "pForm10" in row:
                extra.append(f"pForm10 : {row.get('pForm10')}")
            if "pSurfaceForm5" in row:
                extra.append(f"pSurfaceForm5 : {row.get('pSurfaceForm5')}")
            if "pDominance" in row:
                extra.append(f"pDominance : {row.get('pDominance')}")
            lines.append(" | ".join(extra))
        if isinstance(row.get("premiumPct", None), (int, float)):
            lines.append(f"Premium : {row.get('premiumPct')}%")
        else:
            lines.append(f"Premium : {row.get('premium', '')}")
        lines.append(f"Veto : {row.get('veto', '')}")
        lines.append(f"Décision : {row.get('decision', '')}")
        lines.append("")

    if engine:
        lines.append("Moteur")
        lines.append(f"- Nom : {engine.get('name', '')}")
        lines.append(f"- Version : {engine.get('version', '')}")
        lines.append(f"- Lignes historiques chargées : {engine.get('historyRowsLoaded', 0)}")
        lines.append(f"- Formule Premium : {engine.get('premiumFormula', '')}")
        lines.append(f"- Seuil : {engine.get('threshold', '')}")

    return "\n".join(lines)


def send_to_backend(backend_url: str, payload_items: List[Any]) -> Dict[str, Any]:
    url = backend_url.rstrip("/") + "/calculate"
    body = {"matches": [asdict(x) for x in payload_items]}

    resp = requests.post(url, json=body, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, dict):
        raise RuntimeError("Réponse backend invalide.")
    if "error" in data:
        raise RuntimeError(str(data["error"]))

    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_day", help="today | tomorrow | YYYY-MM-DD")
    parser.add_argument("--include-challenger", action="store_true")
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument(
        "--unsafe-assume-no-veto",
        action="store_true",
        help="Mode non recommande : si le contexte draw/results est introuvable, garde qualifier=false et wins=0 au lieu de proteger par veto inconnu.",
    )
    parser.add_argument(
        "--backend-url",
        default="http://127.0.0.1:8000",
        help="URL du backend calculate",
    )
    args = parser.parse_args()

    global PLAYWRIGHT_HEADLESS
    PLAYWRIGHT_HEADLESS = not args.show_browser

    target_day = parse_target_day(args.target_day)
    session = build_session()
    strict_unknown_veto = not args.unsafe_assume_no_veto

    audit: List[str] = []
    audit.append(f"target_day={target_day.isoformat()}")
    audit.append(f"target_label={args.target_day}")
    audit.append(f"backend_url={args.backend_url}")
    audit.append("mode=V6_7_RESULTS_CONTEXT_FIXED_SAFE_CLAMPED")
    audit.append(f"strict_unknown_veto={str(strict_unknown_veto).lower()}")
    audit.append("wins_output_clamp=max_2")

    points_map, display_map = fetch_live_points_map(session)
    valid_player_keys = set(points_map.keys())
    audit.append(f"points_map_size={len(points_map)}")

    contexts, ctx_audit = build_tournament_contexts(
        session=session,
        include_challenger=args.include_challenger,
        display_map=display_map,
        valid_player_keys=valid_player_keys,
        target_day=target_day,
    )
    audit.extend(ctx_audit)
    audit.append(f"contexts={len(contexts)}")

    day_matches, day_audit = build_day_matches_from_contexts(
        contexts=contexts,
        target_day=target_day,
    )
    audit.extend(day_audit)

    lines, build_audit, payload_items = build_payload_items(
        day_matches=day_matches,
        contexts=contexts,
        points_map=points_map,
        strict_unknown_veto=strict_unknown_veto,
    )
    audit.extend(build_audit)

    stamp = target_day.isoformat()

    lines_path = OUT_DIR / f"lines_{stamp}.txt"
    audit_path = OUT_DIR / f"audit_{stamp}.txt"
    payload_path = OUT_DIR / f"payload_{stamp}.json"
    result_json_path = OUT_DIR / f"result_{stamp}.json"
    result_txt_path = OUT_DIR / f"result_{stamp}.txt"

    unity_text = "\n".join(lines)

    write_text(lines_path, unity_text)
    write_text(audit_path, "\n".join(audit))
    write_text(UNITY_OUT_PATH, unity_text)
    write_text(LINES_LATEST_PATH, unity_text)
    write_text(AUDIT_LATEST_PATH, "\n".join(audit))
    write_json(payload_path, [asdict(x) for x in payload_items])

    if not payload_items:
        print(f"UNITY_INPUT : {UNITY_OUT_PATH}")
        print(f"LINES       : {lines_path}")
        print(f"AUDIT       : {audit_path}")
        print(f"PAYLOAD     : {payload_path}")
        print(f"LINES_LATEST: {LINES_LATEST_PATH}")
        print(f"AUDIT_LATEST: {AUDIT_LATEST_PATH}")
        print("COUNT       : 0")
        print("Aucun match exploitable à envoyer au backend.")
        write_text(RESULT_LATEST_PATH, "Aucun match exploitable.")
        return 0

    result = send_to_backend(args.backend_url, payload_items)
    result_text = render_backend_result(result)

    write_json(result_json_path, result)
    write_text(result_txt_path, result_text)
    write_text(RESULT_LATEST_PATH, result_text)

    safe_result_text = result_text.replace("✅", "[JOUABLE]").replace("❌", "[PAS JOUABLE]")

    print(f"UNITY_INPUT : {UNITY_OUT_PATH}")
    print(f"LINES       : {lines_path}")
    print(f"AUDIT       : {audit_path}")
    print(f"PAYLOAD     : {payload_path}")
    print(f"RESULT_JSON : {result_json_path}")
    print(f"RESULT_TXT  : {result_txt_path}")
    print(f"LINES_LATEST: {LINES_LATEST_PATH}")
    print(f"AUDIT_LATEST: {AUDIT_LATEST_PATH}")
    print(f"RESULT_LATEST: {RESULT_LATEST_PATH}")
    print(f"COUNT       : {len(lines)}")
    print()
    print("--- RESULT PREVIEW ---")
    print(safe_result_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())