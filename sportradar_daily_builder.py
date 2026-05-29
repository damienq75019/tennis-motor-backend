from __future__ import annotations

class SportradarDailyBuilder:
    """STEP44 disabled stub. /daily uses ApiTennisDailyBuilder only."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def build_matches_for_day(self, target_day: str):
        return {
            "status": "error",
            "provider": "api_tennis_only",
            "targetDay": target_day,
            "error": "STEP44 : SportradarDailyBuilder est désactivé. Utilise ApiTennisDailyBuilder.",
            "matches": [],
            "audit": {"sportradarDisabled": True, "apiTennisOnly": True},
        }
