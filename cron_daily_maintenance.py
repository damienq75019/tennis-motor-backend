#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - Cron Daily Maintenance STEP46

Rôle quotidien unique :
- construire /daily aujourd'hui via API-Tennis ;
- enregistrer les historiques PREMIUM / PROCHE / VETO / REFUSE ;
- régler les pending via API-Tennis ;
- synchroniser les résultats 2026 de la veille ;
- nettoyer les doublons.

Variables optionnelles :
- TENNIS_MOTOR_BASE_URL : URL publique du backend.
  Défaut : https://web-production-22524.up.railway.app
- CRON_DAILY_MAINTENANCE_PATH : chemin endpoint.
  Défaut : /sync/daily-maintenance/run?day=today&sync_results_day=yesterday&settle_days_back=0&dry_run=false
- CRON_TIMEOUT_SECONDS : timeout HTTP.
  Défaut : 900
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://web-production-22524.up.railway.app"
DEFAULT_PATH = "/sync/daily-maintenance/run?day=today&sync_results_day=yesterday&settle_days_back=0&dry_run=false"


def build_url() -> str:
    base_url = os.environ.get("TENNIS_MOTOR_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    path = os.environ.get("CRON_DAILY_MAINTENANCE_PATH", DEFAULT_PATH).strip()
    if not path.startswith("/"):
        path = "/" + path
    return base_url + path


def main() -> int:
    url = build_url()
    timeout = int(os.environ.get("CRON_TIMEOUT_SECONDS", "900"))
    print("=== Tennis Motor Cron Daily Maintenance STEP46 ===")
    print(f"URL cible : {url}")
    print(f"Timeout : {timeout}s")
    print(f"Timestamp : {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "TennisMotorDailyMaintenanceCron/1.0", "Accept": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.getcode()
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        print(f"ERREUR HTTP : {exc.code}")
        print(raw[:8000])
        return 2
    except Exception as exc:
        print(f"ERREUR APPEL BACKEND : {type(exc).__name__}: {exc}")
        return 3

    print(f"HTTP status : {status_code}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("ERREUR : réponse backend non JSON.")
        print(raw[:8000])
        return 4

    print(json.dumps(payload, ensure_ascii=False, indent=2)[:16000])

    if not (200 <= status_code < 300):
        print("ERREUR : status HTTP non OK.")
        return 5
    if payload.get("status") not in {"ok", "partial"}:
        print("ERREUR : payload status invalide.")
        return 6
    if payload.get("errors"):
        print("ATTENTION : erreurs backend détectées.")
        return 7

    print("Cron Daily Maintenance terminé correctement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
