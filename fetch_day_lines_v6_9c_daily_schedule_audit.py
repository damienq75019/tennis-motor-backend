#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - V6.9C Audit Daily Schedule Missing Matches

But :
- NE TOUCHE PAS au moteur.
- NE TOUCHE PAS à Unity.
- NE TOUCHE PAS à app.py.
- Compare les matchs du payload V6.9 avec les matchs visibles sur la page ATP daily-schedule.
- Trouve les matchs "invisibles" : présents sur ATP mais absents du payload envoyé au moteur.
- Explique si le problème vient probablement des points ATP introuvables ou de l'extraction V6.9.

Utilisation :
    py fetch_day_lines_v6_9c_daily_schedule_audit.py today
    py fetch_day_lines_v6_9c_daily_schedule_audit.py tomorrow
    py fetch_day_lines_v6_9c_daily_schedule_audit.py 2026-04-26

Sorties :
    output/audit_daily_schedule_missing_YYYY-MM-DD.txt
    output/audit_daily_schedule_missing_YYYY-MM-DD.json
    output/audit_daily_schedule_missing_latest.txt
    output/audit_daily_schedule_missing_latest.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from bs4 import BeautifulSoup


V69_MODULE_NAME = "fetch_day_lines_v6_9_strict_day_filter"
MODE = "V6_9C_DAILY_SCHEDULE_AUDIT"


def load_v69_module():
    try:
        return importlib.import_module(V69_MODULE_NAME)
    except BaseException as exc:
        raise RuntimeError(
            "Impossible d'importer fetch_day_lines_v6_9_strict_day_filter.py. "
            "Mets ce script dans le même dossier backend que fetch_day_lines_v6_9_strict_day_filter.py."
        ) from exc


v69 = load_v69_module()
base = v69.base


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class SchedulePair:
    playerA: str
    playerB: str
    source: str
    source_url: str
    evidence: str
    playerAPoints: int = 0
    playerBPoints: int = 0
    pointsAvailable: bool = False


def out_dir() -> Path:
    path = getattr(base, "OUT_DIR", Path("output"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_name(name: str) -> str:
    try:
        return base.canonical_name(name or "")
    except Exception:
        v = (name or "").lower()
        v = re.sub(r"[^a-z0-9]+", " ", v)
        return re.sub(r"\s+", " ", v).strip()


def clean_name(name: str) -> str:
    try:
        return base.clean_candidate_name(name or "")
    except Exception:
        return re.sub(r"\s+", " ", (name or "")).strip()


def is_name_like(name: str) -> bool:
    try:
        return base.is_name_like(name)
    except Exception:
        parts = name.split()
        return 2 <= len(parts) <= 5 and not any(ch.isdigit() for ch in name)


def normalize_space(text: str) -> str:
    try:
        return base.normalize_space(text or "")
    except Exception:
        return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def full_name_from_href(href: str, fallback: str = "") -> str:
    try:
        return clean_name(base.full_name_from_player_href(href, fallback))
    except Exception:
        m = re.search(r"/players/([^/]+)/", href or "")
        if m:
            slug = m.group(1).replace("-", " ")
            return " ".join(part.capitalize() for part in slug.split())
        return clean_name(fallback)


def unordered_pair_key(a: str, b: str) -> Tuple[str, str]:
    aa = normalize_name(a)
    bb = normalize_name(b)
    return tuple(sorted([aa, bb]))  # type: ignore[return-value]


def discover_daily_schedule_urls(session) -> List[str]:
    urls: List[str] = []

    html = base.fetch_html(session, base.ATP_CURRENT_URL)

    patterns = [
        r"https://www\.atptour\.com/en/scores/current/[^\"'\s<>]+/\d+/daily-schedule",
        r"/en/scores/current/[^\"'\s<>]+/\d+/daily-schedule",
        r"https://www\.atptour\.com/en/scores/current/[^\"'\s<>]+/\d+/(?:draws|results|live-scores)",
        r"/en/scores/current/[^\"'\s<>]+/\d+/(?:draws|results|live-scores)",
    ]

    for pat in patterns:
        for url in re.findall(pat, html, flags=re.I):
            if url.startswith("/"):
                url = "https://www.atptour.com" + url
            url = re.sub(r"/(?:draws|results|live-scores)(?:\?[^\"'\s<>]*)?$", "/daily-schedule", url)
            if url not in urls:
                urls.append(url)

    if not urls and "madrid" in html.lower():
        urls.append("https://www.atptour.com/en/scores/current/madrid/1536/daily-schedule")

    return urls


def extract_player_links_from_element(el) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    for a in el.find_all("a", href=True):
        href = a.get("href", "") or ""
        if "/players/" not in href:
            continue

        name = full_name_from_href(href, a.get_text(" ", strip=True))
        key = normalize_name(name)

        if not key or not is_name_like(name):
            continue

        if key in seen:
            continue

        seen.add(key)
        out.append((name, href))

    return out


def looks_like_match_block(text: str) -> bool:
    t = normalize_space(text).lower()

    if not t:
        return False

    banned = [
        "doubles",
        "privacy",
        "cookies",
        "tickets",
        "news",
        "highlights",
        "stats",
        "draw",
        "order of play below",
    ]

    if any(x in t for x in banned):
        return False

    signals = [
        " vs ",
        " v ",
        "not before",
        "court",
        "stadium",
        "defeats",
        "walkover",
        "retired",
        "scheduled",
        "completed",
        "live",
    ]

    return any(x in t for x in signals)


def extract_pairs_from_daily_schedule_html(html: str, source_url: str) -> List[SchedulePair]:
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    pairs: List[SchedulePair] = []
    seen_unordered: Set[Tuple[str, str]] = set()

    candidates = soup.find_all(["article", "li", "tr", "section", "div"])

    for el in candidates:
        links = extract_player_links_from_element(el)
        if len(links) != 2:
            continue

        text = normalize_space(el.get_text(" ", strip=True))
        if not looks_like_match_block(text):
            continue

        a, _ = links[0]
        b, _ = links[1]

        if normalize_name(a) == normalize_name(b):
            continue

        k = unordered_pair_key(a, b)
        if k in seen_unordered:
            continue

        seen_unordered.add(k)
        pairs.append(
            SchedulePair(
                playerA=a,
                playerB=b,
                source="daily_schedule_block",
                source_url=source_url,
                evidence=text[:260],
            )
        )

    text_lines = [
        normalize_space(x)
        for x in soup.get_text("\n", strip=True).splitlines()
        if normalize_space(x)
    ]

    all_player_links = extract_player_links_from_element(soup)
    alias_to_name: Dict[str, str] = {}

    for name, _ in all_player_links:
        norm = normalize_name(name)
        if not norm:
            continue

        parts = norm.split()
        aliases = {norm, norm.replace(" ", "")}
        if len(parts) >= 2:
            aliases.add(parts[-1])
            aliases.add(f"{parts[0]} {parts[-1]}")
            aliases.add(f"{parts[0][0]} {parts[-1]}")

        for alias in aliases:
            if len(alias) >= 3 and alias not in alias_to_name:
                alias_to_name[alias] = name

    def find_name(fragment: str) -> Optional[str]:
        nf = normalize_name(fragment)
        if nf in alias_to_name:
            return alias_to_name[nf]

        for alias in sorted(alias_to_name.keys(), key=len, reverse=True):
            if re.search(rf"\b{re.escape(alias)}\b", nf):
                return alias_to_name[alias]

        return None

    for line in text_lines:
        if "/" in line:
            continue
        if not re.search(r"\b(?:vs|v)\.?\b", line, flags=re.I):
            continue

        parts = re.split(r"\b(?:vs|v)\.?\b", line, maxsplit=1, flags=re.I)
        if len(parts) != 2:
            continue

        a = find_name(parts[0])
        b = find_name(parts[1])

        if not a or not b:
            continue

        if normalize_name(a) == normalize_name(b):
            continue

        k = unordered_pair_key(a, b)
        if k in seen_unordered:
            continue

        seen_unordered.add(k)
        pairs.append(
            SchedulePair(
                playerA=a,
                playerB=b,
                source="daily_schedule_vs_line",
                source_url=source_url,
                evidence=line[:260],
            )
        )

    return pairs


def read_payload_matches(target_day) -> List[Dict[str, Any]]:
    od = out_dir()
    payload_path = od / f"payload_{target_day.isoformat()}.json"

    if not payload_path.exists():
        latest = od / "payload_latest.json"
        if latest.exists():
            payload_path = latest
        else:
            return []

    try:
        data = json.loads(payload_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        pass

    return []


def point_status(player: str, points_map: Dict[str, int]) -> str:
    key = normalize_name(player)
    if key in points_map:
        return f"OK:{points_map[key]}"
    return "MISSING"


def attach_points_to_pairs(pairs: List[SchedulePair], points_map: Dict[str, int]) -> None:
    for pair in pairs:
        a_key = normalize_name(pair.playerA)
        b_key = normalize_name(pair.playerB)
        pair.playerAPoints = int(points_map.get(a_key, 0) or 0)
        pair.playerBPoints = int(points_map.get(b_key, 0) or 0)
        pair.pointsAvailable = pair.playerAPoints > 0 and pair.playerBPoints > 0


def build_report(target_raw: str, show_browser: bool = False):
    if hasattr(base, "PLAYWRIGHT_HEADLESS"):
        base.PLAYWRIGHT_HEADLESS = not show_browser

    target_day = base.parse_target_day(target_raw)
    session = base.build_session()

    report: List[str] = []
    report.append(f"mode={MODE}")
    report.append(f"target_day={target_day.isoformat()}")
    report.append(f"target_label={target_raw}")

    points_map, display_map = base.fetch_live_points_map(session)
    report.append(f"points_map_size={len(points_map)}")

    daily_urls = discover_daily_schedule_urls(session)
    report.append(f"daily_schedule_urls={len(daily_urls)}")

    all_schedule_pairs: List[SchedulePair] = []

    for url in daily_urls:
        try:
            html = base.fetch_html(session, url)
            pairs = extract_pairs_from_daily_schedule_html(html, url)
            all_schedule_pairs.extend(pairs)
            report.append(f"[DAILY URL] pairs={len(pairs)} | {url}")
        except Exception as exc:
            report.append(f"[DAILY URL FAIL] {url} | {exc}")

    seen: Set[Tuple[str, str]] = set()
    unique_schedule_pairs: List[SchedulePair] = []

    for p in all_schedule_pairs:
        k = unordered_pair_key(p.playerA, p.playerB)
        if k in seen:
            continue
        seen.add(k)
        unique_schedule_pairs.append(p)

    attach_points_to_pairs(unique_schedule_pairs, points_map)

    payload_matches = read_payload_matches(target_day)

    payload_keys = {
        unordered_pair_key(str(m.get("playerA", "")), str(m.get("playerB", "")))
        for m in payload_matches
    }

    schedule_keys = {
        unordered_pair_key(p.playerA, p.playerB)
        for p in unique_schedule_pairs
    }

    missing = [
        p for p in unique_schedule_pairs
        if unordered_pair_key(p.playerA, p.playerB) not in payload_keys
    ]

    extra_payload = [
        m for m in payload_matches
        if unordered_pair_key(str(m.get("playerA", "")), str(m.get("playerB", ""))) not in schedule_keys
    ]

    report.append("")
    report.append("=== SYNTHÈSE ===")
    report.append(f"daily_schedule_pairs={len(unique_schedule_pairs)}")
    report.append(f"payload_v6_9_pairs={len(payload_matches)}")
    report.append(f"missing_from_payload={len(missing)}")
    report.append(f"extra_payload_not_in_daily_schedule={len(extra_payload)}")
    report.append("")

    report.append("=== MATCHS DAILY SCHEDULE ATP ===")
    if unique_schedule_pairs:
        for i, p in enumerate(unique_schedule_pairs, start=1):
            report.append(
                f"{i}. {p.playerA} vs {p.playerB} | "
                f"A_points={'OK:' + str(p.playerAPoints) if p.playerAPoints > 0 else 'MISSING'} | "
                f"B_points={'OK:' + str(p.playerBPoints) if p.playerBPoints > 0 else 'MISSING'} | "
                f"source={p.source}"
            )
    else:
        report.append("Aucun match détecté sur daily-schedule.")

    report.append("")
    report.append("=== MATCHS PAYLOAD V6.9 ===")
    if payload_matches:
        for i, m in enumerate(payload_matches, start=1):
            report.append(
                f"{i}. {m.get('playerA', '')} vs {m.get('playerB', '')} | "
                f"points={m.get('playerAPoints', '')}-{m.get('playerBPoints', '')}"
            )
    else:
        report.append("Aucun payload V6.9 trouvé. Lance d'abord /daily?day=today ou le script V6.9.")

    report.append("")
    report.append("=== MATCHS INVISIBLES / MANQUANTS ===")
    if missing:
        for i, p in enumerate(missing, start=1):
            a_status = f"OK:{p.playerAPoints}" if p.playerAPoints > 0 else "MISSING"
            b_status = f"OK:{p.playerBPoints}" if p.playerBPoints > 0 else "MISSING"

            if not p.pointsAvailable:
                reason = "données manquantes : points ATP / player_key introuvable"
            else:
                reason = "présent sur daily-schedule mais non retenu par l'extraction V6.9"

            report.append(f"{i}. {p.playerA} vs {p.playerB}")
            report.append(f"   Raison probable : {reason}")
            report.append(f"   playerA_points_status : {a_status}")
            report.append(f"   playerB_points_status : {b_status}")
            report.append(f"   Source : {p.source_url}")
            report.append(f"   Evidence : {p.evidence}")
    else:
        report.append("Aucun match invisible détecté.")

    result = {
        "mode": MODE,
        "targetDay": target_day.isoformat(),
        "targetLabel": target_raw,
        "pointsMapSize": len(points_map),
        "dailyScheduleUrls": daily_urls,
        "dailySchedulePairs": [asdict(p) for p in unique_schedule_pairs],
        "payloadV69Pairs": payload_matches,
        "missingFromPayload": [asdict(p) for p in missing],
        "extraPayloadNotInDailySchedule": extra_payload,
        "summary": {
            "dailySchedulePairs": len(unique_schedule_pairs),
            "payloadV69Pairs": len(payload_matches),
            "missingFromPayload": len(missing),
            "extraPayloadNotInDailySchedule": len(extra_payload),
        },
    }

    return result, "\n".join(report)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_day", help="today | tomorrow | YYYY-MM-DD")
    parser.add_argument("--show-browser", action="store_true")
    args = parser.parse_args()

    od = out_dir()

    result, text = build_report(args.target_day, show_browser=args.show_browser)
    stamp = result["targetDay"]

    txt_path = od / f"audit_daily_schedule_missing_{stamp}.txt"
    json_path = od / f"audit_daily_schedule_missing_{stamp}.json"
    latest_txt = od / "audit_daily_schedule_missing_latest.txt"
    latest_json = od / "audit_daily_schedule_missing_latest.json"

    txt_path.write_text(text, encoding="utf-8")
    latest_txt.write_text(text, encoding="utf-8")

    json_text = json.dumps(result, ensure_ascii=False, indent=2)
    json_path.write_text(json_text, encoding="utf-8")
    latest_json.write_text(json_text, encoding="utf-8")

    print(text)
    print("")
    print(f"AUDIT_TXT   : {txt_path}")
    print(f"AUDIT_JSON  : {json_path}")
    print(f"LATEST_TXT  : {latest_txt}")
    print(f"LATEST_JSON : {latest_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
