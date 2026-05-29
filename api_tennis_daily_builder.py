from __future__ import annotations

import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests


ATP_SINGLES_EVENT_TYPE_KEY = "265"


@dataclass
class ApiTennisConfig:
    api_key: str
    base_url: str = "https://api.api-tennis.com/tennis/"
    timeout: int = 30
    timezone: str = "Europe/Paris"
    min_delay_seconds: float = 1.05


class ApiTennisDailyBuilder:
    """Daily ATP singles provider for API-Tennis.com.

    The builder intentionally performs only two API calls for a normal daily payload:
    1) get_standings&event_type=ATP for ATP points/rank
    2) get_fixtures&event_type_key=265 for ATP singles fixtures/results of the target day

    Output fields mimic the previous provider-oriented payload so the rest of Tennis Motor
    (motor, PostgreSQL history, Unity models) can stay stable.
    """

    def __init__(self, audit_dir: Optional[Any] = None, config: Optional[ApiTennisConfig] = None) -> None:
        api_key = os.environ.get("API_TENNIS_KEY", "").strip()
        base_url = os.environ.get("API_TENNIS_BASE_URL", "https://api.api-tennis.com/tennis/").strip()
        timezone = os.environ.get("API_TENNIS_TIMEZONE", "Europe/Paris").strip() or "Europe/Paris"
        self.config = config or ApiTennisConfig(api_key=api_key, base_url=base_url, timezone=timezone)
        self.audit_dir = audit_dir
        self._last_call_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key)

    def _sleep_if_needed(self) -> None:
        delay = max(0.0, float(self.config.min_delay_seconds or 0.0))
        if delay <= 0.0:
            return
        now = time.monotonic()
        elapsed = now - self._last_call_at
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            return {"success": 0, "error": "API_TENNIS_KEY absente dans Railway."}
        clean_params = {k: v for k, v in params.items() if v is not None and str(v) != ""}
        clean_params["APIkey"] = self.config.api_key
        self._sleep_if_needed()
        started = time.monotonic()
        try:
            resp = requests.get(self.config.base_url, params=clean_params, timeout=self.config.timeout)
            self._last_call_at = time.monotonic()
            try:
                data = resp.json()
            except Exception:
                data = {"success": 0, "error": resp.text[:500]}
            data.setdefault("httpStatus", resp.status_code)
            data.setdefault("elapsedMs", int((time.monotonic() - started) * 1000))
            if resp.status_code >= 400:
                data["success"] = 0
                data.setdefault("error", f"HTTP {resp.status_code}")
            return data
        except Exception as exc:
            self._last_call_at = time.monotonic()
            return {"success": 0, "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _norm(value: Any) -> str:
        text = str(value or "")
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = text.lower()
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _initial_last_key(value: Any) -> str:
        norm = ApiTennisDailyBuilder._norm(value)
        parts = [p for p in norm.split() if p]
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[-1]
        return f"{parts[0][0]} {parts[-1]}"

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            if value is None:
                return default
            return int(float(str(value).replace(",", ".").strip()))
        except Exception:
            return default

    @staticmethod
    def _safe_str(value: Any) -> str:
        return str(value or "").strip()

    def _standings(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]]:
        payload = self._request({"method": "get_standings", "event_type": "ATP"})
        result = payload.get("result") if isinstance(payload, dict) else []
        by_key: Dict[str, Dict[str, Any]] = {}
        by_name: Dict[str, Dict[str, Any]] = {}
        by_initial_last: Dict[str, Dict[str, Any]] = {}

        if isinstance(result, list):
            for row in result:
                if not isinstance(row, dict):
                    continue
                player_key = self._safe_str(row.get("player_key"))
                player = self._safe_str(row.get("player"))
                entry = {
                    "player_key": player_key,
                    "player": player,
                    "rank": self._safe_int(row.get("place"), 0),
                    "points": self._safe_int(row.get("points"), 0),
                    "country": self._safe_str(row.get("country")),
                    "league": self._safe_str(row.get("league")),
                }
                if player_key:
                    by_key[player_key] = entry
                norm = self._norm(player)
                if norm:
                    by_name[norm] = entry
                short = self._initial_last_key(player)
                if short:
                    by_initial_last[short] = entry

        audit = {
            "status": "ok" if payload.get("success") == 1 else "error",
            "httpStatus": payload.get("httpStatus"),
            "elapsedMs": payload.get("elapsedMs"),
            "records": len(by_key),
            "error": payload.get("error") or payload.get("message"),
        }
        # Merge short-name index into by_name with a namespaced key.
        for key, value in by_initial_last.items():
            by_name.setdefault(f"short:{key}", value)
        return by_key, by_name, audit

    def _ranking_entry(self, player_key: Any, raw_name: Any, by_key: Dict[str, Dict[str, Any]], by_name: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        key = self._safe_str(player_key)
        if key and key in by_key:
            return by_key[key]
        norm = self._norm(raw_name)
        if norm and norm in by_name:
            return by_name[norm]
        short = self._initial_last_key(raw_name)
        if short and f"short:{short}" in by_name:
            return by_name[f"short:{short}"]
        return {"player_key": key, "player": self._safe_str(raw_name), "rank": 0, "points": 0, "country": "", "league": "ATP"}

    @staticmethod
    def _is_atp_singles(event: Dict[str, Any]) -> bool:
        event_type_key = str(event.get("event_type_key") or "").strip()
        event_type_type = str(event.get("event_type_type") or "").strip().lower()
        if event_type_key == ATP_SINGLES_EVENT_TYPE_KEY:
            return True
        return event_type_type == "atp singles"

    @staticmethod
    def _infer_surface(event: Dict[str, Any]) -> str:
        for key in ["surface", "event_surface", "tournament_surface", "court_surface"]:
            value = str(event.get(key) or "").strip()
            if value:
                value_l = value.lower()
                if "hard" in value_l or "dur" in value_l:
                    return "Hard"
                if "grass" in value_l or "gazon" in value_l:
                    return "Grass"
                if "clay" in value_l or "terre" in value_l:
                    return "Clay"
                return value[:1].upper() + value[1:]

        blob = " ".join(str(event.get(k) or "") for k in ["tournament_name", "tournament_round", "seasonName"]).lower()
        clay_tokens = [
            "french open", "roland", "garros", "geneva", "hamburg", "rome", "madrid", "monte carlo",
            "barcelona", "munich", "bastad", "gstaad", "kitzbuhel", "clay", "terre",
        ]
        grass_tokens = ["wimbledon", "halle", "queens", "queen's", "stuttgart", "mallorca", "s hertogenbosch", "grass", "gazon"]
        if any(t in blob for t in clay_tokens):
            return "Clay"
        if any(t in blob for t in grass_tokens):
            return "Grass"
        return "Hard"

    @staticmethod
    def _round_wins_before_match(round_value: Any) -> int:
        """Infer main-draw wins already earned before the match from API-Tennis round labels.

        This restores the locked clay safety veto without inventing player-level
        qualifier information. For a normal main draw, both players have the same
        number of wins before a given round:
        - R128 / 1/64-finals / 1st Round => 0
        - R64  / 1/32-finals / 2nd Round => 1
        - R32  / 1/16-finals / 3rd Round => 2
        - R16  / 1/8-finals  / 4th Round => 3
        - Quarter-final => 4, Semi-final => 5, Final => 6

        Qualifying labels are kept at 0 because API-Tennis does not reliably tell
        which player is the qualifier at player level in this provider.
        """
        raw = str(round_value or "").strip()
        if not raw:
            return 0

        text = raw.lower()
        text = text.replace("–", "-").replace("—", "-")
        text = re.sub(r"\s+", " ", text)

        if any(tok in text for tok in ["qualif", "qualification", "qualifying", "pre-qual"]):
            return 0

        # Explicit fractions used frequently by API-Tennis, e.g. 1/64-finals.
        fraction_map = {
            "1/64": 0,
            "1/32": 1,
            "1/16": 2,
            "1/8": 3,
            "1/4": 4,
            "1/2": 5,
        }
        for token, wins in fraction_map.items():
            if token in text:
                return wins

        # Round-of labels.
        compact = re.sub(r"[^a-z0-9]+", "", text)
        if any(tok in compact for tok in ["roundof128", "r128"]):
            return 0
        if any(tok in compact for tok in ["roundof64", "r64"]):
            return 1
        if any(tok in compact for tok in ["roundof32", "r32"]):
            return 2
        if any(tok in compact for tok in ["roundof16", "r16"]):
            return 3

        # Ordinal labels.
        if re.search(r"\b(1st|first) round\b", text):
            return 0
        if re.search(r"\b(2nd|second) round\b", text):
            return 1
        if re.search(r"\b(3rd|third) round\b", text):
            return 2
        if re.search(r"\b(4th|fourth) round\b", text):
            return 3

        # Named final rounds. Order matters: semi-final contains final.
        if any(tok in text for tok in ["semi final", "semi-final", "semifinal", "1/2-finals", "1/2 finals"]):
            return 5
        if any(tok in text for tok in ["quarter final", "quarter-final", "quarterfinal", "1/4-finals", "1/4 finals"]):
            return 4
        if re.search(r"\b(final|finals)\b", text):
            return 6

        return 0

    @staticmethod
    def _winner_id(event: Dict[str, Any]) -> str:
        winner = str(event.get("event_winner") or "").strip().lower()
        first_key = str(event.get("first_player_key") or "").strip()
        second_key = str(event.get("second_player_key") or "").strip()
        if "first" in winner and first_key:
            return f"api_tennis:player:{first_key}"
        if "second" in winner and second_key:
            return f"api_tennis:player:{second_key}"
        return ""

    @staticmethod
    def _event_status(event: Dict[str, Any]) -> Tuple[str, str]:
        raw_status = str(event.get("event_status") or "").strip()
        live = str(event.get("event_live") or "").strip()
        status_l = raw_status.lower()
        if status_l in {"finished", "ended", "complete", "completed"} or ApiTennisDailyBuilder._winner_id(event):
            return "finished", raw_status or "Finished"
        if any(tok in status_l for tok in ["retired", "walkover", "cancelled", "canceled", "abandoned", "withdrawn", "forfeit"]):
            return status_l or "finished", raw_status
        if live == "1" or status_l.startswith("set") or "live" in status_l:
            return "live", raw_status or "live"
        return "not_started", raw_status or "not_started"

    @staticmethod
    def _score(event: Dict[str, Any], winner_id: str) -> str:
        scores = event.get("scores")
        first_key = str(event.get("first_player_key") or "").strip()
        second_key = str(event.get("second_player_key") or "").strip()
        first_id = f"api_tennis:player:{first_key}" if first_key else ""
        second_id = f"api_tennis:player:{second_key}" if second_key else ""
        sets: List[str] = []
        if isinstance(scores, list):
            for s in scores:
                if not isinstance(s, dict):
                    continue
                a = str(s.get("score_first") or "").strip()
                b = str(s.get("score_second") or "").strip()
                if a == "" or b == "":
                    continue
                if winner_id and winner_id == second_id:
                    sets.append(f"{b}-{a}")
                else:
                    sets.append(f"{a}-{b}")
        if sets:
            return " ".join(sets)
        return str(event.get("event_final_result") or "").replace(" - ", "-").strip()

    @staticmethod
    def _bool_text(value: Any) -> bool:
        return str(value or "").strip().lower() in {"true", "1", "yes", "oui"}

    @staticmethod
    def _clean_season_name(event: Dict[str, Any], target_day: str) -> str:
        """Build a stable season label from the actual target date.

        API-Tennis can return a stale tournament_season value on some fixtures
        (for example French Open 2025 while event_date is in 2026).  For Tennis
        Motor, the date of the fixture is the source of truth for the displayed
        season year and for the historical key.
        """
        tournament = ApiTennisDailyBuilder._safe_str(event.get("tournament_name")) or "Tournament"
        raw_season = ApiTennisDailyBuilder._safe_str(event.get("tournament_season"))
        target_year = ""
        try:
            target_year = str(datetime.fromisoformat(str(target_day)).year)
        except Exception:
            m = re.search(r"\b(20\d{2})\b", str(target_day or ""))
            target_year = m.group(1) if m else ""

        if target_year:
            return f"{tournament} {target_year}".strip()
        if raw_season:
            return f"{tournament} {raw_season}".strip()
        return tournament

    def build_matches_for_day(self, target_day: str) -> Dict[str, Any]:
        if not self.enabled:
            return {
                "status": "error",
                "provider": "api_tennis",
                "error": "API_TENNIS_KEY absente dans Railway.",
                "matches": [],
                "audit": {"apiKeyConfigured": False},
            }

        standings_by_key, standings_by_name, standings_audit = self._standings()
        fixtures_payload = self._request({
            "method": "get_fixtures",
            "date_start": target_day,
            "date_stop": target_day,
            "event_type_key": ATP_SINGLES_EVENT_TYPE_KEY,
            "timezone": self.config.timezone,
        })

        if fixtures_payload.get("success") != 1:
            return {
                "status": "error",
                "provider": "api_tennis",
                "error": str(fixtures_payload.get("error") or fixtures_payload.get("message") or "Erreur API-Tennis inconnue."),
                "matches": [],
                "audit": {
                    "apiKeyConfigured": True,
                    "fixturesStatus": "error",
                    "fixturesHttpStatus": fixtures_payload.get("httpStatus"),
                    "fixturesElapsedMs": fixtures_payload.get("elapsedMs"),
                    "standings": standings_audit,
                    "targetDay": target_day,
                    "eventTypeKey": ATP_SINGLES_EVENT_TYPE_KEY,
                },
            }

        raw_events = fixtures_payload.get("result") if isinstance(fixtures_payload, dict) else []
        if not isinstance(raw_events, list):
            raw_events = []

        matches: List[Dict[str, Any]] = []
        skipped_non_atp = 0
        skipped_bad_names = 0
        round_wins_inferred_matches = 0
        clay_round_wins_veto_candidates = 0
        qualifier_audit_candidates = 0

        for event in raw_events:
            if not isinstance(event, dict):
                continue
            if not self._is_atp_singles(event):
                skipped_non_atp += 1
                continue

            first_key = self._safe_str(event.get("first_player_key"))
            second_key = self._safe_str(event.get("second_player_key"))
            raw_first = self._safe_str(event.get("event_first_player"))
            raw_second = self._safe_str(event.get("event_second_player"))
            if not raw_first or not raw_second:
                skipped_bad_names += 1
                continue

            rank_a = self._ranking_entry(first_key, raw_first, standings_by_key, standings_by_name)
            rank_b = self._ranking_entry(second_key, raw_second, standings_by_key, standings_by_name)
            player_a = self._safe_str(rank_a.get("player")) or raw_first
            player_b = self._safe_str(rank_b.get("player")) or raw_second

            event_key = self._safe_str(event.get("event_key"))
            sport_event_id = f"api_tennis:{event_key}" if event_key else ""
            player_a_id = f"api_tennis:player:{first_key}" if first_key else ""
            player_b_id = f"api_tennis:player:{second_key}" if second_key else ""
            winner_id = self._winner_id(event)
            status, match_status = self._event_status(event)
            score = self._score(event, winner_id)
            start_time = f"{target_day}T{self._safe_str(event.get('event_time') or '00:00')}:00"
            qualification = self._bool_text(event.get("event_qualification"))
            if qualification:
                qualifier_audit_candidates += 1
            surface = self._infer_surface(event)
            round_label = self._safe_str(event.get("tournament_round"))
            round_wins_before_match = self._round_wins_before_match(round_label)
            if round_wins_before_match > 0:
                round_wins_inferred_matches += 1
            if surface == "Clay" and round_wins_before_match >= 2:
                clay_round_wins_veto_candidates += 1

            match = {
                "playerA": player_a,
                "playerB": player_b,
                "surface": surface,
                "playerAPoints": int(rank_a.get("points") or 0),
                "playerBPoints": int(rank_b.get("points") or 0),
                "playerARank": int(rank_a.get("rank") or 0),
                "playerBRank": int(rank_b.get("rank") or 0),
                "player_a_is_qualifier": False,
                "player_b_is_qualifier": False,
                "player_a_tournament_wins": round_wins_before_match,
                "player_b_tournament_wins": round_wins_before_match,
                "playerAIsQualifier": False,
                "playerBIsQualifier": False,
                "playerATournamentWins": round_wins_before_match,
                "playerBTournamentWins": round_wins_before_match,
                "playerAWins": round_wins_before_match,
                "playerBWins": round_wins_before_match,
                "player_b_qualifier_confidence": "manual_required" if qualification else "not_detected",
                "player_b_qualifier_source": "api_tennis_event_qualification_global_not_player_level" if qualification else "api_tennis_no_qualification_flag",
                "player_a_is_qualifier_detected": False,
                "player_b_is_qualifier_detected": bool(qualification),
                "player_a_qualifier_detection_confidence": "not_detected",
                "player_b_qualifier_detection_confidence": "audit_candidate" if qualification else "not_detected",
                "qualifierDetectorPolicy": "api_tennis_round_wins_engine_veto_active_qualifier_audit_only",
                "apiTennisRoundWinsBeforeMatch": round_wins_before_match,
                "roundWinsBeforeMatchSource": "api_tennis_tournament_round_inferred",
                "clayVetoContextActive": surface == "Clay",
                "sportradarSportEventId": sport_event_id,
                "sportEventId": sport_event_id,
                "apiTennisEventKey": event_key,
                "apiTennisFirstPlayerKey": first_key,
                "apiTennisSecondPlayerKey": second_key,
                "apiTennisEventTypeKey": self._safe_str(event.get("event_type_key") or ATP_SINGLES_EVENT_TYPE_KEY),
                "apiTennisEventTypeType": self._safe_str(event.get("event_type_type") or "Atp Singles"),
                "sportradarSeasonId": "",
                "sportradarCompetitionId": self._safe_str(event.get("tournament_key")),
                "tournament": self._safe_str(event.get("tournament_name")),
                "seasonName": self._clean_season_name(event, target_day),
                "apiTennisRawTournamentSeason": self._safe_str(event.get("tournament_season")),
                "round": self._safe_str(event.get("tournament_round")),
                "startTime": start_time,
                "status": status,
                "matchStatus": match_status,
                "winnerId": winner_id,
                "score": score,
                "source": "api_tennis",
                "dataProvider": "api_tennis",
                "tournamentWinsPolicy": "api_tennis_round_to_main_draw_wins_engine_veto_active",
                "sportradarSourcePlayerAId": player_a_id,
                "sportradarSourcePlayerBId": player_b_id,
                "sportradarPlayerAId": player_a_id,
                "sportradarPlayerBId": player_b_id,
                "sourcePlayerA": player_a,
                "sourcePlayerB": player_b,
                "sourceOriginalPair": f"{player_a} vs {player_b}",
                "apiTennisRawFirstPlayer": raw_first,
                "apiTennisRawSecondPlayer": raw_second,
                "apiTennisRawStatus": self._safe_str(event.get("event_status")),
                "apiTennisRaw": event,
            }
            matches.append(match)

        return {
            "status": "ok",
            "provider": "api_tennis",
            "matches": matches,
            "audit": {
                "provider": "api_tennis",
                "targetDay": target_day,
                "eventTypeKey": ATP_SINGLES_EVENT_TYPE_KEY,
                "eventTypeType": "Atp Singles",
                "timezone": self.config.timezone,
                "fixturesHttpStatus": fixtures_payload.get("httpStatus"),
                "fixturesElapsedMs": fixtures_payload.get("elapsedMs"),
                "rawEvents": len(raw_events),
                "matches": len(matches),
                "skippedNonAtpSingles": skipped_non_atp,
                "skippedBadNames": skipped_bad_names,
                "roundWinsPolicy": "api_tennis_tournament_round_to_main_draw_wins_before_match",
                "roundWinsInferredMatches": round_wins_inferred_matches,
                "clayRoundWinsVetoCandidates": clay_round_wins_veto_candidates,
                "qualifierAuditCandidates": qualifier_audit_candidates,
                "qualifierDetectorPolicy": "qualifier player-level remains audit only; round wins are active for clay veto",
                "standings": standings_audit,
                "callsPolicy": "2 API-Tennis calls per /daily: get_standings ATP + get_fixtures ATP Singles for target date.",
            },
        }
