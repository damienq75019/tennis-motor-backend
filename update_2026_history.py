#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tennis Motor - Update 2026 history from Jeff Sackmann ATP CSV

Objectif :
- Télécharger l'historique ATP 2026 depuis le dépôt Jeff Sackmann.
- Retirer tous les matchs avec Jannik Sinner.
- Écrire le fichier final dans data/2026.csv.
- Faire une sauvegarde automatique de l'ancien data/2026.csv si présent.

Important :
- Le fichier Jeff Sackmann contient des résultats terminés, pas les matchs du jour non terminés.
- On n'injecte donc pas les analyses du jour dans l'Elo avant résultat connu.
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

RAW_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2026.csv"
ALT_RAW_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/refs/heads/master/atp_matches_2026.csv"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_PATH = DATA_DIR / "2026.csv"
RAW_COPY_PATH = DATA_DIR / "2026_raw_jeff_sackmann.csv"
TODAY_INT = int(datetime.now().strftime("%Y%m%d"))
TARGET_NAME = "jannik sinner"


def canonical_name(name: str) -> str:
    value = (name or "").strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = " ".join(value.split())
    return value


def download_file(urls: list[str], destination: Path) -> str:
    last_error: Exception | None = None
    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "TennisMotor/1.0 (+local updater)",
                    "Accept": "text/csv,text/plain,*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                content = response.read()

            if len(content) < 1000:
                raise RuntimeError(f"Téléchargement trop petit: {len(content)} octets")

            destination.write_bytes(content)
            return url
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    raise RuntimeError(f"Impossible de télécharger le CSV 2026. Dernière erreur: {last_error}")


def safe_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        temp_path = Path(tmp.name)

    try:
        used_url = download_file([RAW_URL, ALT_RAW_URL], temp_path)
        shutil.copyfile(temp_path, RAW_COPY_PATH)

        if OUTPUT_PATH.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = DATA_DIR / f"2026_backup_before_update_{stamp}.csv"
            shutil.copyfile(OUTPUT_PATH, backup_path)
        else:
            backup_path = None

        total_rows = 0
        kept_rows = 0
        removed_sinner = 0
        removed_future = 0
        max_tourney_date = 0
        remaining_sinner = 0

        with temp_path.open("r", encoding="utf-8", newline="") as src, OUTPUT_PATH.open(
            "w", encoding="utf-8", newline=""
        ) as dst:
            reader = csv.DictReader(src)
            if not reader.fieldnames:
                raise RuntimeError("CSV source sans en-têtes.")

            writer = csv.DictWriter(dst, fieldnames=reader.fieldnames)
            writer.writeheader()

            for row in reader:
                total_rows += 1
                winner = canonical_name(row.get("winner_name", ""))
                loser = canonical_name(row.get("loser_name", ""))
                tourney_date = safe_int(row.get("tourney_date", "0"))
                if tourney_date > max_tourney_date:
                    max_tourney_date = tourney_date

                if tourney_date > TODAY_INT:
                    removed_future += 1
                    continue

                if winner == TARGET_NAME or loser == TARGET_NAME:
                    removed_sinner += 1
                    continue

                writer.writerow(row)
                kept_rows += 1

        # Vérification finale directe du fichier écrit.
        with OUTPUT_PATH.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                winner = canonical_name(row.get("winner_name", ""))
                loser = canonical_name(row.get("loser_name", ""))
                if winner == TARGET_NAME or loser == TARGET_NAME:
                    remaining_sinner += 1

        print("✅ Mise à jour 2026 terminée")
        print(f"source={used_url}")
        print(f"today_filter={TODAY_INT}")
        print(f"raw_copy={RAW_COPY_PATH}")
        print(f"output={OUTPUT_PATH}")
        if backup_path:
            print(f"backup={backup_path}")
        print(f"total_rows_source={total_rows}")
        print(f"removed_future_rows={removed_future}")
        print(f"removed_jannik_sinner_rows={removed_sinner}")
        print(f"kept_rows_final={kept_rows}")
        print(f"max_tourney_date_seen={max_tourney_date}")
        print(f"jannik_sinner_remaining={remaining_sinner}")

        if remaining_sinner != 0:
            print("❌ ERREUR: Jannik Sinner est encore présent dans data/2026.csv")
            return 2

        return 0

    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
