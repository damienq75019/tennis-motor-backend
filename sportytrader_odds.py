from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


DEFAULT_SPORTYTRADER_URLS = [
    "https://www.sportytrader.com/en/odds/tennis/atp-s/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/roland-garros-354/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/geneva-open-56537/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/hamburg-germany-350/",
]


@dataclass
class OddsRecord:
    player_a: str
    player_b: str
    odd_a: str
    odd_b: str
    source_url: str
    source_match: str
    source_time: str = ""


def _s(value: Any) -> str:
    return str(value or "").strip()


def _normalize(value: str) -> str:
    value = _s(value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = value.replace("jr.", "jr").replace("j.r.", "jr")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(?:wc|q|ll|pr|alt|seed|atp|wta|odds|vs)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _tokens(value: str) -> List[str]:
    return [x for x in _normalize(value).split() if len(x) >= 2]


def _same_player(a: str, b: str) -> bool:
    na = _normalize(a)
    nb = _normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    ta = _tokens(a)
    tb = _tokens(b)
    if not ta or not tb:
        return False
    if set(ta) == set(tb):
        return True

    # Format Sportradar fréquent : "Ruud, Casper" ; SportyTrader : "Casper Ruud".
    last_a = ta[-1]
    last_b = tb[-1]
    if last_a == last_b:
        return ta[0][0] == tb[0][0] or ta[0] in tb or tb[0] in ta

    # Cas avec noms composés : Juan Carlos Prado Angelo, De Minaur, Ugo Carabelli.
    if len(ta) >= 2 and len(tb) >= 2:
        if " ".join(ta[-2:]) == " ".join(tb[-2:]):
            return True

    # Dernier recours : gros recouvrement de tokens.
    inter = set(ta).intersection(tb)
    return len(inter) >= 2


def _same_match(p1: str, p2: str, q1: str, q2: str) -> Tuple[bool, bool]:
    """Retourne (matched, reversed)."""
    if _same_player(p1, q1) and _same_player(p2, q2):
        return True, False
    if _same_player(p1, q2) and _same_player(p2, q1):
        return True, True
    return False, False


def _clean_odd(value: Any) -> str:
    raw = _s(value).replace(",", ".")
    if not raw:
        return ""
    try:
        number = float(raw)
    except Exception:
        return ""
    if number <= 1.0 or number > 100.0:
        return ""
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text


def _valid_player_name(value: str) -> bool:
    value = _s(value)
    if len(value) < 3:
        return False
    low = value.lower()
    if "/" in value:
        return False  # doubles ignorés volontairement.
    banned = ["odds", "betting", "prediction", "bonus", "exclusive", "tennis", "competition"]
    return not any(b in low for b in banned)


def _lines_from_html(html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    return [x for x in lines if x]


def _parse_records_from_lines(lines: List[str], source_url: str) -> List[OddsRecord]:
    records: List[OddsRecord] = []
    seen = set()

    date_line_re = re.compile(
        r"^(?P<date>\d{1,2}\s+[A-Za-zÀ-ÿ]+)\s*-\s*(?P<time>\d{1,2}:\d{2})\s+(?P<body>.+)$"
    )
    match_re = re.compile(r"(?P<a>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .,'\-]+?)\s+-\s+(?P<b>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .,'\-]+?)(?:\s+Odds)?$", re.I)
    odds_re = re.compile(r"(?:^|\s)1\s+(?P<o1>\d+(?:[\.,]\d+)?)\s+2\s+(?P<o2>\d+(?:[\.,]\d+)?)(?:\s|$)")

    for i, line in enumerate(lines):
        m = date_line_re.match(line)
        if not m:
            continue

        body = m.group("body").strip()
        mm = match_re.search(body)
        if not mm:
            # Certains formats placent les joueurs à la ligne suivante.
            for j in range(i + 1, min(i + 4, len(lines))):
                mm = match_re.search(lines[j])
                if mm:
                    body = lines[j]
                    break
        if not mm:
            continue

        p1 = re.sub(r"\s+Odds$", "", mm.group("a").strip(), flags=re.I).strip()
        p2 = re.sub(r"\s+Odds$", "", mm.group("b").strip(), flags=re.I).strip()
        if not _valid_player_name(p1) or not _valid_player_name(p2):
            continue

        odd_match = odds_re.search(" ".join(lines[i:min(i + 12, len(lines))]))
        if not odd_match:
            continue

        odd1 = _clean_odd(odd_match.group("o1"))
        odd2 = _clean_odd(odd_match.group("o2"))
        if not odd1 or not odd2:
            continue

        key = (_normalize(p1), _normalize(p2), odd1, odd2)
        if key in seen:
            continue
        seen.add(key)

        records.append(OddsRecord(
            player_a=p1,
            player_b=p2,
            odd_a=odd1,
            odd_b=odd2,
            source_url=source_url,
            source_match=f"{p1} vs {p2}",
            source_time=f"{m.group('date')} {m.group('time')}",
        ))

    return records


class SportyTraderOddsProvider:
    """Récupère les cotes SportyTrader pour affichage Unity uniquement.

    Politique verrouillée :
    - les cotes ne sont jamais utilisées par le moteur ;
    - aucune décision Premium/Veto/Refusé ne dépend des cotes ;
    - si la source est indisponible, on laisse COTE - côté Unity.
    """

    def __init__(self, urls: Optional[List[str]] = None, timeout: Optional[int] = None) -> None:
        raw_urls = os.environ.get("SPORTYTRADER_ODDS_URLS", "").strip()
        if urls is not None:
            self.urls = urls
        elif raw_urls:
            self.urls = [x.strip() for x in raw_urls.split(",") if x.strip()]
        else:
            self.urls = list(DEFAULT_SPORTYTRADER_URLS)

        self.timeout = int(timeout or os.environ.get("SPORTYTRADER_TIMEOUT_SECONDS", "20"))
        self.enabled = os.environ.get("ENABLE_SPORTYTRADER_ODDS", "true").strip().lower() not in {"0", "false", "no", "off"}

    def fetch_records(self) -> Tuple[List[OddsRecord], Dict[str, Any]]:
        audit: Dict[str, Any] = {
            "provider": "sportytrader",
            "status": "disabled" if not self.enabled else "started",
            "policy": "odds_display_only_engine_ignored",
            "urls": self.urls,
            "records": 0,
            "errors": [],
            "warnings": [],
        }
        if not self.enabled:
            return [], audit

        all_records: List[OddsRecord] = []
        seen_pairs = set()
        headers = {
            "User-Agent": "Mozilla/5.0 TennisMotorOdds/2.6",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8,fr;q=0.7",
        }

        for url in self.urls:
            try:
                response = requests.get(url, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                records = _parse_records_from_lines(_lines_from_html(response.text), url)
                for record in records:
                    pair_key = tuple(sorted([_normalize(record.player_a), _normalize(record.player_b)]))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    all_records.append(record)
            except Exception as exc:
                audit["errors"].append({"url": url, "error": f"{type(exc).__name__}: {exc}"})

        audit["records"] = len(all_records)
        if all_records:
            audit["status"] = "ok" if not audit["errors"] else "partial"
            audit["sample"] = [r.__dict__ for r in all_records[:10]]
        else:
            audit["status"] = "empty" if not audit["errors"] else "error"
        return all_records, audit

    def fetch_odds_audit(self) -> Dict[str, Any]:
        _, audit = self.fetch_records()
        return audit

    def enrich_daily_response(self, daily_response: Dict[str, Any], *, target_day: str = "") -> Dict[str, Any]:
        records, audit = self.fetch_records()
        matches = daily_response.get("matches") if isinstance(daily_response, dict) else None
        if not isinstance(matches, list):
            audit["matched"] = 0
            return {"audit": audit}

        matched = 0
        unmatched: List[str] = []
        for match in matches:
            if not isinstance(match, dict):
                continue

            player_a = _s(match.get("playerA") or match.get("sourcePlayerA"))
            player_b = _s(match.get("playerB") or match.get("sourcePlayerB"))
            if not player_a or not player_b:
                continue

            chosen: Optional[OddsRecord] = None
            reversed_pair = False
            for record in records:
                ok, rev = _same_match(player_a, player_b, record.player_a, record.player_b)
                if ok:
                    chosen = record
                    reversed_pair = rev
                    break

            if not chosen:
                match.setdefault("playerAOdd", "")
                match.setdefault("playerBOdd", "")
                match.setdefault("coteA", "")
                match.setdefault("coteB", "")
                match.setdefault("oddsStatus", "not_found")
                if len(unmatched) < 20:
                    unmatched.append(f"{player_a} vs {player_b}")
                continue

            if reversed_pair:
                odd_a, odd_b = chosen.odd_b, chosen.odd_a
            else:
                odd_a, odd_b = chosen.odd_a, chosen.odd_b

            # Plusieurs alias pour compatibilité avec les scripts Unity existants.
            match["playerAOdd"] = odd_a
            match["playerBOdd"] = odd_b
            match["player_a_odd"] = odd_a
            match["player_b_odd"] = odd_b
            match["oddA"] = odd_a
            match["oddB"] = odd_b
            match["coteA"] = odd_a
            match["coteB"] = odd_b
            match["oddsA"] = odd_a
            match["oddsB"] = odd_b
            match["unibetOddA"] = odd_a
            match["unibetOddB"] = odd_b
            match["oddsSource"] = "SportyTrader"
            match["oddsStatus"] = "matched"
            match["oddsSourceMatch"] = chosen.source_match
            match["oddsSourceUrl"] = chosen.source_url
            matched += 1

        audit["targetDay"] = target_day
        audit["matched"] = matched
        audit["unmatchedCount"] = max(0, len(matches) - matched)
        audit["unmatchedSample"] = unmatched[:20]
        return {"audit": audit}
