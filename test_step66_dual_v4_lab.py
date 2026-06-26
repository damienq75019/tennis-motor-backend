from app import attach_audit_to_payload, attach_dual_v4_to_payload, v4_legacy_status, v4_status


def _sample_match(category="PREMIUM", pct=0.82, odd=1.60):
    return {
        "playerA": "Player A",
        "playerB": "Player B",
        "surface": "Hard",
        "playerAPoints": 2500,
        "playerBPoints": 900,
        "playerAHistoryMatches": 80,
        "playerBHistoryMatches": 70,
        "playerASurfaceHistoryMatches": 20,
        "playerBSurfaceHistoryMatches": 18,
        "premiumPct": pct,
        "decision": category,
        "oddA": odd,
        "pSwe": 0.76,
        "pAtp": 0.82,
        "pRank": 0.79,
        "pForm5": 0.70,
        "pForm10": 0.68,
        "pSurfaceForm5": 0.64,
        "pDominance": 0.73,
    }


def test_dual_v4_adds_legacy_and_full_candidate_without_mutating_v3():
    payload = {"status": "ok", "daily": {}, "matches": [_sample_match()]}
    payload = attach_audit_to_payload(payload, require_timestamps=False, include_market_check=True)
    payload = attach_dual_v4_to_payload(payload)

    match = payload["matches"][0]
    assert match["decision"] == "PREMIUM"
    assert match["premiumPct"] == 0.82
    assert "audit" in match
    assert "v4Legacy" in match
    assert "v4Lab" in match
    assert "v4LegacyShortReason" in match
    assert "v4ShortReason" in match
    assert payload["v4LegacySummary"]["version"].startswith("STEP64_")
    assert payload["v4Summary"]["version"].startswith("STEP65_")
    assert payload["v4DualSummary"]["version"].startswith("STEP66_")
    assert payload["daily"]["v4DualLab"]["officialMutation"] is False


def test_status_endpoints_are_separate():
    legacy = v4_legacy_status()
    full = v4_status()
    assert legacy["version"].startswith("STEP64_")
    assert full["version"].startswith("STEP65_")
    assert legacy["version"] != full["version"]
