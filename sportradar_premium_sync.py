from __future__ import annotations

# STEP44 compatibility wrapper.
# The active implementation is api_tennis_premium_sync.py and uses API-Tennis.
# This old filename is kept only so older imports do not crash.

from api_tennis_premium_sync import PremiumHistorySyncer, tracked_category

__all__ = ["PremiumHistorySyncer", "tracked_category"]
