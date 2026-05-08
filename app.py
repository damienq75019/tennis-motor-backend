from __future__ import annotations

import asyncio
import json
import re
import os
import subprocess
import sys
import traceback
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from motor import calculate_predictions, get_state


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PAYLOAD_LATEST_PATH = OUTPUT_DIR / "payload_latest.json"
DAILY_SCRIPT_NAME = "fetch_day_lines_v6_10k_daily_schedule_no_forced_veto.py"

# Pages tournoi SportyTrader.
# Important : on évite la page générale /atp-s/ car elle renvoie souvent 403 sur Railway.
# Ces pages tournoi sont plus stables et contiennent directement les matchs + cotes.
SPORTYTRADER_ATP_ODDS_URLS = [
    # Rome / Internazionali BNL d'Italia
    "https://www.sportytrader.com/en/odds/tennis/atp-s/rome-italy-347/",

    # Masters 1000 / ATP principaux, pour les semaines suivantes
    "https://www.sportytrader.com/en/odds/tennis/atp-s/madrid-spain-383/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/monte-carlo-monaco-260/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/roland-garros-354/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/wimbledon-london-great-britain-356/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/toronto-canada-360/",

    # ATP 250 / 500 fréquents
    "https://www.sportytrader.com/en/odds/tennis/atp-s/barcelona-spain-320/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/munich-germany-321/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/montpellier-france-523/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/geneva-open-56537/",
    "https://www.sportytrader.com/en/odds/tennis/atp-s/bastad-sweden-470/",
]

app = FastAPI(title="Tennis Motor Railway Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _paris_today() -> date:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:
        return date.today()


def normalize_day(day: str) -> str:
    value = (day or "today").strip().lower()
    today = _paris_today()

    if value == "today":
        return today.isoformat()

    if value == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    return date.fromisoformat(value).isoformat()


def _extract_matches_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in ("matches", "payload", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

    return []


async def _read_request_matches(request: Request) -> List[Dict[str, Any]]:
    try:
        payload = await request.json()
    except Exception:
        payload = []

    return _extract_matches_from_payload(payload)


def _empty_response(
    status: str,
    message: str = "",
    target_day: str = "",
    stdout_tail: str = "",
    stderr_tail: str = "",
    command: str = "",
) -> Dict[str, Any]:
    try:
        state = get_state()
        history_rows_loaded = int(state.get("history_rows_loaded", 0))
    except Exception:
        history_rows_loaded = 0

    return {
        "matches": [],
        "summary": {
            "totalRows": 0,
            "validRows": 0,
            "errorRows": 0,
            "over80": 0,
            "vetoCount": 0,
            "jouables": 0,
        },
        "engine": {
            "name": "Tennis Motor V7",
            "version": "Bayesian Shrinkage",
            "historyYears": [2022, 2023, 2024, 2025],
            "historyRowsLoaded": history_rows_loaded,
            "premiumFormula": "Bayesian shrinkage blend of SWE, ATP, Rank, Form5, Form10, SurfaceForm5, Dominance",
            "threshold": "> 0.80",
            "status": status,
        },
        "daily": {
            "targetDay": target_day,
            "payloadCount": 0,
            "stdoutTail": stdout_tail[-4000:] if stdout_tail else "",
            "stderrTail": stderr_tail[-4000:] if stderr_tail else "",
            "command": command,
        },
        "error": message,
    }



def _norm_name(value: str) -> str:
    value = value or ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(?:wc|q|ll|pr|alt|seed)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _name_tokens(value: str) -> List[str]:
    return [x for x in _norm_name(value).split() if len(x) >= 2]


def _same_player(a: str, b: str) -> bool:
    na = _norm_name(a)
    nb = _norm_name(b)

    if not na or not nb:
        return False

    if na == nb:
        return True

    ta = _name_tokens(a)
    tb = _name_tokens(b)

    if not ta or not tb:
        return False

    if set(ta) == set(tb):
        return True

    last_a = ta[-1]
    last_b = tb[-1]

    if last_a == last_b:
        first_a = ta[0][0]
        first_b = tb[0][0]
        return first_a == first_b or ta[0] in tb or tb[0] in ta

    return False


def _target_sporty_date_tokens(target_day: str) -> List[str]:
    try:
        d = date.fromisoformat(target_day)
    except Exception:
        return []

    month_short = d.strftime("%b")
    month_long = d.strftime("%B")
    day2 = f"{d.day:02d}"
    day1 = str(d.day)

    return [
        f"{day2} {month_short}",
        f"{day1} {month_short}",
        f"{day2} {month_long}",
        f"{day1} {month_long}",
    ]


def _extract_decimal(value: str) -> str:
    value = (value or "").strip().replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", value)
    if not m:
        return ""
    return m.group(0)


def _fetch_sportytrader_text(url: str) -> str:
    """
    Lit une page tournoi SportyTrader.

    Stratégie propre :
    - d'abord requests sur la page tournoi précise, souvent accessible ;
    - si 403 ou contenu trop faible, fallback Playwright/Chromium ;
    - scroll pour charger le lazy-load.
    """
    requests_error = ""

    try:
        import requests
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://www.sportytrader.com/en/odds/tennis/",
        }

        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        content = soup.get_text("\n", strip=True)
        content = re.sub(r"\r", "\n", content)
        content = re.sub(r"\n{2,}", "\n", content)

        # Une page tournoi lisible contient normalement "Upcoming" ou des lignes "1 1.45 2 2.83".
        if content and len(content) > 500:
            return content

        requests_error = "contenu requests trop faible"

    except Exception as exc:
        requests_error = f"{type(exc).__name__}: {exc}"

    try:
        from playwright.sync_api import sync_playwright

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
                locale="en-US",
                timezone_id="Europe/Paris",
                viewport={"width": 1365, "height": 1800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
                    "Referer": "https://www.sportytrader.com/en/odds/tennis/",
                },
            )

            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)

            for selector in [
                "text=OK",
                "text=Accept",
                "text=Accept all",
                "text=I agree",
                "button:has-text('OK')",
                "button:has-text('Accept')",
            ]:
                try:
                    page.locator(selector).first.click(timeout=1500)
                    break
                except Exception:
                    pass

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            for _ in range(10):
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(600)

            content = page.locator("body").inner_text(timeout=15000)
            browser.close()

            content = re.sub(r"\r", "\n", content or "")
            content = re.sub(r"\n{2,}", "\n", content)

            if content and len(content) > 100:
                return content

            raise RuntimeError("Playwright body vide")

    except Exception as exc:
        raise RuntimeError(
            f"requests_failed=[{requests_error}] | playwright_failed=[{type(exc).__name__}: {exc}]"
        )


def _looks_like_odd(value: str) -> bool:
    raw = (value or "").strip().replace(",", ".")
    if not re.fullmatch(r"\d{1,2}(?:\.\d{1,2})?", raw):
        return False
    try:
        number = float(raw)
    except Exception:
        return False
    return 1.01 <= number <= 25.0


def _parse_match_line(line: str) -> Tuple[str, str]:
    clean = re.sub(r"\s+", " ", line or "").strip()
    clean = re.sub(
        r"^\d{1,2}\s+[A-Za-zÀ-ÖØ-öø-ÿ]{3,9}\s*-\s*\d{1,2}:\d{2}\s+",
        "",
        clean,
        flags=re.IGNORECASE,
    )

    if " - " in clean:
        left, right = clean.split(" - ", 1)
        left = re.sub(r"^\d+\s+", "", left).strip()
        right = re.sub(r"\s+\d+$", "", right).strip()
        if left and right and "/" not in left and "/" not in right:
            return left, right

    return "", ""


def _parse_sportytrader_odds_from_text(content: str, target_day: str) -> List[Dict[str, str]]:
    date_tokens = _target_sporty_date_tokens(target_day)
    if not content:
        return []

    rows: List[Dict[str, str]] = []
    seen = set()

    lines = [re.sub(r"\s+", " ", x).strip() for x in content.splitlines() if re.sub(r"\s+", " ", x).strip()]

    for i, line in enumerate(lines):
        player_a, player_b = _parse_match_line(line)
        if not player_a or not player_b:
            continue

        nearby = lines[i + 1: i + 20]
        odds = []

        for item in nearby:
            value = item.strip().replace(",", ".")
            if _looks_like_odd(value) and value not in {"1", "2"}:
                odds.append(_extract_decimal(value))
            if len(odds) >= 2:
                break

        if len(odds) >= 2:
            key = (_norm_name(player_a), _norm_name(player_b))
            if key not in seen:
                seen.add(key)
                rows.append({"playerA": player_a, "playerB": player_b, "oddA": odds[0], "oddB": odds[1]})

    if rows:
        return rows

    one_line = re.sub(r"\s+", " ", content)
    pattern = re.compile(
        r"(?:\d{1,2}\s+[A-Za-zÀ-ÖØ-öø-ÿ]{3,9}\s*-\s*\d{1,2}:\d{2}\s+)?"
        r"(?P<a>[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ' .\-]{2,60}?)"
        r"\s+-\s+"
        r"(?P<b>[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ' .\-]{2,60}?)"
        r"(?P<trail>.{0,280})",
        flags=re.IGNORECASE,
    )

    for m in pattern.finditer(one_line):
        player_a = re.sub(r"\s+", " ", m.group("a")).strip()
        player_b = re.sub(r"\s+", " ", m.group("b")).strip()
        trail = m.group("trail")

        if "/" in player_a or "/" in player_b:
            continue

        nums = re.findall(r"\b\d{1,2}(?:[.,]\d{1,2})\b", trail)
        odds = []
        for num in nums:
            if _looks_like_odd(num):
                odds.append(_extract_decimal(num))
            if len(odds) >= 2:
                break

        if len(odds) >= 2:
            key = (_norm_name(player_a), _norm_name(player_b))
            if key not in seen:
                seen.add(key)
                rows.append({"playerA": player_a, "playerB": player_b, "oddA": odds[0], "oddB": odds[1]})

    return rows


def fetch_sportytrader_atp_odds(target_day: str) -> Tuple[List[Dict[str, str]], str]:
    audit: List[str] = []
    all_rows: List[Dict[str, str]] = []
    seen = set()

    for url in SPORTYTRADER_ATP_ODDS_URLS:
        try:
            content = _fetch_sportytrader_text(url)
            rows = _parse_sportytrader_odds_from_text(content, target_day)
            audit.append(f"{url} rows={len(rows)} content_len={len(content)}")

            if rows:
                sample_rows = []
                for row in rows[:6]:
                    sample_rows.append(f"{row.get('playerA')} - {row.get('playerB')} = {row.get('oddA')}/{row.get('oddB')}")
                audit.append("parsed_sample=" + " || ".join(sample_rows))

            if not rows:
                sample_lines = []
                for line in content.splitlines():
                    compact = re.sub(r"\s+", " ", line).strip()
                    if not compact:
                        continue
                    if len(compact) > 120:
                        compact = compact[:120]
                    sample_lines.append(compact)
                    if len(sample_lines) >= 20:
                        break
                audit.append("sample=" + " || ".join(sample_lines))

            for row in rows:
                key = (_norm_name(row["playerA"]), _norm_name(row["playerB"]))
                if key in seen:
                    continue
                seen.add(key)
                all_rows.append(row)

            # Optimisation : la première page tournoi active qui trouve des cotes suffit souvent.
            # Mais on continue quand même pour couvrir multi-tournois.
        except Exception as exc:
            audit.append(f"{url} error={type(exc).__name__}: {exc}")

    return all_rows, " | ".join(audit)


def _find_odds_for_match(player_a: str, player_b: str, odds_rows: List[Dict[str, str]]) -> Dict[str, str]:
    for row in odds_rows:
        st_a = row.get("playerA", "")
        st_b = row.get("playerB", "")

        if _same_player(player_a, st_a) and _same_player(player_b, st_b):
            return {
                "oddA": row.get("oddA", ""),
                "oddB": row.get("oddB", ""),
                "sourcePlayerA": st_a,
                "sourcePlayerB": st_b,
                "orientation": "same",
            }

        if _same_player(player_a, st_b) and _same_player(player_b, st_a):
            return {
                "oddA": row.get("oddB", ""),
                "oddB": row.get("oddA", ""),
                "sourcePlayerA": st_a,
                "sourcePlayerB": st_b,
                "orientation": "reversed",
            }

    return {}


def enrich_result_with_sportytrader_odds(result: Dict[str, Any], target_day: str) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result

    matches = result.get("matches")
    if not isinstance(matches, list) or not matches:
        return result

    try:
        odds_rows, odds_audit = fetch_sportytrader_atp_odds(target_day)
    except Exception as exc:
        odds_rows = []
        odds_audit = f"global_error={type(exc).__name__}: {exc}"

    matched_count = 0

    for match in matches:
        if not isinstance(match, dict):
            continue

        player_a = str(match.get("playerA") or match.get("player_a") or "")
        player_b = str(match.get("playerB") or match.get("player_b") or "")

        found = _find_odds_for_match(player_a, player_b, odds_rows)

        if found:
            odd_a = found.get("oddA", "")
            odd_b = found.get("oddB", "")

            match["oddA"] = odd_a
            match["oddB"] = odd_b
            match["playerAOdd"] = odd_a
            match["playerBOdd"] = odd_b
            match["player_a_odd"] = odd_a
            match["player_b_odd"] = odd_b
            match["coteA"] = odd_a
            match["coteB"] = odd_b
            match["oddsSource"] = "SportyTrader"
            match["oddsStatus"] = "matched"
            match["oddsSourceMatch"] = f'{found.get("sourcePlayerA", "")} - {found.get("sourcePlayerB", "")}'
            matched_count += 1
        else:
            match.setdefault("oddA", "")
            match.setdefault("oddB", "")
            match.setdefault("playerAOdd", "")
            match.setdefault("playerBOdd", "")
            match.setdefault("player_a_odd", "")
            match.setdefault("player_b_odd", "")
            match.setdefault("coteA", "")
            match.setdefault("coteB", "")
            match["oddsSource"] = "SportyTrader"
            match["oddsStatus"] = "not_found"

    result.setdefault("daily", {})
    result["daily"]["oddsSource"] = "SportyTrader"
    result["daily"]["oddsRowsFound"] = len(odds_rows)
    result["daily"]["oddsMatched"] = matched_count
    result["daily"]["oddsAudit"] = odds_audit[-1200:]

    return result


def calculate_from_matches(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not matches:
        return _empty_response(
            status="empty_payload",
            message="Aucun match exploitable dans le payload daily.",
        )

    return calculate_predictions(matches)


def _read_payload_for_day(target_day: str) -> Tuple[List[Dict[str, Any]], str]:
    """
    Sécurité anti-cache :
    on lit uniquement le payload daté du jour demandé.
    On ne retombe jamais sur payload_latest.json, car il peut contenir
    les matchs d'une ancienne journée.
    """
    payload_path = OUTPUT_DIR / f"payload_{target_day}.json"

    if not payload_path.exists():
        return [], ""

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    return _extract_matches_from_payload(payload), payload_path.name


def _read_audit_for_day(target_day: str) -> str:
    audit_path = OUTPUT_DIR / f"audit_{target_day}.txt"

    if not audit_path.exists():
        audit_path = OUTPUT_DIR / "audit_latest.txt"

    if not audit_path.exists():
        return ""

    try:
        return audit_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def run_daily_fetch_sync(target_day: str) -> Dict[str, Any]:
    """
    Lance l'extraction daily.

    Cette fonction ne doit jamais faire tomber FastAPI en 500.
    Si l'extraction ATP plante, on renvoie un JSON propre avec l'erreur.
    """

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    script = BASE_DIR / DAILY_SCRIPT_NAME
    if not script.exists():
        return _empty_response(
            status="script_missing",
            message=f"{DAILY_SCRIPT_NAME} introuvable sur Railway.",
            target_day=target_day,
        )

    cmd = [
        sys.executable,
        str(script),
        target_day,
        "--no-send-backend",
    ]

    command_text = " ".join(cmd)
    timeout_seconds = int(os.environ.get("FETCH_TIMEOUT_SECONDS", "540"))

    try:
        completed = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return _empty_response(
            status="daily_fetch_timeout",
            message=f"Extraction daily trop longue : timeout après {timeout_seconds} secondes.",
            target_day=target_day,
            stdout_tail=exc.stdout or "",
            stderr_tail=exc.stderr or "",
            command=command_text,
        )
    except Exception as exc:
        return _empty_response(
            status="daily_fetch_exception",
            message=f"Erreur lancement extraction daily : {exc}",
            target_day=target_day,
            stdout_tail="",
            stderr_tail=traceback.format_exc(),
            command=command_text,
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    if completed.returncode != 0:
        return _empty_response(
            status="daily_fetch_failed",
            message=f"Extraction daily échouée. Code retour : {completed.returncode}",
            target_day=target_day,
            stdout_tail=stdout,
            stderr_tail=stderr,
            command=command_text,
        )

    try:
        matches, payload_name = _read_payload_for_day(target_day)
    except Exception as exc:
        return _empty_response(
            status="payload_read_failed",
            message=f"Payload daily illisible : {exc}",
            target_day=target_day,
            stdout_tail=stdout,
            stderr_tail=stderr + "\n" + traceback.format_exc(),
            command=command_text,
        )

    result = calculate_from_matches(matches)

    # Ajout des cotes APRES le calcul moteur :
    # le moteur reste inchangé et n'utilise jamais les cotes.
    try:
        result = enrich_result_with_sportytrader_odds(result, target_day)
    except Exception:
        result.setdefault("daily", {})
        result["daily"]["oddsSource"] = "SportyTrader"
        result["daily"]["oddsStatus"] = "failed"
        result["daily"]["oddsAudit"] = traceback.format_exc()[-1200:]

    result.setdefault("daily", {})
    result["daily"].update(
        {
            "targetDay": target_day,
            "payloadCount": len(matches),
            "payloadPath": payload_name,
            "stdoutTail": stdout[-1200:],
            "stderrTail": stderr[-1200:],
            "command": command_text,
        }
    )

    return result


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "Tennis Motor Railway Backend",
        "endpoints": [
            "/health",
            "/calculate",
            "/daily?day=today",
            "/daily?day=tomorrow",
            "/predictions?day=today",
        ],
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    try:
        state = get_state()
        history_rows = int(state.get("history_rows_loaded", 0))

        return {
            "status": "ok",
            "service": "Tennis Motor Railway Backend",
            "engine": "loaded",
            "historyRowsLoaded": history_rows,
            "oddsSource": "SportyTrader",
        }
    except Exception as exc:
        return {
            "status": "error",
            "service": "Tennis Motor Railway Backend",
            "engine": "not_loaded",
            "error": str(exc),
        }


@app.post("/calculate")
async def calculate(request: Request) -> Dict[str, Any]:
    try:
        matches = await _read_request_matches(request)
        return calculate_from_matches(matches)
    except Exception as exc:
        return _empty_response(
            status="calculate_failed",
            message=str(exc),
            stderr_tail=traceback.format_exc(),
        )


@app.post("/predictions")
async def predictions_post(request: Request) -> Dict[str, Any]:
    try:
        matches = await _read_request_matches(request)
        return calculate_from_matches(matches)
    except Exception as exc:
        return _empty_response(
            status="predictions_post_failed",
            message=str(exc),
            stderr_tail=traceback.format_exc(),
        )


@app.get("/daily")
async def daily(day: str = Query("today")) -> Dict[str, Any]:
    try:
        target_day = normalize_day(day)
    except Exception as exc:
        return _empty_response(
            status="bad_day_parameter",
            message=f"Paramètre day invalide : {day} | {exc}",
            target_day=str(day),
        )

    return await asyncio.to_thread(run_daily_fetch_sync, target_day)


@app.get("/predictions")
async def predictions_get(day: str = Query("today")) -> Dict[str, Any]:
    try:
        target_day = normalize_day(day)
    except Exception as exc:
        return _empty_response(
            status="bad_day_parameter",
            message=f"Paramètre day invalide : {day} | {exc}",
            target_day=str(day),
        )

    return await asyncio.to_thread(run_daily_fetch_sync, target_day)


@app.get("/state")
async def state() -> Dict[str, Any]:
    try:
        s = get_state()
        return {
            "status": "ok",
            "historyRowsLoaded": s.get("history_rows_loaded", 0),
            "rankReferenceSize": len(s.get("rank_reference_points", [])),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }



@app.get("/audit")
async def audit(day: str = Query("today")) -> Dict[str, Any]:
    try:
        target_day = normalize_day(day)
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Paramètre day invalide : {day} | {exc}",
        }

    text = _read_audit_for_day(target_day)

    return {
        "status": "ok" if text else "empty",
        "targetDay": target_day,
        "dailyScript": DAILY_SCRIPT_NAME,
        "audit": text,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
