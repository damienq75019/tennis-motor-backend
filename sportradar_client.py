from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


class SportradarError(RuntimeError):
    pass


@dataclass(frozen=True)
class SportradarConfig:
    api_key: str
    access_level: str = "trial"
    language: str = "en"
    timeout_seconds: int = 45
    min_interval_seconds: float = 1.05


class SportradarClient:
    """Client minimal et strict pour Sportradar Tennis API v3.

    La clé API reste exclusivement côté backend via SPORTRADAR_API_KEY.
    """

    def __init__(self, config: Optional[SportradarConfig] = None) -> None:
        if config is None:
            api_key = os.environ.get("SPORTRADAR_API_KEY", "").strip()
            access_level = os.environ.get("SPORTRADAR_ACCESS_LEVEL", "trial").strip() or "trial"
            language = os.environ.get("SPORTRADAR_LANGUAGE", "en").strip() or "en"
            config = SportradarConfig(api_key=api_key, access_level=access_level, language=language)

        self.config = config
        self._last_call_monotonic = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key)

    @property
    def base_url(self) -> str:
        return f"https://api.sportradar.com/tennis/{self.config.access_level}/v3/{self.config.language}"

    def _wait_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call_monotonic
        wait = self.config.min_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)

    def get(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.enabled:
            raise SportradarError("SPORTRADAR_API_KEY absente côté backend.")

        if not path.startswith("/"):
            path = "/" + path

        self._wait_rate_limit()
        url = self.base_url + path

        try:
            response = requests.get(
                url,
                params=params,
                headers={
                    "accept": "application/json",
                    "x-api-key": self.config.api_key,
                    "User-Agent": "TennisMotorBackendClean/step2",
                },
                timeout=self.config.timeout_seconds,
            )
            self._last_call_monotonic = time.monotonic()
        except requests.RequestException as exc:
            raise SportradarError(f"Erreur réseau Sportradar: {exc}") from exc

        if response.status_code == 401:
            raise SportradarError("Sportradar 401: clé API absente, invalide ou expirée.")
        if response.status_code == 403:
            raise SportradarError("Sportradar 403: accès refusé pour ce flux ou trial insuffisant.")
        if response.status_code == 429:
            raise SportradarError("Sportradar 429: quota/rate limit dépassé.")
        if response.status_code < 200 or response.status_code >= 300:
            body = response.text[:1200]
            raise SportradarError(f"Sportradar HTTP {response.status_code}: {body}")

        try:
            return response.json()
        except ValueError as exc:
            raise SportradarError("Réponse Sportradar non JSON.") from exc

    def rankings(self) -> Dict[str, Any]:
        return self.get("/rankings.json")

    def daily_summaries(self, target_day: str) -> Dict[str, Any]:
        return self.get(f"/schedules/{target_day}/summaries.json")

    def season_info(self, season_id: str) -> Dict[str, Any]:
        return self.get(f"/seasons/{season_id}/info.json")

    def season_summaries(self, season_id: str, *, limit: int = 200) -> Dict[str, Any]:
        return self.get(f"/seasons/{season_id}/summaries.json", params={"limit": limit})
