#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - Fetch daily lines V6.9 STRICT DAY FILTER

But :
- garder le moteur / veto existant de la V6.7 ;
- empêcher today et tomorrow de reprendre la même page ATP globale ;
- utiliser les paires d'article uniquement si elles sont dans une section de date
  correspondant réellement à target_day ;
- si aucune section datée fiable n'est trouvée, ne pas inventer de matchs.

Ce fichier est volontairement un wrapper complet : il réutilise le script V6.7
existant pour toutes les fonctions lourdes déjà validées, puis remplace seulement
la sélection des matchs du jour.
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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup


BASE_MODULE_CANDIDATES = [
    "fetch_day_lines_v6_7_results_context_fixed_safe_clamped",
    "fetch_day_lines_v6_6_results_context_fixed_safe",
    "fetch_day_lines_v6_5_results_context_safe",
]

MODE = "V6_9_STRICT_DAY_FILTER"
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

    # Signatures fortes : ISO ou mois + jour, ou weekday + mois/jour.
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

    # Certains articles ATP titrent "Saturday Schedule" sans date numérique.
    # On accepte seulement si la ligne ressemble à un titre de section.
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
        # Ligne avec weekday + mois/jour = très probablement nouvelle section.
        if any(m in ln for m in _month_names()):
            return True

    # Mois + jour différent, ex : "Sunday, April 27".
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
            aliases.add(parts[-1])  # nom de famille
            aliases.add(f"{parts[0]} {parts[-1]}")
            aliases.add(f"{parts[0][0]} {parts[-1]}")

        for alias in aliases:
            if len(alias) >= 3:
                tmp.setdefault(alias, set()).add(display_clean)

    out: Dict[str, List[str]] = {}
    for alias, names in tmp.items():
        # garder seulement les alias non ambigus
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

    # Plus long d'abord pour éviter de matcher un nom court avant un nom complet.
    for alias in sorted(alias_index.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", norm):
            return alias_index[alias][0]

    return None


def extract_pair_from_vs_line(
    line: str,
    alias_index: Dict[str, List[str]],
    valid_player_keys: Set[str],
) -> Optional[Tuple[str, str]]:
    # Supprimer les morceaux de contexte qui parasitent souvent une ligne ATP.
    cleaned = base.normalize_space(line)
    cleaned = re.sub(r"\b(Court|Stadium|Manolo Santana|Arantxa Sanchez|Not Before|NB|Starts at).*$", "", cleaned, flags=re.I)

    if not re.search(r"\b(vs|v)\.?\b", cleaned, flags=re.I):
        return None

    parts = re.split(r"\b(?:vs|v)\.?\b", cleaned, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None

    left, right = parts[0], parts[1]

    # Doubles : on ignore volontairement si / est présent autour du vs.
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
    alias_index = build_name_alias_index(display_map, valid_player_keys)
    pairs: List[Tuple[str, str]] = []

    for ln in lines:
        pair = extract_pair_from_vs_line(ln, alias_index, valid_player_keys)
        if pair:
            pairs.append(pair)

    if pairs:
        return base.pair_consecutive_names([x for pair in pairs for x in pair])

    # Fallback dans la section datée uniquement : ordre des noms ATP.
    section_html = "\n".join(lines)
    ordered_names = base.extract_ordered_valid_names(section_html, display_map, valid_player_keys)
    return base.pair_consecutive_names(ordered_names)


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
        # éviter des sections immenses si l'article ne découpe pas proprement
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

    # dédoublonnage stable
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
    """
    On laisse la V6.7 construire le contexte complet, puis on remplace seulement
    ctx.article_pairs par des paires filtrées par target_day.
    """
    ctx = base.parse_tournament_context(
        session=session,
        draw_url=draw_url,
        display_map=display_map,
        valid_player_keys=valid_player_keys,
        target_day=target_day,
    )

    audit: List[str] = []
    strict_pairs: List[Tuple[str, str]] = []
    article_urls = base.discover_schedule_article_urls(session, ctx.slug, target_day.year)

    audit.append(f"[STRICT ARTICLE] {ctx.tournament_name} | urls={len(article_urls)}")

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

    # Remplacement essentiel : on ne garde pas les article_pairs globaux de la V6.7.
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
