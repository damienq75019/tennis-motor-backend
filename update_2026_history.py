#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - premium_history.py

Rôle :
- enregistrer uniquement les vrais picks PREMIUM jouables dans /app/output/premium_history.json
- régler automatiquement les anciens "pending" quand le résultat est récupérable
- fournir /history avec résumé + courbe
- NE JAMAIS envoyer les matchs à venir dans l'Elo
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, unquote, urlparse, parse_qs

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


# -----------------------------
# CONFIG
# -----------------------------

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_PATH = Path(os.getenv("PREMIUM_HISTORY_PATH", OUTPUT_DIR / "premium_history.json"))
SUMMARY_PATH = Path(os.getenv("PREMIUM_HISTORY_SUMMARY_PATH", OUTPUT_DIR / "premium_history_summary.json"))
CACHE_PATH = Path(os.getenv("PREMIUM_RESULT_CACHE_PATH", OUTPUT_DIR / "premium_result_lookup_cache.json"))

PARIS_TZ_NAME = "Europe/Paris"
STAKE_EUR = float(os.getenv("PREMIUM_STAKE_EUR", "100"))

PREMIUM_THRESHOLD_PCT = float(os.getenv("PREMIUM_THRESHOLD_PCT", "80"))
SETTLE_DAYS_BACK_DEFAULT = int(os.getenv("PREMIUM_SETTLE_DAYS_BACK", "45"))

# Internet lookups can be disabled if Railway blocks them.
ENABLE_WEB_SETTLE = os.getenv("ENABLE_WEB_SETTLE", "1").strip().lower() not in {"0", "false", "no", "off"}

REQUEST_TIMEOUT = int(os.getenv("PREMIUM_HISTORY_HTTP_TIMEOUT", "20"))

USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)

# ATP archive pages: official source, easy to parse when updated.
# On garde une liste courte mais extensible.
ATP_ARCHIVE_URLS_BY_YEAR = {
    2026: [
        "https://www.atptour.com/en/scores/archive/rome/416/2026/results",
        "https://www.atptour.com/en/scores/archive/madrid/1536/2026/results",
        "https://www.atptour.com/en/scores/archive/monte-carlo/410/2026/results",
        "https://www.atptour.com/en/scores/archive/indian-wells/404/2026/results",
        "https://www.atptour.com/en/scores/archive/miami/403/2026/results",
        "https://www.atptour.com/en/scores/archive/barcelona/425/2026/results",
        "https://www.atptour.com/en/scores/archive/munich/308/2026/results",
        "https://www.atptour.com/en/scores/archive/bucharest/773/2026/results",
        "https://www.atptour.com/en/scores/archive/geneva/322/2026/results",
        "https://www.atptour.com/en/scores/archive/hamburg/414/2026/results",
    ],
    2025: [
        "https://www.atptour.com/en/scores/archive/rome/416/2025/results",
        "https://www.atptour.com/en/scores/archive/madrid/1536/2025/results",
        "https://www.atptour.com/en/scores/archive/monte-carlo/410/2025/results",
        "https://www.atptour.com/en/scores/archive/indian-wells/404/2025/results",
        "https://www.atptour.com/en/scores/archive/miami/403/2025/results",
    ],
}


# -----------------------------
# DATE / JSON
# -----------------------------

def _paris_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(PARIS_TZ_NAME))
    return datetime.now()


def paris_today_iso() -> str:
    return _paris_now().date().isoformat()


def parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def normalize_day(day: Optional[str]) -> str:
    today = _paris_now().date()
    if not day or str(day).strip().lower() in {"today", "aujourd'hui", "aujourdhui"}:
        return today.isoformat()
    if str(day).strip().lower() in {"tomorrow", "demain"}:
        return (today + timedelta(days=1)).isoformat()
    parsed = parse_date(day)
    if parsed:
        return parsed.isoformat()
    return today.isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_history() -> List[Dict[str, Any]]:
    data = load_json(HISTORY_PATH, [])
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return data["rows"]
    if isinstance(data, list):
        return data
    return []


def save_history(rows: List[Dict[str, Any]]) -> None:
    save_json_atomic(HISTORY_PATH, rows)


# -----------------------------
# NORMALISATION NOMS
# -----------------------------

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def normalize_name(s: Any) -> str:
    s = strip_accents(str(s or "")).lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\b(atp|wta|q|ll|wc|seed|sr|jr)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def name_tokens(s: Any) -> List[str]:
    return [t for t in normalize_name(s).split() if t]


def compact_name(s: Any) -> str:
    return "".join(name_tokens(s))


def last_name(s: Any) -> str:
    toks = name_tokens(s)
    if not toks:
        return ""
    return toks[-1]


def first_initial(s: Any) -> str:
    toks = name_tokens(s)
    if not toks:
        return ""
    return toks[0][:1]


def name_match(a: Any, b: Any) -> bool:
    """Tolérant : 'Daniil Medvedev' match 'Medvedev D.'."""
    na = normalize_name(a)
    nb = normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ca, cb = compact_name(na), compact_name(nb)
    if ca and cb and (ca in cb or cb in ca):
        return True

    la, lb = last_name(na), last_name(nb)
    if la and lb and la == lb:
        ia, ib = first_initial(na), first_initial(nb)
        if not ia or not ib or ia == ib:
            return True

    # cas "J. Sinner" vs "Jannik Sinner"
    ta, tb = name_tokens(na), name_tokens(nb)
    if len(ta) >= 2 and len(tb) >= 2:
        if ta[-1] == tb[-1] and ta[0][:1] == tb[0][:1]:
            return True
    return False


def pair_key(date_iso: str, player1: str, player2: str) -> str:
    names = sorted([normalize_name(player1), normalize_name(player2)])
    return f"{date_iso}__{names[0]}__{names[1]}"


def make_pick_id(date_iso: str, predicted: str, opponent: str) -> str:
    names = sorted([normalize_name(predicted), normalize_name(opponent)])
    return f"{date_iso}__{names[0]}__{names[1]}__pick_{normalize_name(predicted)}"


# -----------------------------
# RECORD PREMIUM
# -----------------------------

def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            x = x.replace(",", ".").replace("%", "").strip()
        return float(x)
    except Exception:
        return default


def _is_truthy_no_veto(v: Any) -> bool:
    s = str(v or "").strip().lower()
    return s in {"", "non", "no", "false", "0", "none", "null"}


def _match_decision_is_jouable(m: Dict[str, Any]) -> bool:
    decision = str(m.get("decision") or "").lower()
    veto = m.get("veto")
    premium = _as_float(m.get("premiumPct", _as_float(m.get("premium"), 0) * 100), 0)
    return premium >= PREMIUM_THRESHOLD_PCT and _is_truthy_no_veto(veto) and ("jouable" in decision or "✅" in decision or decision == "")


def _predicted_winner_from_match(m: Dict[str, Any]) -> Tuple[str, str]:
    """
    Le moteur renvoie déjà playerA comme côté choisi après double-side dans tes payloads.
    Si un champ predictedWinner existe, on le respecte.
    """
    predicted = m.get("predictedWinner") or m.get("winnerPredicted") or m.get("pick")
    if predicted:
        predicted = str(predicted)
        a = str(m.get("playerA") or m.get("sourcePlayerA") or "")
        b = str(m.get("playerB") or m.get("sourcePlayerB") or "")
        if name_match(predicted, a):
            opponent = b
        elif name_match(predicted, b):
            opponent = a
        else:
            opponent = str(m.get("opponent") or b or a)
        return predicted, opponent

    # par convention dans le payload final app.py : playerA = joueur choisi / affiché gagnant moteur
    a = str(m.get("playerA") or m.get("sourcePlayerA") or "")
    b = str(m.get("playerB") or m.get("sourcePlayerB") or "")
    return a, b


def _odd_for_player(m: Dict[str, Any], player_name: str, default: str = "") -> str:
    a = str(m.get("playerA") or "")
    b = str(m.get("playerB") or "")
    source_a = str(m.get("sourcePlayerA") or a)
    source_b = str(m.get("sourcePlayerB") or b)

    if name_match(player_name, a):
        return str(m.get("playerAOdd") or m.get("oddA") or m.get("coteA") or m.get("player_a_odd") or default)
    if name_match(player_name, b):
        return str(m.get("playerBOdd") or m.get("oddB") or m.get("coteB") or m.get("player_b_odd") or default)
    if name_match(player_name, source_a):
        return str(m.get("oddA") or m.get("coteA") or m.get("playerAOdd") or default)
    if name_match(player_name, source_b):
        return str(m.get("oddB") or m.get("coteB") or m.get("playerBOdd") or default)
    return default


def record_daily_analysis(analysis: Dict[str, Any], target_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Enregistre les picks PREMIUM jouables du payload /daily.
    N'enregistre PAS les veto, PAS les proches, PAS les non-premium.
    """
    date_iso = normalize_day(target_date or analysis.get("targetDay") or analysis.get("daily", {}).get("targetDay"))
    rows = load_history()
    existing = {r.get("id"): r for r in rows if r.get("id")}

    added = 0
    updated = 0
    ignored_non_premium = 0
    ignored_duplicates = 0

    matches = analysis.get("matches") if isinstance(analysis, dict) else None
    if not isinstance(matches, list):
        matches = []

    for m in matches:
        if not isinstance(m, dict):
            continue

        if not _match_decision_is_jouable(m):
            ignored_non_premium += 1
            continue

        predicted, opponent = _predicted_winner_from_match(m)
        if not predicted or not opponent:
            ignored_non_premium += 1
            continue

        rid = make_pick_id(date_iso, predicted, opponent)
        premium_pct = round(_as_float(m.get("premiumPct", _as_float(m.get("premium"), 0) * 100), 0), 1)

        odd_pred = _odd_for_player(m, predicted)
        odd_opp = _odd_for_player(m, opponent)

        record = {
            "id": rid,
            "date": date_iso,
            "sourcePlayerA": m.get("sourcePlayerA") or m.get("playerA") or predicted,
            "sourcePlayerB": m.get("sourcePlayerB") or m.get("playerB") or opponent,
            "predictedWinner": predicted,
            "opponent": opponent,
            "surface": m.get("surface", ""),
            "premiumPct": premium_pct,
            "status": "PREMIUM",
            "veto": m.get("veto", "non"),
            "decision": m.get("decision", "✅ Jouable"),
            "oddPredicted": str(odd_pred or ""),
            "oddOpponent": str(odd_opp or ""),
            "oddsSource": m.get("oddsSource", ""),
            "result": "pending",
            "realWinner": "",
            "settledAt": "",
            # champs utiles pour update_2026_history.py
            "playerAPoints": m.get("playerAPoints", ""),
            "playerBPoints": m.get("playerBPoints", ""),
            "playerARank": m.get("playerARank", ""),
            "playerBRank": m.get("playerBRank", ""),
            "playerA": m.get("playerA", ""),
            "playerB": m.get("playerB", ""),
            "score": "",
            "settleSource": "",
        }

        old = existing.get(rid)
        if old:
            # Ne pas écraser un résultat déjà réglé.
            if str(old.get("result", "pending")).lower() in {"win", "loss"}:
                ignored_duplicates += 1
                continue
            old.update({k: v for k, v in record.items() if k not in {"result", "realWinner", "settledAt", "score", "settleSource"}})
            updated += 1
        else:
            rows.insert(0, record)
            existing[rid] = record
            added += 1

    cleanup = cleanup_history(rows, save=False)
    save_history(cleanup["rows"])
    summary = build_summary(cleanup["rows"])
    save_json_atomic(SUMMARY_PATH, summary)

    return {
        "status": "ok",
        "date": date_iso,
        "added": added,
        "updated": updated,
        "ignoredNonPremium": ignored_non_premium,
        "ignoredDuplicates": ignored_duplicates,
        "cleanup": {k: v for k, v in cleanup.items() if k != "rows"},
        "historyPath": str(HISTORY_PATH),
        "summaryPath": str(SUMMARY_PATH),
    }


# -----------------------------
# SETTLE RESULTATS
# -----------------------------

@dataclass
class ResultInfo:
    winner: str
    loser: str
    score: str = ""
    source: str = ""


def _headers() -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
    }


def _http_get(url: str, params: Optional[Dict[str, str]] = None) -> str:
    if requests is None:
        return ""
    try:
        r = requests.get(url, params=params, headers=_headers(), timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400:
            return ""
        return r.text or ""
    except Exception:
        return ""


def _text_from_html(raw: str) -> str:
    if not raw:
        return ""
    if BeautifulSoup:
        try:
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            txt = soup.get_text("\n")
            txt = html.unescape(txt)
            return re.sub(r"[ \t\r\f\v]+", " ", txt)
        except Exception:
            pass
    txt = re.sub(r"<[^>]+>", "\n", raw)
    return html.unescape(re.sub(r"[ \t\r\f\v]+", " ", txt))


def _extract_links_from_duckduckgo(raw: str) -> List[str]:
    links: List[str] = []
    if not raw:
        return links
    if BeautifulSoup:
        soup = BeautifulSoup(raw, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                if "uddg" in qs:
                    links.append(unquote(qs["uddg"][0]))
            elif href.startswith("http"):
                links.append(href)
    else:
        for m in re.finditer(r'href="([^"]+)"', raw):
            href = html.unescape(m.group(1))
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                if "uddg" in qs:
                    links.append(unquote(qs["uddg"][0]))
            elif href.startswith("http"):
                links.append(href)
    clean = []
    seen = set()
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        clean.append(u)
    return clean


def _parse_flashscore_text_for_pair(text: str, p1: str, p2: str) -> Optional[ResultInfo]:
    """
    Flashscore pages/snippets contiennent souvent :
    Landaluce M. ATP: 94. 1-2. Medvedev D. ATP: 9.
    """
    if not text:
        return None
    low = normalize_name(text)
    if not (last_name(p1) in low and last_name(p2) in low):
        return None

    # Version simple sur texte brut avec ordre des noms + score sets "2-0", "1-2", etc.
    compact_text = re.sub(r"\s+", " ", text)
    # On cherche les formes courtes aussi.
    last1, last2 = last_name(p1), last_name(p2)
    if not last1 or not last2:
        return None

    # fenêtre contenant les deux noms et un score sets
    patterns = [
        (p1, p2, rf"({re.escape(last1)}[^\.]{{0,80}}?)([0-3])\s*[-:]\s*([0-3])([^\.]{{0,80}}?{re.escape(last2)})"),
        (p2, p1, rf"({re.escape(last2)}[^\.]{{0,80}}?)([0-3])\s*[-:]\s*([0-3])([^\.]{{0,80}}?{re.escape(last1)})"),
    ]

    norm_text = normalize_name(compact_text)
    for first, second, pat in patterns:
        m = re.search(pat, norm_text)
        if not m:
            continue
        try:
            s1 = int(m.group(2))
            s2 = int(m.group(3))
        except Exception:
            continue
        if s1 == s2:
            continue
        winner = first if s1 > s2 else second
        loser = second if s1 > s2 else first
        return ResultInfo(winner=winner, loser=loser, score=f"{s1}-{s2}", source="flashscore_text")

    # Si le texte contient "X wins the match" ou "bat"
    ri = _parse_generic_winner_sentence(text, p1, p2)
    if ri:
        ri.source = ri.source or "flashscore_text_sentence"
        return ri

    return None


def _parse_generic_winner_sentence(text: str, p1: str, p2: str) -> Optional[ResultInfo]:
    if not text:
        return None

    # anglais ATP : "Daniil Medvedev wins the match 1-6 6-4 7-5"
    for player in (p1, p2):
        ln = last_name(player)
        if not ln:
            continue
        if re.search(rf"{re.escape(ln)}[^.\n]{{0,120}}\bwins the match\b", text, flags=re.I):
            loser = p2 if name_match(player, p1) else p1
            score_match = re.search(r"wins the match\s+([0-9\-\(\) ]{3,40})", text, flags=re.I)
            return ResultInfo(winner=player, loser=loser, score=(score_match.group(1).strip() if score_match else ""), source="winner_sentence")

    # français : "Medvedev bat Landaluce 1-6, 6-4, 7-5"
    for player in (p1, p2):
        ln = last_name(player)
        if not ln:
            continue
        if re.search(rf"{re.escape(ln)}[^.\n]{{0,80}}\b(bat|def|d\.|defeats|defeated)\b", text, flags=re.I):
            loser = p2 if name_match(player, p1) else p1
            return ResultInfo(winner=player, loser=loser, source="winner_sentence_fr")
    return None


def resolve_from_duckduckgo_flashscore(record: Dict[str, Any]) -> Optional[ResultInfo]:
    if not ENABLE_WEB_SETTLE:
        return None
    p1 = str(record.get("predictedWinner") or "")
    p2 = str(record.get("opponent") or "")
    d = str(record.get("date") or "")
    if not p1 or not p2 or not d:
        return None

    queries = [
        f'site:flashscore.fr/match/tennis "{p1}" "{p2}" "{d}"',
        f'site:flashscore.fr/match/tennis "{last_name(p1)}" "{last_name(p2)}" tennis {d}',
        f'"{p1}" "{p2}" tennis score {d}',
    ]
    for q in queries:
        raw = _http_get("https://html.duckduckgo.com/html/", params={"q": q})
        if not raw:
            continue

        # Snippet search result can be enough.
        text = _text_from_html(raw)
        ri = _parse_flashscore_text_for_pair(text, p1, p2)
        if ri:
            ri.source = f"duckduckgo_flashscore_snippet:{q[:80]}"
            return ri

        links = _extract_links_from_duckduckgo(raw)
        for link in links[:8]:
            if "flashscore" not in link and "atptour" not in link and "eurosport" not in link and "reuters" not in link:
                continue
            page = _http_get(link)
            if not page:
                continue
            page_text = _text_from_html(page)
            ri = _parse_flashscore_text_for_pair(page_text, p1, p2) or _parse_generic_winner_sentence(page_text, p1, p2)
            if ri:
                ri.source = f"web:{link}"
                return ri

    return None


def _parse_atp_archive_page(text: str, p1: str, p2: str) -> Optional[ResultInfo]:
    if not text:
        return None
    if not (last_name(p1) and last_name(p2)):
        return None
    if last_name(p1) not in normalize_name(text) or last_name(p2) not in normalize_name(text):
        return None

    # ATP official page has repeated blocks:
    # Game Set and Match Jannik Sinner. Jannik Sinner wins the match 6-2 6-4 .
    # We use a window around both last names to avoid wrong match.
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    joined = "\n".join(raw_lines)

    # find windows where both names occur
    lines = raw_lines
    for i, ln in enumerate(lines):
        if name_match(ln, p1) or name_match(ln, p2) or last_name(p1) in normalize_name(ln) or last_name(p2) in normalize_name(ln):
            window = "\n".join(lines[max(0, i - 20): min(len(lines), i + 40)])
            nwin = normalize_name(window)
            if last_name(p1) in nwin and last_name(p2) in nwin:
                ri = _parse_generic_winner_sentence(window, p1, p2)
                if ri:
                    ri.source = "atp_archive"
                    return ri

    # global fallback
    ri = _parse_generic_winner_sentence(joined, p1, p2)
    if ri and last_name(p1) in normalize_name(joined) and last_name(p2) in normalize_name(joined):
        ri.source = "atp_archive_global"
        return ri
    return None


def resolve_from_atp_archives(record: Dict[str, Any]) -> Optional[ResultInfo]:
    if not ENABLE_WEB_SETTLE:
        return None
    d = parse_date(record.get("date"))
    if not d:
        return None
    p1 = str(record.get("predictedWinner") or "")
    p2 = str(record.get("opponent") or "")
    if not p1 or not p2:
        return None
    urls = ATP_ARCHIVE_URLS_BY_YEAR.get(d.year, [])
    for url in urls:
        raw = _http_get(url)
        if not raw:
            continue
        text = _text_from_html(raw)
        ri = _parse_atp_archive_page(text, p1, p2)
        if ri:
            ri.source = f"atp_archive:{url}"
            return ri
    return None


def resolve_result(record: Dict[str, Any]) -> Optional[ResultInfo]:
    """
    Ordre :
    1) ATP archive officiel si disponible
    2) Flashscore / web search direct
    """
    cache = load_json(CACHE_PATH, {})
    cache_key = record.get("id") or pair_key(str(record.get("date", "")), str(record.get("predictedWinner", "")), str(record.get("opponent", "")))
    if cache_key in cache:
        c = cache[cache_key]
        if c.get("winner"):
            return ResultInfo(winner=c.get("winner", ""), loser=c.get("loser", ""), score=c.get("score", ""), source=c.get("source", "cache"))

    resolvers = [resolve_from_atp_archives, resolve_from_duckduckgo_flashscore]
    for resolver in resolvers:
        try:
            ri = resolver(record)
            if ri and ri.winner:
                cache[cache_key] = {"winner": ri.winner, "loser": ri.loser, "score": ri.score, "source": ri.source, "cachedAt": _paris_now().isoformat()}
                save_json_atomic(CACHE_PATH, cache)
                return ri
        except Exception:
            continue
    return None


def settle_pending(days_back: int = SETTLE_DAYS_BACK_DEFAULT, force: bool = False) -> Dict[str, Any]:
    rows = load_history()
    today = _paris_now().date()
    min_date = today - timedelta(days=max(0, int(days_back)))
    settled = 0
    checked = 0
    pending_before = 0
    errors: List[str] = []
    settled_rows: List[Dict[str, Any]] = []

    for r in rows:
        if str(r.get("result", "pending")).lower() not in {"pending", "", "none", "null"}:
            continue
        pending_before += 1
        rd = parse_date(r.get("date"))
        if not rd:
            continue
        if rd < min_date:
            continue
        # Par sécurité, on évite d'insister sur les matchs du futur.
        if rd > today:
            continue

        checked += 1
        ri = resolve_result(r)
        if not ri:
            continue

        predicted = str(r.get("predictedWinner") or "")
        is_win = name_match(ri.winner, predicted)
        r["result"] = "win" if is_win else "loss"
        r["realWinner"] = ri.winner
        r["settledAt"] = today.isoformat()
        r["score"] = ri.score or r.get("score", "")
        r["settleSource"] = ri.source
        settled += 1
        settled_rows.append({"id": r.get("id"), "winner": ri.winner, "score": ri.score, "source": ri.source})

    cleanup = cleanup_history(rows, save=False)
    save_history(cleanup["rows"])
    summary = build_summary(cleanup["rows"])
    save_json_atomic(SUMMARY_PATH, summary)

    return {
        "status": "ok",
        "pendingBefore": pending_before,
        "checked": checked,
        "settled": settled,
        "settledRows": settled_rows,
        "errors": errors,
        "summaryPath": str(SUMMARY_PATH),
        "historyPath": str(HISTORY_PATH),
    }


# -----------------------------
# CLEANUP / SUMMARY
# -----------------------------

def cleanup_history(rows: Optional[List[Dict[str, Any]]] = None, save: bool = True) -> Dict[str, Any]:
    if rows is None:
        rows = load_history()

    today = _paris_now().date()
    seen = set()
    cleaned: List[Dict[str, Any]] = []
    removed_future = 0
    removed_duplicate = 0

    for r in rows:
        if not isinstance(r, dict):
            continue
        rd = parse_date(r.get("date"))
        if rd and rd > today + timedelta(days=1):
            removed_future += 1
            continue

        rid = r.get("id") or make_pick_id(str(r.get("date", "")), str(r.get("predictedWinner", "")), str(r.get("opponent", "")))
        r["id"] = rid

        if rid in seen:
            removed_duplicate += 1
            continue
        seen.add(rid)

        if not r.get("result"):
            r["result"] = "pending"
        cleaned.append(r)

    # plus récent en haut
    cleaned.sort(key=lambda x: (str(x.get("date", "")), str(x.get("id", ""))), reverse=True)

    if save:
        save_history(cleaned)
        save_json_atomic(SUMMARY_PATH, build_summary(cleaned))

    return {
        "removedFutureRows": removed_future,
        "removedDuplicateRows": removed_duplicate,
        "keptRows": len(cleaned),
        "restoredVerifiedRows": 0,
        "reason": "history_not_empty" if cleaned else "history_empty",
        "rows": cleaned,
    }


def _odd_value(x: Any) -> float:
    try:
        s = str(x or "").replace(",", ".").strip()
        if not s:
            return 0.0
        return float(s)
    except Exception:
        return 0.0


def _row_profit_units(r: Dict[str, Any]) -> Tuple[float, int]:
    res = str(r.get("result", "")).lower()
    odd = _odd_value(r.get("oddPredicted"))
    if res == "win":
        return (max(0.0, odd - 1.0) if odd > 1 else 0.0, 1 if odd > 1 else 0)
    if res == "loss":
        return (-1.0, 1 if odd > 1 else 0)
    return (0.0, 0)


def _summary_for_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    tracked = len(rows)
    settled_rows = [r for r in rows if str(r.get("result", "")).lower() in {"win", "loss"}]
    wins = sum(1 for r in settled_rows if str(r.get("result", "")).lower() == "win")
    losses = sum(1 for r in settled_rows if str(r.get("result", "")).lower() == "loss")
    pending = sum(1 for r in rows if str(r.get("result", "pending")).lower() not in {"win", "loss"})
    settled = wins + losses
    profit_units = 0.0
    odds_used = 0
    for r in settled_rows:
        p, used = _row_profit_units(r)
        profit_units += p
        odds_used += used
    return {
        "trackedPremium": tracked,
        "settled": settled,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "winRate": round((wins / settled) * 100, 1) if settled else 0.0,
        "profitUnits": round(profit_units, 2),
        "roiPct": round((profit_units / odds_used) * 100, 1) if odds_used else 0.0,
        "oddsUsed": odds_used,
    }


def build_chart(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    today = _paris_now().date()
    dates = [parse_date(r.get("date")) for r in rows if parse_date(r.get("date"))]
    if dates:
        start = min(min(dates), today - timedelta(days=5))
    else:
        start = today - timedelta(days=5)
    if (today - start).days < 5:
        start = today - timedelta(days=5)

    days = []
    cumulative = []
    cum_wins = cum_losses = cum_settled = 0
    cum_profit = 0.0

    d = start
    while d <= today:
        iso = d.isoformat()
        day_rows = [r for r in rows if str(r.get("date")) == iso]
        day_settled = [r for r in day_rows if str(r.get("result", "")).lower() in {"win", "loss"}]
        wins = sum(1 for r in day_settled if str(r.get("result", "")).lower() == "win")
        losses = sum(1 for r in day_settled if str(r.get("result", "")).lower() == "loss")
        pending = sum(1 for r in day_rows if str(r.get("result", "pending")).lower() not in {"win", "loss"})
        profit = sum(_row_profit_units(r)[0] for r in day_settled)
        settled = wins + losses
        day_win_rate = round((wins / settled) * 100, 1) if settled else 0.0

        days.append({
            "date": iso,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "winRate": day_win_rate,
            "profitUnits": round(profit, 2),
            "profitEur": round(profit * STAKE_EUR, 2),
            "hadPremiumToday": bool(day_rows),
            "hadPremiumSettledToday": bool(day_settled),
        })

        cum_wins += wins
        cum_losses += losses
        cum_settled += settled
        cum_profit += profit
        cumulative.append({
            "date": iso,
            "cumulativeWins": cum_wins,
            "cumulativeLosses": cum_losses,
            "cumulativeSettled": cum_settled,
            "cumulativeWinRate": round((cum_wins / cum_settled) * 100, 1) if cum_settled else 0.0,
            "cumulativeProfitUnits": round(cum_profit, 2),
            "cumulativeProfitEur": round(cum_profit * STAKE_EUR, 2),
            "pendingThatDay": pending,
            "hadPremiumToday": bool(day_rows),
            "hadPremiumSettledToday": bool(day_settled),
        })
        d += timedelta(days=1)

    return {
        "days": days,
        "cumulativeDays": cumulative,
        "description": "days = jour par jour ; cumulativeDays = courbe cumulée qui ne repart jamais à zéro.",
        "stakeEur": STAKE_EUR,
        "euroAxisMin": -2000.0,
        "euroAxisMax": 2000.0,
        "winRateAxisMin": 0.0,
        "winRateAxisMax": 100.0,
    }


def build_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    today = _paris_now().date()
    week_start = today - timedelta(days=6)
    month_start = today - timedelta(days=29)
    year_start = date(today.year, 1, 1)

    def in_range(r: Dict[str, Any], start: date, end: date) -> bool:
        rd = parse_date(r.get("date"))
        return bool(rd and start <= rd <= end)

    day_rows = [r for r in rows if str(r.get("date")) == today.isoformat()]
    week_rows = [r for r in rows if in_range(r, week_start, today)]
    month_rows = [r for r in rows if in_range(r, month_start, today)]
    year_rows = [r for r in rows if in_range(r, year_start, today)]

    return {
        "day": _summary_for_rows(day_rows),
        "week": _summary_for_rows(week_rows),
        "month": _summary_for_rows(month_rows),
        "year": _summary_for_rows(year_rows),
        "all": _summary_for_rows(rows),
    }


def get_history_payload(settle: bool = False) -> Dict[str, Any]:
    if settle:
        settle_info = settle_pending()
    else:
        settle_info = None

    rows = load_history()
    cleanup = cleanup_history(rows, save=True)
    rows = cleanup["rows"]
    summary = build_summary(rows)
    chart = build_chart(rows)
    save_json_atomic(SUMMARY_PATH, summary)

    storage = {
        "outputDir": str(OUTPUT_DIR),
        "historyDirEnv": os.getenv("HISTORY_DIR", ""),
        "railwayVolumeMountPath": os.getenv("RAILWAY_VOLUME_MOUNT_PATH", str(OUTPUT_DIR)),
        "persistentVolumeDetected": OUTPUT_DIR.exists(),
    }

    return {
        "status": "ok",
        "historyPath": str(HISTORY_PATH),
        "summaryPath": str(SUMMARY_PATH),
        "storage": storage,
        "cleanup": {k: v for k, v in cleanup.items() if k != "rows"},
        "settle": settle_info,
        "summary": summary,
        "chart": chart,
        "rows": rows,
    }


def history_refresh() -> Dict[str, Any]:
    settle_info = settle_pending(force=True)
    payload = get_history_payload(settle=False)
    payload["settle"] = settle_info
    return payload


def reset_history(confirm: str = "") -> Dict[str, Any]:
    if confirm != "RESET":
        return {"status": "refused", "message": "Ajoute ?confirm=RESET pour vider l'historique."}
    save_history([])
    save_json_atomic(SUMMARY_PATH, build_summary([]))
    return {"status": "ok", "message": "Historique premium vidé.", "historyPath": str(HISTORY_PATH)}


if __name__ == "__main__":
    info = history_refresh()
    print(json.dumps(info, ensure_ascii=False, indent=2))
