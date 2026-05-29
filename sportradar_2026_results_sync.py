from __future__ import annotations

# STEP44 compatibility wrapper.
# The old filename is kept only so an older import does not crash.
# It does NOT call Sportradar anymore.

from api_tennis_results2026_sync import ApiTennisResults2026Syncer, Results2026Syncer

__all__ = ["ApiTennisResults2026Syncer", "Results2026Syncer"]
