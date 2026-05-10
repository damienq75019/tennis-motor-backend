#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
premium_history.py

Historique séparé pour Tennis Motor.

Objectif :
- Ne pas modifier le moteur.
- Ne pas modifier les probabilités.
- Suivre uniquement les matchs PREMIUM jouables.
- Sauvegarder cote, statut, résultat réel, win/loss, profit et ROI.
- Fournir des stats 1 jour / 7 jours / 30 jours / 365 jours / total.
- Fournir des données de graphique par jour.

Utilisation locale :
    py premium_history.py record-url "https://web-production-22524.up.railway.app/daily?day=today"
    py premium_history.py record-json output/result_2026-05-10.json
    py premium_history.py settle-flashscore
    py premium_history.py stats
    py premium_history.py reset --confirm YES

Fichiers :
    output/premium_history.json
    output/premium_history_summary.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen


OUTPUT_DIR = Path("output")
HISTORY_PATH = OUTPUT_DIR / "premium_history.json"
SUMMARY_PATH = OUTPUT_DIR / "premium_history_summary.json"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Base utils
# ---------------------------------------------------------------------------

def today_iso() -> str:
    return date.today().isoformat()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def norm_name(name: str) -> str:
    value = (name or "").lower()
    value = value.replace("é", "e").replace("è", "e").replace("ê", "e")
    value = value.replace("á", "a").replace("à", "a").replace("â", "a")
    value = value.replace("í", "i").replace("ï", "i")
    value = value.replace("ó", "o").replace("ö", "o")
    value = value.replace("ú", "u").replace("ü", "u")
    value = value.replace("ñ", "n").replace("ç", "c")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return normalize_space(value)


def same_player(a: str, b: str) -> bool:
    """
    Matching simple :
    - nom complet identique normalisé
    - initiale + nom : "Zverev A." ~= "Alexander Zverev"
    - nom de famille fort
    """
    na = norm_name(a)
    nb = norm_name(b)

    if not na or not nb:
        return False

    if na == nb:
        return True

    pa = na.split()
    pb = nb.split()

    if not pa or not pb:
        return False

    # Même dernier nom long.
    if len(pa[-1]) >= 4 and pa[-1] == pb[-1]:
        return True

    # Flashscore peut afficher "Zverev A." ou "A. Zverev".
    def initial_last(parts: List[str]) -> Tuple[str, str]:
        if len(parts) < 2:
            return "", ""
        if len(parts[0]) == 1:
            return parts[0], parts[-1]
        if len(parts[-1]) == 1:
            return parts[-1], parts[0]
        return parts[0][0], parts[-1]

    ia, la = initial_last(pa)
    ib, lb = initial_last(pb)

    return bool(ia and ib and ia == ib and la == lb)


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def premium_pct(match: Dict[str, Any]) -> float:
    if "premiumPct" in match:
        return to_float(match.get("premiumPct"), 0.0)

    if "premium" in match:
        v = to_float(match.get("premium"), 0.0)
        return v * 100.0 if v <= 1.0 else v

    return 0.0


def is_veto(match: Dict[str, Any]) -> bool:
    return str(match.get("veto", "")).strip().lower() in {"oui", "yes", "true", "1"}


def is_premium_jouable(match: Dict[str, Any]) -> bool:
    if is_veto(match):
        return False

    if premium_pct(match) < 80.0:
        return False

    decision = str(match.get("decision", "")).lower()

    if "pas jouable" in decision or "refus" in decision:
        return False

    # Accepte "✅ Jouable", "Jouable", etc.
    return True


def match_id(target_day: str, source_a: str, source_b: str, predicted: str) -> str:
    pair = sorted([norm_name(source_a), norm_name(source_b)])
    return f"{target_day}__{pair[0]}__{pair[1]}__pick_{norm_name(predicted)}"


# ---------------------------------------------------------------------------
# JSON read/write
# ---------------------------------------------------------------------------

def load_history() -> List[Dict[str, Any]]:
    try:
        if not HISTORY_PATH.exists():
            return []
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        pass
    return []


def save_history(rows: List[Dict[str, Any]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda x: (str(x.get("date", "")), str(x.get("predictedWinner", ""))), reverse=True)
    HISTORY_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json_file(path: str) -> Dict[str, Any]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def read_json_url(url: str, timeout: int = 180) -> Dict[str, Any]:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 TennisMotorHistory/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Record Premium picks
# ---------------------------------------------------------------------------

def infer_target_day(result: Dict[str, Any], fallback: Optional[str] = None) -> str:
    for key_path in [
        ("daily", "targetDay"),
        ("meta", "targetDay"),
        ("targetDay",),
    ]:
        cur: Any = result
        ok = True
        for k in key_path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and cur:
            return str(cur)

    return fallback or today_iso()


def record_result_json(result: Dict[str, Any], target_day: Optional[str] = None) -> Dict[str, Any]:
    day = infer_target_day(result, target_day)
    matches = result.get("matches") if isinstance(result, dict) else None

    if not isinstance(matches, list):
        return {
            "status": "error",
            "message": "JSON sans champ matches[]",
            "added": 0,
            "updated": 0,
        }

    history = load_history()
    by_id = {str(row.get("id", "")): row for row in history if row.get("id")}

    added = 0
    updated = 0
    ignored = 0

    for match in matches:
        if not isinstance(match, dict):
            ignored += 1
            continue

        if not is_premium_jouable(match):
            ignored += 1
            continue

        predicted = str(match.get("playerA") or match.get("player_a") or "")
        opponent = str(match.get("playerB") or match.get("player_b") or "")

        source_a = str(match.get("sourcePlayerA") or predicted)
        source_b = str(match.get("sourcePlayerB") or opponent)

        if not predicted or not opponent:
            ignored += 1
            continue

        row_id = match_id(day, source_a, source_b, predicted)
        old = by_id.get(row_id, {})

        row = {
            "id": row_id,
            "date": day,
            "sourcePlayerA": source_a,
            "sourcePlayerB": source_b,
            "predictedWinner": predicted,
            "opponent": opponent,
            "surface": str(match.get("surface") or ""),
            "premiumPct": round(premium_pct(match), 3),
            "status": "PREMIUM",
            "veto": str(match.get("veto") or "non"),
            "decision": str(match.get("decision") or ""),
            "oddPredicted": str(match.get("oddA") or match.get("playerAOdd") or match.get("coteA") or ""),
            "oddOpponent": str(match.get("oddB") or match.get("playerBOdd") or match.get("coteB") or ""),
            "oddsSource": str(match.get("oddsSource") or ""),
            "result": old.get("result", "pending"),
            "realWinner": old.get("realWinner", ""),
            "settledAt": old.get("settledAt", ""),
        }

        if row_id in by_id:
            # Ne jamais écraser un résultat déjà validé.
            if by_id[row_id].get("result") in {"win", "loss"}:
                row["result"] = by_id[row_id].get("result")
                row["realWinner"] = by_id[row_id].get("realWinner", "")
                row["settledAt"] = by_id[row_id].get("settledAt", "")
            by_id[row_id] = row
            updated += 1
        else:
            by_id[row_id] = row
            added += 1

    save_history(list(by_id.values()))
    summary = build_summary()
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "date": day,
        "added": added,
        "updated": updated,
        "ignoredNonPremium": ignored,
        "historyPath": str(HISTORY_PATH),
        "summaryPath": str(SUMMARY_PATH),
    }


# ---------------------------------------------------------------------------
# Manual settle
# ---------------------------------------------------------------------------

def settle_manual(predicted_or_pair: str, winner: str, target_day: Optional[str] = None) -> Dict[str, Any]:
    """
    Règle un résultat à la main.

    Exemple :
        py premium_history.py settle-manual --pick "Alexander Zverev" --winner "Alexander Zverev"
    """
    history = load_history()
    changed = 0

    for row in history:
        if target_day and str(row.get("date")) != target_day:
            continue

        predicted = str(row.get("predictedWinner") or "")
        source_a = str(row.get("sourcePlayerA") or "")
        source_b = str(row.get("sourcePlayerB") or "")

        query = predicted_or_pair

        match_pick = same_player(query, predicted)
        match_pair = same_player(query, source_a) or same_player(query, source_b)

        if not (match_pick or match_pair):
            continue

        row["realWinner"] = winner
        row["result"] = "win" if same_player(predicted, winner) else "loss"
        row["settledAt"] = today_iso()
        changed += 1

    save_history(history)
    summary = build_summary()
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "changed": changed,
        "summaryPath": str(SUMMARY_PATH),
    }


# ---------------------------------------------------------------------------
# Optional Flashscore settle by importing app.py helpers
# ---------------------------------------------------------------------------

def parse_scores_from_flashscore_raw(row: Dict[str, str]) -> Dict[str, Any]:
    player_a = str(row.get("playerA", ""))
    player_b = str(row.get("playerB", ""))
    raw = str(row.get("raw", ""))

    if not player_a or not player_b or not raw:
        return {}

    parts = [p.strip() for p in re.split(r"\s*\|\s*", raw) if p.strip()]
    if len(parts) < 4:
        return {}

    idx_a = -1
    idx_b = -1

    for i, part in enumerate(parts):
        if idx_a < 0 and same_player(player_a, part):
            idx_a = i
        if idx_b < 0 and same_player(player_b, part):
            idx_b = i

    if idx_a < 0 or idx_b < 0 or idx_b <= idx_a:
        return {}

    def collect_ints(start: int, end: int) -> List[int]:
        nums: List[int] = []
        for part in parts[start:end]:
            if re.fullmatch(r"\d+", part):
                nums.append(int(part))
        return nums

    a_nums = collect_ints(idx_a + 1, idx_b)
    b_nums = collect_ints(idx_b + 1, min(len(parts), idx_b + 6))

    if not a_nums or not b_nums:
        return {}

    # Sur Flashscore, le premier chiffre après le joueur est souvent le nombre de sets gagnés.
    a_sets = a_nums[0]
    b_sets = b_nums[0]

    if a_sets == b_sets:
        return {}

    return {
        "winner": player_a if a_sets > b_sets else player_b,
        "scoreA": a_nums,
        "scoreB": b_nums,
    }


def settle_flashscore() -> Dict[str, Any]:
    """
    Essaie de régler automatiquement les pending via les fonctions Flashscore déjà présentes dans app.py.

    Avantage :
    - app.py reste le seul endroit qui connaît Flashscore.
    - premium_history.py reste un script séparé.
    """
    history = load_history()
    pending = [row for row in history if row.get("result") == "pending"]

    if not pending:
        return {"status": "ok", "pendingBefore": 0, "settled": 0, "message": "Aucun Premium en attente."}

    try:
        import app  # type: ignore

        flash_rows, flash_audit = app.fetch_flashscore_tennis_odds()
    except Exception as exc:
        return {
            "status": "error",
            "pendingBefore": len(pending),
            "settled": 0,
            "error": f"Impossible d'utiliser Flashscore via app.py : {type(exc).__name__}: {exc}",
        }

    settled = 0

    for row in history:
        if row.get("result") != "pending":
            continue

        source_a = str(row.get("sourcePlayerA") or "")
        source_b = str(row.get("sourcePlayerB") or "")
        predicted = str(row.get("predictedWinner") or "")

        real_winner = ""

        for fs in flash_rows:
            fa = str(fs.get("playerA") or "")
            fb = str(fs.get("playerB") or "")

            same_order = same_player(source_a, fa) and same_player(source_b, fb)
            reversed_order = same_player(source_a, fb) and same_player(source_b, fa)

            if not same_order and not reversed_order:
                continue

            parsed = parse_scores_from_flashscore_raw(fs)
            winner = str(parsed.get("winner") or "")

            if not winner:
                continue

            if same_order:
                real_winner = winner
            elif same_player(winner, fa):
                real_winner = source_b
            elif same_player(winner, fb):
                real_winner = source_a

            break

        if not real_winner:
            continue

        row["realWinner"] = real_winner
        row["result"] = "win" if same_player(predicted, real_winner) else "loss"
        row["settledAt"] = today_iso()
        settled += 1

    save_history(history)
    summary = build_summary()
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "pendingBefore": len(pending),
        "settled": settled,
        "flashscoreRows": len(flash_rows),
        "summaryPath": str(SUMMARY_PATH),
    }


# ---------------------------------------------------------------------------
# Stats / chart
# ---------------------------------------------------------------------------

def stats_for_period(rows: List[Dict[str, Any]], period_days: Optional[int]) -> Dict[str, Any]:
    today = date.today()
    selected: List[Dict[str, Any]] = []

    for row in rows:
        if row.get("status") != "PREMIUM":
            continue

        try:
            d = date.fromisoformat(str(row.get("date", "")))
        except Exception:
            continue

        if period_days is not None and d < today - timedelta(days=period_days - 1):
            continue

        selected.append(row)

    settled = [x for x in selected if x.get("result") in {"win", "loss"}]
    wins = sum(1 for x in settled if x.get("result") == "win")
    losses = sum(1 for x in settled if x.get("result") == "loss")
    pending = sum(1 for x in selected if x.get("result") == "pending")

    total = wins + losses
    win_rate = round((wins / total) * 100.0, 2) if total else 0.0

    profit = 0.0
    odds_used = 0

    for row in settled:
        odd = to_float(row.get("oddPredicted"), 0.0)
        if odd <= 1.0:
            continue

        odds_used += 1

        if row.get("result") == "win":
            profit += odd - 1.0
        else:
            profit -= 1.0

    roi = round((profit / odds_used) * 100.0, 2) if odds_used else 0.0

    return {
        "trackedPremium": len(selected),
        "settled": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "winRate": win_rate,
        "profitUnits": round(profit, 3),
        "roiPct": roi,
        "oddsUsed": odds_used,
    }


def build_summary() -> Dict[str, Any]:
    rows = load_history()

    by_day: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        if row.get("status") != "PREMIUM":
            continue

        d = str(row.get("date") or "")
        if not d:
            continue

        bucket = by_day.setdefault(
            d,
            {
                "date": d,
                "wins": 0,
                "losses": 0,
                "pending": 0,
                "winRate": 0.0,
                "profitUnits": 0.0,
            },
        )

        odd = to_float(row.get("oddPredicted"), 0.0)
        result = row.get("result")

        if result == "win":
            bucket["wins"] += 1
            if odd > 1.0:
                bucket["profitUnits"] += odd - 1.0
        elif result == "loss":
            bucket["losses"] += 1
            if odd > 1.0:
                bucket["profitUnits"] -= 1.0
        else:
            bucket["pending"] += 1

    days = []
    for bucket in by_day.values():
        settled = bucket["wins"] + bucket["losses"]
        bucket["winRate"] = round((bucket["wins"] / settled) * 100.0, 2) if settled else 0.0
        bucket["profitUnits"] = round(bucket["profitUnits"], 3)
        days.append(bucket)

    days.sort(key=lambda x: x["date"])

    return {
        "status": "ok",
        "historyPath": str(HISTORY_PATH),
        "summaryPath": str(SUMMARY_PATH),
        "summary": {
            "day": stats_for_period(rows, 1),
            "week": stats_for_period(rows, 7),
            "month": stats_for_period(rows, 30),
            "year": stats_for_period(rows, 365),
            "all": stats_for_period(rows, None),
        },
        "chart": {
            "days": days,
            "description": "Données pour graphique : winRate + profitUnits par jour.",
        },
        "rows": rows,
    }


def reset_history(confirm: str) -> Dict[str, Any]:
    if confirm != "YES":
        return {
            "status": "refused",
            "message": "Pour confirmer : py premium_history.py reset --confirm YES",
        }

    save_history([])
    SUMMARY_PATH.write_text(json.dumps(build_summary(), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "message": "Historique effacé."}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Historique Premium Tennis Motor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("record-json", help="Enregistre les Premium depuis un result_YYYY-MM-DD.json")
    p.add_argument("path")
    p.add_argument("--date", default="")

    p = sub.add_parser("record-url", help="Appelle une URL /daily?day=... puis enregistre les Premium")
    p.add_argument("url")
    p.add_argument("--date", default="")

    p = sub.add_parser("settle-flashscore", help="Met à jour les pending via Flashscore/app.py")
    p = sub.add_parser("stats", help="Écrit et affiche premium_history_summary.json")

    p = sub.add_parser("settle-manual", help="Valide un résultat à la main")
    p.add_argument("--pick", required=True, help="Nom du joueur prédit ou un joueur de la paire")
    p.add_argument("--winner", required=True, help="Vrai gagnant")
    p.add_argument("--date", default="")

    p = sub.add_parser("reset", help="Efface l'historique")
    p.add_argument("--confirm", default="")

    args = parser.parse_args()

    if args.cmd == "record-json":
        result = read_json_file(args.path)
        out = record_result_json(result, args.date or None)

    elif args.cmd == "record-url":
        result = read_json_url(args.url)
        out = record_result_json(result, args.date or None)

    elif args.cmd == "settle-flashscore":
        out = settle_flashscore()

    elif args.cmd == "settle-manual":
        out = settle_manual(args.pick, args.winner, args.date or None)

    elif args.cmd == "stats":
        out = build_summary()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        SUMMARY_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    elif args.cmd == "reset":
        out = reset_history(args.confirm)

    else:
        out = {"status": "error", "message": "Commande inconnue."}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
