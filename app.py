from __future__ import annotations

import asyncio
import json
import re
import os
import subprocess
import sys
import threading
import traceback
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from motor import HISTORY_YEARS, calculate_predictions, calculate_match_prediction, get_state


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PAYLOAD_LATEST_PATH = OUTPUT_DIR / "payload_latest.json"
DAILY_SCRIPT_NAME = "fetch_day_lines_v6_10k_daily_schedule_no_forced_veto.py"

# Source cotes : Flashscore tennis.
# On garde ATP pour les matchs + points + moteur.
# Flashscore sert uniquement à ajouter les cotes 1 / 2 après calcul moteur.
FLASHSCORE_TENNIS_URL = "https://www.flashscore.fr/tennis/"

# Historique 2026 automatique.
# update_2026_history.py met à jour uniquement les matchs ATP terminés.
# Les matchs du jour non terminés ne sont jamais injectés dans l'Elo.
UPDATE_2026_SCRIPT = BASE_DIR / "update_2026_history.py"
UPDATE_2026_MARKER = OUTPUT_DIR / "update_2026_last_run.json"
_UPDATE_2026_LOCK = threading.Lock()

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


def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _paris_now_iso() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Paris")).isoformat()
    except Exception:
        return datetime.now().isoformat()


def _tail(text: str, max_chars: int = 4000) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _reset_motor_state_cache() -> Dict[str, Any]:
    """
    Important : /health peut charger le moteur avant la mise à jour 2026.
    Après une mise à jour réussie de data/2026.csv, on vide le cache moteur
    pour que l'analyse suivante reconstruise l'Elo avec 2026 inclus.
    """
    try:
        import motor

        if hasattr(motor, "_STATE"):
            motor._STATE = None
            return {"status": "ok", "message": "motor._STATE réinitialisé"}
        return {"status": "skipped", "message": "motor._STATE introuvable"}
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}




def _read_update_2026_audit() -> Dict[str, Any]:
    """Lit le dernier audit écrit par update_2026_history.py.

    Le script peut finir avec returncode 0 même quand il n'a rien pu ajouter
    faute de payload ou faute de résultats Flashscore. Dans ces cas-là, il ne faut
    surtout pas écrire le marqueur already_done_today, sinon la mise à jour 2026
    reste bloquée jusqu'au lendemain.
    """
    audit_path = OUTPUT_DIR / "update_2026_history_audit.json"
    try:
        if audit_path.exists():
            data = json.loads(audit_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        return {"auditReadError": f"{type(exc).__name__}: {exc}"}
    return {}


def _update_2026_marker_allowed(audit_data: Dict[str, Any], stdout_text: str) -> Tuple[bool, str]:
    """Décide si on peut écrire update_2026_last_run.json.

    Autorisé uniquement quand le script a réellement traité la veille :
    - lignes ajoutées dans 2026.csv ;
    - ou lignes déjà présentes détectées comme doublons ;
    - ou payload + résultats Flashscore exploitables mais aucun match à ajouter.

    Refusé quand la réussite est un faux succès technique : pas de payload ou
    aucun résultat terminé exploitable. Dans ce cas on réessaiera au prochain clic.
    """
    text = stdout_text or ""
    reason_text = str(audit_data.get("message") or "") + "\n" + text

    if "reason=no_payload_for_target_day" in text or "Aucun payload Tennis Motor" in reason_text:
        return False, "no_payload_for_target_day"

    if "reason=no_completed_flashscore_rows" in text or "aucun match terminé exploitable" in reason_text.lower():
        return False, "no_completed_flashscore_rows"

    added = int(audit_data.get("addedRows") or 0)
    duplicates = int(audit_data.get("skippedDuplicate") or 0)
    payload_rows = int(audit_data.get("payloadRows") or 0)
    completed_rows = int(audit_data.get("completedRows") or 0)

    if added > 0:
        return True, "rows_added"

    if duplicates > 0:
        return True, "already_in_2026_csv"

    if payload_rows > 0 and completed_rows > 0:
        return True, "processed_no_new_rows"

    return False, "no_confirmed_history_processing"

def run_update_2026_history_if_needed(force: bool = False) -> Dict[str, Any]:
    """
    Lance update_2026_history.py automatiquement avant une analyse.

    Règles :
    - une seule mise à jour réussie par jour ;
    - si la mise à jour échoue, on réessaiera au prochain lancement ;
    - ne met jamais les matchs du jour non terminés dans l'Elo ;
    - après une réussite, le cache moteur est vidé pour recharger data/2026.csv.
    """
    enabled = _bool_env("AUTO_UPDATE_2026_HISTORY", True)
    today = _paris_today().isoformat()

    if not enabled:
        return {
            "enabled": False,
            "ran": False,
            "ok": True,
            "status": "disabled",
            "date": today,
            "script": UPDATE_2026_SCRIPT.name,
        }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not UPDATE_2026_SCRIPT.exists():
        return {
            "enabled": True,
            "ran": False,
            "ok": False,
            "status": "missing_script",
            "date": today,
            "script": str(UPDATE_2026_SCRIPT),
            "message": "update_2026_history.py introuvable dans le dossier backend.",
        }

    # FIX 2026 / PREMIUM HISTORY
    # Ancien bug : si update_2026_last_run.json existait déjà pour aujourd'hui,
    # Railway retournait "already_done_today" et ne relançait pas update_2026_history.py.
    # Résultat : les anciens premiums restaient pending et les dates précédentes
    # n'étaient pas recheckées.
    #
    # Nouvelle règle : chaque appel /daily relance update_2026_history.py.
    # Le script update_2026_history.py est idempotent : il ignore les doublons,
    # n'ajoute pas les matchs interrompus/non terminés dans 2026.csv, et settle
    # uniquement les picks avec vainqueur final fiable.
    marker_skip_disabled = True

    if False and not force and UPDATE_2026_MARKER.exists():
        try:
            marker = json.loads(UPDATE_2026_MARKER.read_text(encoding="utf-8"))
            if marker.get("date") == today and marker.get("ok") is True:
                return {
                    "enabled": True,
                    "ran": False,
                    "ok": True,
                    "status": "already_done_today",
                    "date": today,
                    "script": UPDATE_2026_SCRIPT.name,
                    "last_run": marker,
                }
        except Exception:
            # Marqueur illisible : on relance proprement.
            pass

    if not _UPDATE_2026_LOCK.acquire(blocking=False):
        return {
            "enabled": True,
            "ran": False,
            "ok": False,
            "status": "already_running",
            "date": today,
            "script": UPDATE_2026_SCRIPT.name,
        }

    try:
        timeout_seconds = int(os.environ.get("UPDATE_2026_TIMEOUT_SECONDS", "540"))
        completed = subprocess.run(
            [sys.executable, str(UPDATE_2026_SCRIPT)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

        ok = completed.returncode == 0
        result: Dict[str, Any] = {
            "enabled": True,
            "ran": True,
            "ok": ok,
            "status": "ok" if ok else "error",
            "date": today,
            "script": UPDATE_2026_SCRIPT.name,
            "returncode": completed.returncode,
            "stdoutTail": _tail(completed.stdout),
            "stderrTail": _tail(completed.stderr),
        }

        if ok:
            audit_data = _read_update_2026_audit()
            marker_allowed, marker_reason = _update_2026_marker_allowed(audit_data, completed.stdout)
            result["auditData"] = audit_data
            result["markerAllowed"] = marker_allowed
            result["markerReason"] = marker_reason

            if marker_allowed:
                reset_info = _reset_motor_state_cache()
                result["motorReload"] = reset_info

                UPDATE_2026_MARKER.write_text(
                    json.dumps(
                        {
                            "date": today,
                            "ok": True,
                            "script": UPDATE_2026_SCRIPT.name,
                            "reason": marker_reason,
                            "ranAtParis": _paris_now_iso(),
                            "returncode": completed.returncode,
                            "addedRows": audit_data.get("addedRows", 0),
                            "payloadRows": audit_data.get("payloadRows", 0),
                            "completedRows": audit_data.get("completedRows", 0),
                            "skippedDuplicate": audit_data.get("skippedDuplicate", 0),
                            "motorReload": reset_info,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            else:
                result["ok"] = False
                result["status"] = "not_marked_done"
                result["message"] = (
                    "update_2026_history.py a fini sans erreur Python, mais la mise à jour "
                    "2026 n'est pas confirmée. Le marqueur already_done_today n'a pas été écrit ; "
                    "le backend réessaiera au prochain lancement."
                )

        return result

    except subprocess.TimeoutExpired as exc:
        return {
            "enabled": True,
            "ran": True,
            "ok": False,
            "status": "timeout",
            "date": today,
            "script": UPDATE_2026_SCRIPT.name,
            "timeoutSeconds": int(os.environ.get("UPDATE_2026_TIMEOUT_SECONDS", "540")),
            "stdoutTail": _tail(exc.stdout if isinstance(exc.stdout, str) else ""),
            "stderrTail": _tail(exc.stderr if isinstance(exc.stderr, str) else ""),
        }

    except Exception as exc:
        return {
            "enabled": True,
            "ran": True,
            "ok": False,
            "status": "exception",
            "date": today,
            "script": UPDATE_2026_SCRIPT.name,
            "error": repr(exc),
        }

    finally:
        _UPDATE_2026_LOCK.release()


def _attach_update_2026(result: Dict[str, Any], update2026: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result
    result.setdefault("daily", {})
    result["daily"]["update2026"] = update2026
    return result


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
            "historyYears": list(HISTORY_YEARS),
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
    """
    Comparaison joueurs robuste pour SportyTrader.

    SportyTrader affiche parfois seulement le nom de famille :
    - "Hanfmann" au lieu de "Yannick Hanfmann"
    - "Darderi" au lieu de "Luciano Darderi"

    Le moteur ne l'utilise pas. Cette fonction sert seulement à accrocher la cote
    au bon match pour l'affichage Unity.
    """
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

    # Cas SportyTrader fréquent : un côté = nom de famille uniquement.
    if len(ta) == 1 and len(ta[0]) >= 4 and ta[0] == last_b:
        return True

    if len(tb) == 1 and len(tb[0]) >= 4 and tb[0] == last_a:
        return True

    # Cas nom composé partiel : "Mpetshi Perricard" / "Giovanni Mpetshi Perricard".
    if len(ta) >= 2 and len(tb) >= 2:
        tail_a = " ".join(ta[-2:])
        tail_b = " ".join(tb[-2:])
        if tail_a == tail_b:
            return True

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


def _last_name_key(name: str) -> str:
    tokens = _name_tokens(name)
    if not tokens:
        return ""
    return tokens[-1]


def _player_search_keys(name: str) -> List[str]:
    tokens = _name_tokens(name)
    keys: List[str] = []

    if not tokens:
        return keys

    full = " ".join(tokens)
    keys.append(full)

    if len(tokens) >= 2:
        keys.append(" ".join(tokens[-2:]))

    keys.append(tokens[-1])

    # dédoublonnage, plus long d'abord
    out: List[str] = []
    for key in sorted(keys, key=len, reverse=True):
        if key and key not in out:
            out.append(key)

    return out


def _line_has_player(norm_line: str, player_name: str) -> bool:
    for key in _player_search_keys(player_name):
        if re.search(rf"\b{re.escape(key)}\b", norm_line):
            return True
    return False


def _first_key_position(norm_line: str, player_name: str) -> int:
    positions: List[int] = []
    for key in _player_search_keys(player_name):
        m = re.search(rf"\b{re.escape(key)}\b", norm_line)
        if m:
            positions.append(m.start())
    return min(positions) if positions else 10**9


def _extract_two_odds_from_lines(lines: List[str], start_index: int) -> List[str]:
    odds: List[str] = []

    # SportyTrader met généralement les cotes dans les lignes juste après le match.
    for item in lines[start_index + 1: start_index + 22]:
        value = item.strip().replace(",", ".")
        if _looks_like_odd(value) and value not in {"1", "2"}:
            odds.append(_extract_decimal(value))

        if len(odds) >= 2:
            return odds

    # Fallback : si tout est sur une ligne.
    nearby = " ".join(lines[start_index: start_index + 8])
    nums = re.findall(r"\b\d{1,2}(?:[.,]\d{1,2})\b", nearby)
    for num in nums:
        if _looks_like_odd(num) and num.replace(",", ".") not in {"1", "2"}:
            odds.append(_extract_decimal(num))
        if len(odds) >= 2:
            break

    return odds[:2]


def _find_odds_for_match_in_content(player_a: str, player_b: str, content: str) -> Dict[str, str]:
    if not player_a or not player_b or not content:
        return {}

    lines = [re.sub(r"\s+", " ", x).strip() for x in content.splitlines() if re.sub(r"\s+", " ", x).strip()]
    norm_lines = [_norm_name(x) for x in lines]

    for i, norm_line in enumerate(norm_lines):
        if not _line_has_player(norm_line, player_a):
            continue
        if not _line_has_player(norm_line, player_b):
            continue

        odds = _extract_two_odds_from_lines(lines, i)
        if len(odds) < 2:
            continue

        pos_a = _first_key_position(norm_line, player_a)
        pos_b = _first_key_position(norm_line, player_b)

        if pos_a <= pos_b:
            return {
                "oddA": odds[0],
                "oddB": odds[1],
                "sourceLine": lines[i],
                "orientation": "same",
            }

        return {
            "oddA": odds[1],
            "oddB": odds[0],
            "sourceLine": lines[i],
            "orientation": "reversed",
        }

    return {}


def fetch_sportytrader_pages_texts() -> Tuple[List[Dict[str, str]], str]:
    pages: List[Dict[str, str]] = []
    audit: List[str] = []

    for url in SPORTYTRADER_ATP_ODDS_URLS:
        try:
            content = _fetch_sportytrader_text(url)
            pages.append({"url": url, "content": content})
            audit.append(f"{url} content_len={len(content)}")
        except Exception as exc:
            audit.append(f"{url} error={type(exc).__name__}: {exc}")

    return pages, " | ".join(audit)


def enrich_result_with_sportytrader_odds(result: Dict[str, Any], target_day: str) -> Dict[str, Any]:
    """
    Ajoute les cotes SportyTrader après le calcul moteur.

    Version propre :
    - ATP reste source des matchs.
    - On lit les pages tournoi SportyTrader.
    - Pour chaque match ATP, on cherche directement sa ligne SportyTrader.
    - On n'utilise jamais les cotes dans le moteur.
    """
    if not isinstance(result, dict):
        return result

    matches = result.get("matches")
    if not isinstance(matches, list) or not matches:
        return result

    try:
        pages, odds_audit = fetch_sportytrader_pages_texts()
    except Exception as exc:
        pages = []
        odds_audit = f"global_error={type(exc).__name__}: {exc}"

    matched_count = 0
    found_lines: List[str] = []

    for match in matches:
        if not isinstance(match, dict):
            continue

        player_a = str(match.get("playerA") or match.get("player_a") or "")
        player_b = str(match.get("playerB") or match.get("player_b") or "")

        found: Dict[str, str] = {}

        for page in pages:
            found = _find_odds_for_match_in_content(player_a, player_b, page.get("content", ""))
            if found:
                found["sourceUrl"] = page.get("url", "")
                break

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
            match["oddsSourceMatch"] = found.get("sourceLine", "")
            match["oddsSourceUrl"] = found.get("sourceUrl", "")
            matched_count += 1

            if len(found_lines) < 8:
                found_lines.append(f"{player_a} - {player_b} => {odd_a}/{odd_b} via {found.get('sourceLine', '')}")

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
    result["daily"]["oddsSource"] = "Flashscore"
    result["daily"]["oddsRowsFound"] = len(pages)
    result["daily"]["oddsMatched"] = matched_count
    result["daily"]["oddsAudit"] = (odds_audit + " | matched_sample=" + " || ".join(found_lines))[-4000:]

    return result



def _flashscore_click_optional(page, labels: List[str], timeout_ms: int = 1500) -> bool:
    for label in labels:
        selectors = [
            f"text={label}",
            f"button:has-text('{label}')",
            f"[role='button']:has-text('{label}')",
        ]
        for selector in selectors:
            try:
                page.locator(selector).first.click(timeout=timeout_ms)
                return True
            except Exception:
                pass
    return False


def _parse_odd_text(value: str) -> str:
    """
    Parse strict des cotes européennes.

    Accepte uniquement :
    - 1.56
    - 2.52
    - 1,35

    Refuse explicitement :
    - scores : 6, 3, 2, 0
    - scores tennis : 2/6, 15/40, 6/3
    - textes mélangés
    """
    raw = (value or "").strip()

    if "/" in raw:
        return ""

    if not re.fullmatch(r"\d{1,2}[.,]\d{1,2}", raw):
        return ""

    normalized = raw.replace(",", ".")

    try:
        number = float(normalized)
    except Exception:
        return ""

    if number < 1.01 or number > 100.0:
        return ""

    return f"{number:.2f}".rstrip("0").rstrip(".")


def _flashscore_extract_rows_js() -> str:
    return r"""
() => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

    const readText = (root, selectors) => {
        for (const sel of selectors) {
            const el = root.querySelector(sel);
            if (el) {
                const t = clean(el.textContent);
                if (t) return t;
            }
        }
        return '';
    };

    const readOdds = (root) => {
        const odds = [];
        const seen = new Set();

        const addOdd = (txt) => {
            txt = clean(txt);
            if (!txt || seen.has(txt)) return;

            // Cotes européennes uniquement : 1.40 / 2.36 / 10.5
            if (/^\d{1,2}[.,]\d{1,2}$/.test(txt)) {
                const val = parseFloat(txt.replace(',', '.'));
                if (val >= 1.01 && val <= 100) {
                    seen.add(txt);
                    odds.push(txt);
                }
            }
        };

        const parseOddsFromText = (txt) => {
            txt = clean(txt);
            if (!txt) return;

            // Extrait 1.40 / 2.36 / 10,5 depuis le texte brut du bloc match.
            const re = /(^|[^\d])(\d{1,2}[.,]\d{1,2})(?!\d)/g;
            let m;
            while ((m = re.exec(txt)) !== null) {
                addOdd(m[2]);
                if (odds.length >= 2) return;
            }
        };

        const selectors = [
            '[class*="event__odd"]',
            '[class*="oddsValue"]',
            '[class*="odds"]',
            '[class*="bookmaker"]',
            'button',
            'span',
            'div'
        ];

        for (const sel of selectors) {
            root.querySelectorAll(sel).forEach((el) => {
                addOdd(el.textContent);
            });
            if (odds.length >= 2) break;
        }

        // Fallback important : sur Flashscore, les cotes sont parfois dans le
        // texte du bloc ligne mais sans classe "event__odd" stable.
        if (odds.length < 2) {
            parseOddsFromText(root.innerText || root.textContent || '');
        }

        // Fallback parent : selon la structure DOM, les cotes peuvent être dans
        // un conteneur voisin du participant.
        if (odds.length < 2 && root.parentElement) {
            parseOddsFromText(root.parentElement.innerText || root.parentElement.textContent || '');
        }

        if (odds.length < 2 && root.parentElement && root.parentElement.parentElement) {
            parseOddsFromText(root.parentElement.parentElement.innerText || root.parentElement.parentElement.textContent || '');
        }

        return odds.slice(0, 2);
    };

    const rows = [];
    const matchSelectors = [
        '[class*="event__match"]',
        '[id^="g_2_"]',
        '[id^="g_1_"]'
    ];

    let nodes = [];
    for (const sel of matchSelectors) {
        nodes = Array.from(document.querySelectorAll(sel));
        if (nodes.length) break;
    }

    for (const node of nodes) {
        const playerA = readText(node, [
            '[class*="event__participant--home"]',
            '[class*="participant__participantNameWrapper"]:nth-of-type(1)',
            '[class*="participantName"]:nth-of-type(1)'
        ]);

        const playerB = readText(node, [
            '[class*="event__participant--away"]',
            '[class*="participant__participantNameWrapper"]:nth-of-type(2)',
            '[class*="participantName"]:nth-of-type(2)'
        ]);

        const time = readText(node, [
            '[class*="event__time"]',
            '[class*="event__stage"]'
        ]);

        const odds = readOdds(node);

        if (playerA && playerB) {
            rows.push({
                playerA,
                playerB,
                oddA: odds[0] || '',
                oddB: odds[1] || '',
                time,
                raw: clean(node.innerText || node.textContent || '')
            });
        }
    }

    return rows;
}
"""


def _looks_like_flashscore_player_line(line: str) -> bool:
    line = re.sub(r"\s+", " ", line or "").strip()

    if not line or len(line) > 70:
        return False

    low = line.lower()
    banned = [
        "atp", "wta", "simples", "doubles", "classement", "publicité",
        "preview", "live", "bet", "terminé", "annulé", "reporté",
        "tous", "direct", "cotes", "prévus", "prevus", "calendrier"
    ]

    if any(x in low for x in banned):
        return False

    if re.fullmatch(r"\d+", line):
        return False

    if re.fullmatch(r"\d{1,2}:\d{2}", line):
        return False

    if _parse_odd_text(line):
        return False

    return bool(re.search(r"[A-Za-zÀ-ÿ]", line))


def _flashscore_extract_rows_from_text(content: str) -> List[Dict[str, str]]:
    """
    Fallback texte score-aware.

    Corrige le cas visible dans ton navigateur :
    Terminé / Ruud C. / 2 / 6 / 6 / Lehecka J. / 0 / 3 / 4 / 1.56 / 2.52

    On refuse les scores entiers et on cherche les deux premières vraies cotes décimales.
    """
    lines = [re.sub(r"\s+", " ", x).strip() for x in (content or "").splitlines()]
    lines = [x for x in lines if x]

    rows: List[Dict[str, str]] = []
    seen = set()

    def collect_decimal_odds(start_index: int, end_index: int) -> List[str]:
        odds: List[str] = []
        for candidate in lines[start_index:end_index]:
            parsed = _parse_odd_text(candidate)
            if parsed and parsed not in odds:
                odds.append(parsed)
            if len(odds) >= 2:
                break
        return odds

    for i in range(len(lines) - 1):
        player_a = lines[i]
        player_b = lines[i + 1]

        direct_pair = (
            _looks_like_flashscore_player_line(player_a)
            and _looks_like_flashscore_player_line(player_b)
        )

        score_pair_index = -1

        if not direct_pair and _looks_like_flashscore_player_line(player_a):
            # Match terminé/live : le joueur B peut être après des scores entiers.
            for j in range(i + 2, min(i + 9, len(lines))):
                between = lines[i + 1:j]
                if not between:
                    continue

                only_scores = all(re.fullmatch(r"\d+", x or "") for x in between)
                if only_scores and _looks_like_flashscore_player_line(lines[j]):
                    score_pair_index = j
                    player_b = lines[j]
                    break

        if not direct_pair and score_pair_index < 0:
            continue

        key = (_norm_name(player_a), _norm_name(player_b))
        if not key[0] or not key[1] or key in seen:
            continue

        seen.add(key)

        odds_start = score_pair_index + 1 if score_pair_index >= 0 else i + 2
        odds = collect_decimal_odds(odds_start, min(odds_start + 22, len(lines)))

        rows.append({
            "playerA": player_a,
            "playerB": player_b,
            "oddA": odds[0] if len(odds) > 0 else "",
            "oddB": odds[1] if len(odds) > 1 else "",
            "time": "",
            "raw": " | ".join(lines[i:min(i + 22, len(lines))]),
        })

    return rows


def _flashscore_tokens_keep_initials(name: str) -> List[str]:
    """
    Tokens Flashscore sans supprimer les initiales.
    Exemple :
    - "Cilic M." -> ["cilic", "m"]
    - "Auger-Aliassime F." -> ["auger", "aliassime", "f"]
    """
    value = _norm_name(name)
    return [x for x in value.split() if x]


def _normalize_flashscore_initial_name(name: str) -> List[str]:
    return _flashscore_tokens_keep_initials(name)


def _same_player_flashscore(full_name: str, flash_name: str) -> bool:
    """
    Matching robuste ATP full name <-> Flashscore.
    Corrige le bug principal :
    _name_tokens supprimait les initiales d'une lettre, donc "Cilic M."
    devenait seulement ["cilic"] et les cas nom composé + initiale étaient mal reconnus.
    """
    if _same_player(full_name, flash_name):
        return True

    full = _flashscore_tokens_keep_initials(full_name)
    flash = _flashscore_tokens_keep_initials(flash_name)

    if not full or not flash:
        return False

    full_first_initial = full[0][0] if full[0] else ""
    full_last = full[-1]
    full_tail_2 = " ".join(full[-2:]) if len(full) >= 2 else full_last
    full_tail_3 = " ".join(full[-3:]) if len(full) >= 3 else full_tail_2

    # Cas exact après normalisation.
    if " ".join(full) == " ".join(flash):
        return True

    # Flashscore : "Cilic M.", "Norrie C.", "Auger Aliassime F."
    if len(flash) >= 2 and len(flash[-1]) == 1:
        initial = flash[-1]
        surname_parts = flash[:-1]
        surname = " ".join(surname_parts)

        if initial == full_first_initial:
            # Nom composé complet : Felix Auger Aliassime <-> Auger Aliassime F.
            if surname == full_tail_2 or surname == full_tail_3:
                return True

            # Sécurité nom de famille simple : Marin Cilic <-> Cilic M.
            if surname_parts and surname_parts[-1] == full_last:
                return True

    # Flashscore peut parfois afficher nom de famille seul.
    if len(flash) == 1 and len(flash[0]) >= 4:
        if flash[0] == full_last:
            return True
        if flash[0] in full:
            return True

    # Cas sans initiale mais nom composé partiel.
    flash_join = " ".join(flash)
    if len(flash) >= 2:
        if flash_join == full_tail_2 or flash_join == full_tail_3:
            return True

    return False



def _flashscore_count_match_nodes_js() -> str:
    return r"""
() => {
    const selectors = [
        '[class*="event__match"]',
        '[id^="g_2_"]',
        '[id^="g_1_"]'
    ];

    for (const sel of selectors) {
        const nodes = document.querySelectorAll(sel);
        if (nodes && nodes.length) return nodes.length;
    }

    return 0;
}
"""


def _flashscore_scroll_until_stable(page, audit: List[str], max_rounds: int = 22) -> None:
    """
    Scroll réel de la page Flashscore.

    Objectif :
    - charger les matchs plus bas dans la page ;
    - attendre le lazy-load ;
    - arrêter seulement quand le nombre de lignes ne monte plus.
    """
    last_count = -1
    stable_rounds = 0

    for round_index in range(max_rounds):
        try:
            current_count = int(page.evaluate(_flashscore_count_match_nodes_js()))
        except Exception:
            current_count = -1

        audit.append(f"scroll_round={round_index + 1} rows_before={current_count}")

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass

        try:
            page.mouse.wheel(0, 1400)
        except Exception:
            pass

        page.wait_for_timeout(900)

        try:
            new_count = int(page.evaluate(_flashscore_count_match_nodes_js()))
        except Exception:
            new_count = current_count

        audit.append(f"scroll_round={round_index + 1} rows_after={new_count}")

        if new_count <= last_count or new_count == current_count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        last_count = max(last_count, new_count)

        # 3 tours sans nouvelle ligne = page chargée.
        if stable_rounds >= 3:
            break

    # Remonte légèrement en haut pour garder la page stable avant extraction finale.
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
    except Exception:
        pass


def fetch_flashscore_tennis_odds() -> Tuple[List[Dict[str, str]], str]:
    audit: List[str] = []

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return [], f"playwright_import_error={type(exc).__name__}: {exc}"

    rows: List[Dict[str, str]] = []

    try:
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
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1365, "height": 1800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                },
            )

            page = context.new_page()
            page.goto(FLASHSCORE_TENNIS_URL, wait_until="domcontentloaded", timeout=45000)

            _flashscore_click_optional(page, ["J'accepte", "Tout refuser", "Accepter", "OK"], timeout_ms=2500)

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # V4 :
            # Scanner plusieurs onglets au lieu de seulement COTES.
            # Flashscore ne met pas toujours tous les matchs du jour dans le même onglet.
            # On combine TOUS + COTES + PRÉVUS pour récupérer plus de paires.
            all_rows: List[Dict[str, str]] = []
            seen_rows = set()

            tab_sets = [
                ["TOUS", "Tous"],
                ["COTES", "Cotes"],
                ["PRÉVUS", "PREVUS", "Prévus", "Prevus"],
            ]

            for tab_labels in tab_sets:
                clicked = _flashscore_click_optional(page, tab_labels, timeout_ms=2500)
                page.wait_for_timeout(2200)

                audit.append(f"tab_scan={tab_labels[0]} clicked={clicked}")

                # Scroll pour charger toutes les lignes de cet onglet.
                _flashscore_scroll_until_stable(page, audit, max_rounds=18)

                tab_rows: List[Dict[str, str]] = []

                try:
                    tab_rows = page.evaluate(_flashscore_extract_rows_js())
                    audit.append(f"tab={tab_labels[0]} dom_rows={len(tab_rows)}")
                except Exception as exc:
                    audit.append(f"tab={tab_labels[0]} dom_extract_error={type(exc).__name__}: {exc}")
                    tab_rows = []

                # Toujours ajouter le fallback texte : les vraies cotes peuvent être
                # dans le texte global même si le sous-bloc DOM de la ligne ne les contient pas.
                try:
                    content = page.locator("body").inner_text(timeout=15000)
                    text_rows = _flashscore_extract_rows_from_text(content)
                    audit.append(f"tab={tab_labels[0]} text_rows={len(text_rows)} content_len={len(content)}")
                    tab_rows.extend(text_rows)
                except Exception as exc:
                    audit.append(f"tab={tab_labels[0]} text_extract_error={type(exc).__name__}: {exc}")

                for row in tab_rows:
                    a = str(row.get("playerA", "")).strip()
                    b = str(row.get("playerB", "")).strip()
                    oa = str(row.get("oddA", "")).strip()
                    ob = str(row.get("oddB", "")).strip()

                    if not a or not b:
                        continue

                    # On garde prioritairement les lignes qui ont des cotes.
                    # Les lignes sans cotes servent rarement à l'affichage.
                    key_pair = (_norm_name(a), _norm_name(b))
                    if not key_pair[0] or not key_pair[1]:
                        continue

                    replaced = False
                    for idx, existing in enumerate(all_rows):
                        existing_pair = (_norm_name(existing.get("playerA", "")), _norm_name(existing.get("playerB", "")))
                        if existing_pair == key_pair:
                            existing_has_odds = bool(existing.get("oddA")) and bool(existing.get("oddB"))
                            new_has_odds = bool(oa) and bool(ob)
                            if new_has_odds and not existing_has_odds:
                                all_rows[idx] = row
                            replaced = True
                            break

                    if replaced:
                        continue

                    all_rows.append(row)

            rows = all_rows
            audit.append(f"dom_rows_combined={len(rows)}")
            audit.append("odds_decimal_only_filter=on")
            audit.append("text_fallback_score_aware=on")
            audit.append("strict_parse_no_slash=on")

            if rows:
                sample = []
                for row in rows[:15]:
                    sample.append(f"{row.get('playerA')} - {row.get('playerB')} = {row.get('oddA')}/{row.get('oddB')}")
                audit.append("sample=" + " || ".join(sample))

            browser.close()

    except Exception as exc:
        audit.append(f"flashscore_error={type(exc).__name__}: {exc}")

    # Nettoyage : garder seulement les lignes avec deux joueurs.
    clean_rows: List[Dict[str, str]] = []
    seen = set()

    for row in rows:
        a = str(row.get("playerA", "")).strip()
        b = str(row.get("playerB", "")).strip()

        if not a or not b:
            continue
        if "/" in a or "/" in b:
            continue

        key = (_norm_name(a), _norm_name(b))
        if key in seen:
            continue
        seen.add(key)

        clean_rows.append({
            "playerA": a,
            "playerB": b,
            "oddA": _parse_odd_text(str(row.get("oddA", ""))),
            "oddB": _parse_odd_text(str(row.get("oddB", ""))),
            "time": str(row.get("time", "")),
            "raw": str(row.get("raw", ""))[:300],
        })

    return clean_rows, " | ".join(audit)


def _flashscore_match_keys(name: str) -> List[str]:
    """
    Clés très tolérantes pour matcher ATP ↔ Flashscore.
    Exemples :
    - Yannick Hanfmann -> ["yannick hanfmann", "hanfmann"]
    - Alex de Minaur -> ["alex de minaur", "de minaur", "minaur"]
    - Giovanni Mpetshi Perricard -> ["giovanni mpetshi perricard", "mpetshi perricard", "perricard"]
    """
    tokens = _name_tokens(name)
    if not tokens:
        return []

    keys: List[str] = []
    keys.append(" ".join(tokens))

    if len(tokens) >= 2:
        keys.append(" ".join(tokens[-2:]))

    keys.append(tokens[-1])

    out: List[str] = []
    for key in sorted(keys, key=len, reverse=True):
        if key and len(key) >= 3 and key not in out:
            out.append(key)

    return out


def _contains_match_key(text: str, player_name: str) -> bool:
    norm = _norm_name(text)
    for key in _flashscore_match_keys(player_name):
        if re.search(rf"\b{re.escape(key)}\b", norm):
            return True
    return False


def _key_position(text: str, player_name: str) -> int:
    norm = _norm_name(text)
    positions: List[int] = []

    for key in _flashscore_match_keys(player_name):
        m = re.search(rf"\b{re.escape(key)}\b", norm)
        if m:
            positions.append(m.start())

    return min(positions) if positions else 10**9


def _find_flashscore_odds_for_match(player_a: str, player_b: str, rows: List[Dict[str, str]]) -> Dict[str, str]:
    for row in rows:
        fs_a = row.get("playerA", "")
        fs_b = row.get("playerB", "")
        raw = row.get("raw", "") or f"{fs_a} - {fs_b}"

        # Méthode normale.
        if _same_player_flashscore(player_a, fs_a) and _same_player_flashscore(player_b, fs_b):
            return {
                "oddA": row.get("oddA", ""),
                "oddB": row.get("oddB", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "same",
            }

        if _same_player_flashscore(player_a, fs_b) and _same_player_flashscore(player_b, fs_a):
            return {
                "oddA": row.get("oddB", ""),
                "oddB": row.get("oddA", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "reversed",
            }

        # Fallback très tolérant : Flashscore peut afficher nom + initiale ou juste nom de famille.
        a_in_fs_a = _contains_match_key(fs_a, player_a)
        b_in_fs_b = _contains_match_key(fs_b, player_b)

        if a_in_fs_a and b_in_fs_b:
            return {
                "oddA": row.get("oddA", ""),
                "oddB": row.get("oddB", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "same_fallback",
            }

        a_in_fs_b = _contains_match_key(fs_b, player_a)
        b_in_fs_a = _contains_match_key(fs_a, player_b)

        if a_in_fs_b and b_in_fs_a:
            return {
                "oddA": row.get("oddB", ""),
                "oddB": row.get("oddA", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "reversed_fallback",
            }

        # Dernier fallback : chercher les deux joueurs dans la ligne brute.
        if _contains_match_key(raw, player_a) and _contains_match_key(raw, player_b):
            pos_a = _key_position(raw, player_a)
            pos_b = _key_position(raw, player_b)

            if pos_a <= pos_b:
                return {
                    "oddA": row.get("oddA", ""),
                    "oddB": row.get("oddB", ""),
                    "sourcePlayerA": fs_a,
                    "sourcePlayerB": fs_b,
                    "orientation": "raw_same",
                }

            return {
                "oddA": row.get("oddB", ""),
                "oddB": row.get("oddA", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "raw_reversed",
            }

    return {}



def _find_flashscore_odds_for_match_with_source_pair(match: Dict[str, Any], rows: List[Dict[str, str]]) -> Dict[str, str]:
    """
    Retrouve les cotes même après double-side.

    Priorité :
    1. chercher directement avec le Joueur A/Joueur B affichés ;
    2. si non trouvé, chercher avec la paire ATP source d'origine ;
    3. si trouvé avec la paire source, remettre oddA sur le Joueur A actuellement affiché.

    Exemple :
    - paire source ATP : Andrea Pellegrino vs Arthur Fils
    - affichage moteur : Arthur Fils vs Andrea Pellegrino
    - Flashscore trouve la source : Pellegrino/Fils = 8.20/1.08
    - on renvoie pour l'affichage : Fils = 1.08, Pellegrino = 8.20
    """
    player_a = str(match.get("playerA") or match.get("player_a") or "")
    player_b = str(match.get("playerB") or match.get("player_b") or "")

    found = _find_flashscore_odds_for_match(player_a, player_b, rows)
    if found:
        found["matchMethod"] = "display_pair"
        return found

    source_a = str(match.get("sourcePlayerA") or "")
    source_b = str(match.get("sourcePlayerB") or "")

    if not source_a or not source_b:
        return {}

    source_found = _find_flashscore_odds_for_match(source_a, source_b, rows)
    if not source_found:
        return {}

    source_odd_a = source_found.get("oddA", "")
    source_odd_b = source_found.get("oddB", "")

    # Si l'affichage actuel est le même que la source.
    if _same_player_flashscore(player_a, source_a) and _same_player_flashscore(player_b, source_b):
        return {
            "oddA": source_odd_a,
            "oddB": source_odd_b,
            "sourcePlayerA": source_found.get("sourcePlayerA", ""),
            "sourcePlayerB": source_found.get("sourcePlayerB", ""),
            "orientation": source_found.get("orientation", "same") + "_via_source_pair",
            "matchMethod": "source_pair_same",
        }

    # Si l'affichage actuel est inversé par rapport à la source.
    if _same_player_flashscore(player_a, source_b) and _same_player_flashscore(player_b, source_a):
        return {
            "oddA": source_odd_b,
            "oddB": source_odd_a,
            "sourcePlayerA": source_found.get("sourcePlayerA", ""),
            "sourcePlayerB": source_found.get("sourcePlayerB", ""),
            "orientation": source_found.get("orientation", "same") + "_via_source_pair_swapped",
            "matchMethod": "source_pair_swapped",
        }

    return {}

def enrich_result_with_flashscore_odds(result: Dict[str, Any], target_day: str) -> Dict[str, Any]:
    """
    Ajoute les cotes Flashscore après le calcul moteur.
    Le moteur reste inchangé et n'utilise jamais les cotes.
    """
    if not isinstance(result, dict):
        return result

    matches = result.get("matches")
    if not isinstance(matches, list) or not matches:
        return result

    try:
        flash_rows, flash_audit = fetch_flashscore_tennis_odds()
    except Exception as exc:
        flash_rows = []
        flash_audit = f"global_error={type(exc).__name__}: {exc}"

    matched_count = 0
    matched_sample: List[str] = []

    for match in matches:
        if not isinstance(match, dict):
            continue

        player_a = str(match.get("playerA") or match.get("player_a") or "")
        player_b = str(match.get("playerB") or match.get("player_b") or "")

        found = _find_flashscore_odds_for_match_with_source_pair(match, flash_rows)

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
            match["oddsSource"] = "Flashscore"
            match["oddsStatus"] = "matched"
            match["oddsSourceMatch"] = f'{found.get("sourcePlayerA", "")} - {found.get("sourcePlayerB", "")}'
            matched_count += 1

            if len(matched_sample) < 8:
                matched_sample.append(f"{player_a} - {player_b} => {odd_a}/{odd_b} via {found.get('sourcePlayerA', '')} - {found.get('sourcePlayerB', '')} [{found.get('orientation', '')} | {found.get('matchMethod', '')}]")
        else:
            match.setdefault("oddA", "")
            match.setdefault("oddB", "")
            match.setdefault("playerAOdd", "")
            match.setdefault("playerBOdd", "")
            match.setdefault("player_a_odd", "")
            match.setdefault("player_b_odd", "")
            match.setdefault("coteA", "")
            match.setdefault("coteB", "")
            match["oddsSource"] = "Flashscore"
            match["oddsStatus"] = "not_found"

    result.setdefault("daily", {})
    result["daily"]["oddsSource"] = "Flashscore"
    result["daily"]["flashscoreRowsFound"] = len(flash_rows)
    result["daily"]["flashscoreMatched"] = matched_count
    result["daily"]["oddsRowsFound"] = len(flash_rows)
    result["daily"]["oddsMatched"] = matched_count
    result["daily"]["flashscoreAudit"] = (flash_audit + " | matched_sample=" + " || ".join(matched_sample))[-4000:]
    result["daily"]["oddsAudit"] = result["daily"]["flashscoreAudit"]

    return result


def _get_first_existing(match: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for key in keys:
        if key in match and match.get(key) is not None:
            return match.get(key)
    return default


def _reverse_match_for_engine(match: Dict[str, Any]) -> Dict[str, Any]:
    """
    Version inversée du même match pour analyser proprement l'autre joueur.
    Les cotes restent uniquement affichage et sont ajoutées après le calcul moteur.
    """
    rev = dict(match)

    player_a = str(_get_first_existing(match, ["playerA", "player_a"], "") or "")
    player_b = str(_get_first_existing(match, ["playerB", "player_b"], "") or "")

    points_a = _get_first_existing(match, ["playerAPoints", "player_a_points"], 0)
    points_b = _get_first_existing(match, ["playerBPoints", "player_b_points"], 0)

    rev["playerA"] = player_b
    rev["playerB"] = player_a
    rev["player_a"] = player_b
    rev["player_b"] = player_a

    rev["playerAPoints"] = points_b
    rev["playerBPoints"] = points_a
    rev["player_a_points"] = points_b
    rev["player_b_points"] = points_a

    # Après inversion, le nouveau joueur B est l'ancien joueur A.
    # Le payload daily actuel fournit surtout les infos du joueur B.
    # Si les infos du joueur A existent, on les reprend ; sinon fallback neutre false/0.
    old_a_is_qualifier = _get_first_existing(
        match,
        ["player_a_is_qualifier", "playerAIsQualifier", "player_a_qualifier", "playerAQualifier"],
        False,
    )
    old_a_tournament_wins = _get_first_existing(
        match,
        ["player_a_tournament_wins", "playerATournamentWins", "player_a_wins", "playerAWins"],
        0,
    )

    old_b_is_qualifier = _get_first_existing(
        match,
        ["player_b_is_qualifier", "playerBIsQualifier", "player_b_qualifier", "playerBQualifier"],
        False,
    )
    old_b_tournament_wins = _get_first_existing(
        match,
        ["player_b_tournament_wins", "playerBTournamentWins", "player_b_wins", "playerBWins"],
        0,
    )

    # Après inversion :
    # - nouveau joueur A = ancien joueur B ;
    # - nouveau joueur B = ancien joueur A.
    rev["player_a_is_qualifier"] = old_b_is_qualifier
    rev["playerAIsQualifier"] = old_b_is_qualifier
    rev["player_a_tournament_wins"] = old_b_tournament_wins
    rev["playerATournamentWins"] = old_b_tournament_wins

    rev["player_b_is_qualifier"] = old_a_is_qualifier
    rev["playerBIsQualifier"] = old_a_is_qualifier
    rev["player_b_tournament_wins"] = old_a_tournament_wins
    rev["playerBTournamentWins"] = old_a_tournament_wins

    return rev


def _premium_score(match: Dict[str, Any]) -> float:
    value = match.get("premiumPct", match.get("premium", 0.0))
    try:
        score = float(value)
    except Exception:
        return 0.0

    if 0.0 <= score <= 1.0:
        score *= 100.0

    return score


def _is_veto(match: Dict[str, Any]) -> bool:
    return str(match.get("veto", "")).strip().lower() in {"oui", "yes", "true", "1"}


def _rebuild_summary_from_matches(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(matches)
    error_rows = 0
    over80 = 0
    proches = 0
    veto_count = 0
    jouables = 0
    refuses_sans_veto = 0

    for match in matches:
        if match.get("error"):
            error_rows += 1

        premium_pct = _premium_score(match)
        veto = _is_veto(match)

        if veto:
            veto_count += 1

        if premium_pct >= 80.0 and not veto:
            over80 += 1
            jouables += 1
        elif 75.0 <= premium_pct < 80.0 and not veto:
            proches += 1
        elif not veto:
            refuses_sans_veto += 1

    return {
        "totalRows": total,
        "validRows": total - error_rows,
        "errorRows": error_rows,
        "over80": over80,
        "vetoCount": veto_count,
        "jouables": jouables,
        "proches": proches,
        "refusedNoVeto": refuses_sans_veto,
        "refusesSansVeto": refuses_sans_veto,
    }


def _to_bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value or "").strip().lower()
    return s in {"1", "true", "yes", "oui", "y", "o"}


def _to_int_value(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return default


def _copy_daily_context_to_prediction(
    source_match: Dict[str, Any],
    prediction: Dict[str, Any],
    orientation: str,
) -> Dict[str, Any]:
    """
    Garde les champs récupérés par le script daily V6.11H jusque dans la réponse Unity.

    Problème corrigé :
    - fetch_day_lines récupère bien player_b_tournament_wins=3 ;
    - calculate_match_prediction peut ne pas recopier ce champ dans son résultat ;
    - Unity voyait donc encore 0.

    Règle :
    - orientation original : A/B restent les mêmes que dans le payload ;
    - orientation reversed : A/B sont inversés, donc les contextes A/B doivent aussi être inversés.
    """
    if not isinstance(source_match, dict) or not isinstance(prediction, dict):
        return prediction

    source_a_qualifier = _to_bool_value(_get_first_existing(
        source_match,
        ["player_a_is_qualifier", "playerAIsQualifier", "player_a_qualifier", "playerAQualifier"],
        False,
    ))
    source_b_qualifier = _to_bool_value(_get_first_existing(
        source_match,
        ["player_b_is_qualifier", "playerBIsQualifier", "player_b_qualifier", "playerBQualifier"],
        False,
    ))

    source_a_wins = _to_int_value(_get_first_existing(
        source_match,
        ["player_a_tournament_wins", "playerATournamentWins", "player_a_wins", "playerAWins"],
        0,
    ))
    source_b_wins = _to_int_value(_get_first_existing(
        source_match,
        ["player_b_tournament_wins", "playerBTournamentWins", "player_b_wins", "playerBWins"],
        0,
    ))

    if orientation == "reversed":
        display_a_qualifier = source_b_qualifier
        display_b_qualifier = source_a_qualifier
        display_a_wins = source_b_wins
        display_b_wins = source_a_wins
    else:
        display_a_qualifier = source_a_qualifier
        display_b_qualifier = source_b_qualifier
        display_a_wins = source_a_wins
        display_b_wins = source_b_wins

    # Champs snake_case.
    prediction["player_a_is_qualifier"] = display_a_qualifier
    prediction["player_b_is_qualifier"] = display_b_qualifier
    prediction["player_a_tournament_wins"] = display_a_wins
    prediction["player_b_tournament_wins"] = display_b_wins

    # Champs camelCase attendus par certaines versions Unity.
    prediction["playerAIsQualifier"] = display_a_qualifier
    prediction["playerBIsQualifier"] = display_b_qualifier
    prediction["playerATournamentWins"] = display_a_wins
    prediction["playerBTournamentWins"] = display_b_wins

    # Alias courts déjà utilisés dans d'anciennes versions.
    prediction["playerAWins"] = display_a_wins
    prediction["playerBWins"] = display_b_wins

    # Debug lisible dans /daily si besoin.
    prediction["contextSource"] = "daily_payload_v6_11h"
    prediction["contextOrientation"] = orientation
    prediction["contextPlayerATournamentWins"] = display_a_wins
    prediction["contextPlayerBTournamentWins"] = display_b_wins

    return prediction



def calculate_from_matches(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not matches:
        return _empty_response(
            status="empty_payload",
            message="Aucun match exploitable dans le payload daily.",
        )

    # Correction importante :
    # Ne pas utiliser calculate_predictions() sur deux listes puis zip(),
    # car calculate_predictions() trie les résultats par premium.
    # On calcule donc chaque match A/B et B/A paire par paire, sans désalignement.
    state = get_state()

    final_matches: List[Dict[str, Any]] = []
    reversed_chosen = 0

    for match in matches:
        original_prediction = calculate_match_prediction(match, state)
        reversed_prediction = calculate_match_prediction(_reverse_match_for_engine(match), state)

        original_score = _premium_score(original_prediction)
        reversed_score = _premium_score(reversed_prediction)

        if reversed_score > original_score:
            chosen = dict(reversed_prediction)
            orientation = "reversed"
            reversed_chosen += 1
        else:
            chosen = dict(original_prediction)
            orientation = "original"

        # Correction V6.11H app.py :
        # calculate_match_prediction peut ne pas recopier les champs de contexte daily.
        # On les réinjecte explicitement pour que Unity affiche les vraies victoires tournoi.
        chosen = _copy_daily_context_to_prediction(match, chosen, orientation)

        # Garder la paire ATP d'origine pour rattacher les cotes après réorientation.
        # Flashscore peut afficher le match dans l'ordre ATP original alors que le moteur
        # choisit ensuite l'autre joueur en Joueur A.
        source_player_a = str(match.get("playerA") or match.get("player_a") or "")
        source_player_b = str(match.get("playerB") or match.get("player_b") or "")

        chosen["engineOrientation"] = orientation
        chosen["engineComparedOriginalPct"] = round(original_score, 3)
        chosen["engineComparedReversedPct"] = round(reversed_score, 3)
        chosen["sourcePlayerA"] = source_player_a
        chosen["sourcePlayerB"] = source_player_b
        chosen["sourceOriginalPair"] = f"{source_player_a} vs {source_player_b}"
        final_matches.append(chosen)

    final_matches.sort(key=lambda row: row.get("premium", -1), reverse=True)

    return {
        "matches": final_matches,
        "summary": _rebuild_summary_from_matches(final_matches),
        "engine": {
            "name": "Tennis Motor V7",
            "version": "Bayesian Shrinkage",
            "historyYears": list(HISTORY_YEARS),
            "historyRowsLoaded": state["history_rows_loaded"],
            "premiumFormula": "Bayesian shrinkage blend of SWE, ATP, Rank, Form5, Form10, SurfaceForm5, Dominance",
            "threshold": "> 0.80",
            "orientationMode": "double_side_pairwise_best_premium",
        },
        "daily": {
            "doubleSideStatus": "ok",
            "doubleSideMode": "pairwise_best_premium_no_zip_after_sort",
            "doubleSideMatches": len(final_matches),
            "doubleSideReversedChosen": reversed_chosen,
            "contextPropagation": "daily_payload_v6_11h_preserved",
        },
    }


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
        result = enrich_result_with_flashscore_odds(result, target_day)
    except Exception:
        result.setdefault("daily", {})
        result["daily"]["oddsSource"] = "Flashscore"
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

    # HISTORIQUE PREMIUM — correction V6.11J.
    # Problème corrigé :
    # - /daily trouvait bien les Premium ;
    # - /history ne les voyait pas parce que l'analyse du jour n'était pas enregistrée
    #   dans premium_history.json après le calcul.
    #
    # Sécurité :
    # - premium_history.record_result_json ignore automatiquement les jours futurs ;
    # - il dédoublonne les matchs déjà présents ;
    # - il conserve les résultats déjà réglés win/loss ;
    # - en cas d'erreur historique, /daily reste utilisable.
    try:
        import premium_history
        history_record = premium_history.record_result_json(result, target_day=target_day)
        result["daily"]["historyRecord"] = history_record
    except Exception as exc:
        result["daily"]["historyRecord"] = {
            "status": "error",
            "message": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
        }

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
            "/audit?day=today",
            "/debug-audit?day=today",
            "/history",
            "/history-refresh",
            "/history-reset?confirm=RESET",
            "/update-2026-history?force=true",
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
            "oddsSource": "Flashscore",
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
        update2026 = await asyncio.to_thread(run_update_2026_history_if_needed)
        result = calculate_from_matches(matches)
        return _attach_update_2026(result, update2026)
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
        update2026 = await asyncio.to_thread(run_update_2026_history_if_needed)
        result = calculate_from_matches(matches)
        return _attach_update_2026(result, update2026)
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

    update2026 = await asyncio.to_thread(run_update_2026_history_if_needed)
    result = await asyncio.to_thread(run_daily_fetch_sync, target_day)
    return _attach_update_2026(result, update2026)


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

    update2026 = await asyncio.to_thread(run_update_2026_history_if_needed)
    result = await asyncio.to_thread(run_daily_fetch_sync, target_day)
    return _attach_update_2026(result, update2026)


@app.get("/update-2026-history")
async def update_2026_history_manual(force: bool = Query(True)) -> Dict[str, Any]:
    """
    Endpoint manuel de contrôle.
    force=true relance même si la mise à jour a déjà réussi aujourd'hui.
    """
    return await asyncio.to_thread(run_update_2026_history_if_needed, force)


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


@app.get("/debug-audit")
async def debug_audit(day: str = Query("today")) -> Dict[str, Any]:
    """
    Alias sécurisé de /audit.

    Objectif :
    - lire le même fichier audit que /audit ;
    - ne pas relancer l'analyse ;
    - ne pas modifier l'historique ;
    - ne pas toucher au moteur.
    """
    return await audit(day=day)


@app.get("/history")
async def history() -> Dict[str, Any]:
    """
    Historique Premium Tennis Motor.

    Ne relance pas /daily.
    Ne touche pas au moteur.
    Lit premium_history.py et renvoie le résumé, le graphique et les lignes historiques.
    """
    try:
        import premium_history
        return await asyncio.to_thread(premium_history.build_summary)
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-4000:],
        }


@app.get("/history-refresh")
async def history_refresh() -> Dict[str, Any]:
    """
    Alias de sécurité pour reconstruire/lire le résumé historique.

    Même comportement que /history :
    - pas de relance daily ;
    - pas de modification moteur ;
    - lecture via premium_history.build_summary().
    """
    try:
        import premium_history
        return await asyncio.to_thread(premium_history.build_summary)
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-4000:],
        }


@app.get("/history-reset")
async def history_reset(confirm: str = Query("")) -> Dict[str, Any]:
    """
    Remise à zéro de l'historique Premium.

    Sécurité :
    - ne relance pas /daily ;
    - ne touche pas au moteur ;
    - ne touche pas aux fichiers payload/audit/résultats du jour ;
    - supprime seulement premium_history.json et premium_history_summary.json ;
    - nécessite confirm=RESET.
    """
    if confirm != "RESET":
        return {
            "status": "refused",
            "message": "Pour remettre l'historique à zéro, appelle /history-reset?confirm=RESET",
        }

    deleted: List[str] = []
    missing: List[str] = []
    errors: List[str] = []

    candidate_dirs: List[Path] = []

    try:
        candidate_dirs.append(OUTPUT_DIR)
    except Exception:
        pass

    try:
        volume_dir = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        if volume_dir:
            candidate_dirs.append(Path(volume_dir))
            candidate_dirs.append(Path(volume_dir) / "output")
    except Exception:
        pass

    # Dédoublonnage des dossiers.
    unique_dirs: List[Path] = []
    for d in candidate_dirs:
        try:
            resolved = d.resolve()
        except Exception:
            resolved = d
        if resolved not in unique_dirs:
            unique_dirs.append(resolved)

    filenames = [
        "premium_history.json",
        "premium_history_summary.json",
    ]

    for directory in unique_dirs:
        for filename in filenames:
            path = directory / filename
            try:
                if path.exists():
                    path.unlink()
                    deleted.append(str(path))
                else:
                    missing.append(str(path))
            except Exception as exc:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")

    # Reconstruire un résumé vide si premium_history.py le permet.
    rebuilt: Dict[str, Any] = {}
    try:
        import premium_history
        rebuilt = await asyncio.to_thread(premium_history.build_summary)
    except Exception as exc:
        rebuilt = {
            "status": "warning",
            "message": f"Historique supprimé, mais résumé non reconstruit automatiquement : {type(exc).__name__}: {exc}",
        }

    return {
        "status": "ok" if not errors else "partial",
        "message": "Historique Premium remis à zéro.",
        "deleted": deleted,
        "missing": missing,
        "errors": errors,
        "history": rebuilt,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
