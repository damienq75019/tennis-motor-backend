#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - Cron Results 2026 Yesterday

Rôle :
- Appeler automatiquement le backend Tennis Motor chaque jour.
- Synchroniser les résultats ATP hommes simples terminés de la veille.
- Laisser le backend écrire dans PostgreSQL et reconstruire data/2026.csv.

Variables optionnelles :
- TENNIS_MOTOR_BASE_URL : URL publique du backend.
  Défaut : https://web-production-22524.up.railway.app
- CRON_SYNC_PATH : chemin endpoint.
  Défaut : /sync/results2026/run?day=yesterday
- CRON_TIMEOUT_SECONDS : timeout HTTP.
  Défaut : 900
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "https://web-production-22524.up.railway.app"
DEFAULT_PATH = "/sync/results2026/run?day=yesterday"


def build_url() -> str:
    base_url = os.environ.get("TENNIS_MOTOR_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    path = os.environ.get("CRON_SYNC_PATH", DEFAULT_PATH).strip()

    if not path.startswith("/"):
        path = "/" + path

    return base_url + path


def main() -> int:
    url = build_url()
    timeout = int(os.environ.get("CRON_TIMEOUT_SECONDS", "900"))

    print("=== Tennis Motor Cron Results 2026 ===")
    print(f"URL cible : {url}")
    print(f"Timeout : {timeout}s")
    print(f"Timestamp : {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "TennisMotorCron/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.getcode()
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        print(f"ERREUR HTTP : {exc.code}")
        print(raw[:4000])
        return 2
    except Exception as exc:
        print(f"ERREUR APPEL BACKEND : {type(exc).__name__}: {exc}")
        return 3

    print(f"HTTP status : {status_code}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("ERREUR : réponse backend non JSON.")
        print(raw[:4000])
        return 4

    print(json.dumps(payload, ensure_ascii=False, indent=2)[:12000])

    if status_code < 200 or status_code >= 300:
        print("ERREUR : status HTTP non OK.")
        return 5

    if payload.get("status") != "ok":
        print("ERREUR : payload status != ok.")
        return 6

    errors = payload.get("errors") or []
    if errors:
        print("ERREUR : le backend a retourné des erreurs.")
        return 7

    counts = payload.get("counts") or {}
    storage = payload.get("storage") or {}

    print("=== Résumé cron ===")
    print(f"targetDay : {payload.get('targetDay')}")
    print(f"rows_added : {counts.get('rows_added')}")
    print(f"skipped_duplicate : {counts.get('skipped_duplicate')}")
    print(f"skipped_not_finished : {counts.get('skipped_not_finished')}")
    print(f"skipped_missing_score : {counts.get('skipped_missing_score')}")
    print(f"storage mode : {storage.get('mode')}")
    print("Cron terminé correctement.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
