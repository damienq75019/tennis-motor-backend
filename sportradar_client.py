from __future__ import annotations

class SportradarError(RuntimeError):
    pass


class _DisabledConfig:
    access_level = "disabled"
    language = "disabled"
    timeout = 0


class SportradarClient:
    """STEP44 disabled stub. Sportradar is not used by Tennis Motor anymore."""

    def __init__(self, *args, **kwargs) -> None:
        self.config = _DisabledConfig()
        self.enabled = False

    def daily_summaries(self, *args, **kwargs):
        raise SportradarError("STEP44 : Sportradar est désactivé. Utilise API-Tennis.")

    def __getattr__(self, name):
        raise SportradarError(f"STEP44 : Sportradar est désactivé ({name}). Utilise API-Tennis.")
