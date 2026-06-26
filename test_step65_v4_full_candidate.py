from tennis_motor_v4_lab import analyze_match_v4, attach_v4_lab_to_payload, status_payload


def base_match(**overrides):
    m = {
        "playerA": "Player A",
        "playerB": "Player B",
        "surface": "Hard",
        "playerAPoints": 1000,
        "playerBPoints": 800,
        "playerAHistoryMatches": 80,
        "playerBHistoryMatches": 70,
        "playerASurfaceHistoryMatches": 20,
        "playerBSurfaceHistoryMatches": 18,
        "playerAForm5Matches": 5,
        "playerBForm5Matches": 5,
        "playerAForm10Matches": 10,
        "playerBForm10Matches": 10,
        "pSwe": 0.78,
        "pAtp": 0.76,
        "pRank": 0.74,
        "pForm5": 0.70,
        "pForm10": 0.72,
        "pSurfaceForm5": 0.73,
        "pDominance": 0.75,
        "audit": {"flagCodes": []},
    }
    m.update(overrides)
    return m


def test_premium_can_be_downgraded_for_negative_edge():
    m = base_match(step56Confidence=0.86, step56OfficialCategory="STEP56_PREMIUM", oddA=1.12)
    v4 = analyze_match_v4(m)
    assert v4["v3Category"] == "PREMIUM"
    assert v4["v4Decision"] == "NO_BET"
    assert v4["v4Action"].startswith("DOWNGRADE_PREMIUM")
    assert v4["edge"] < 0


def test_proche_can_be_upgraded_to_value():
    m = base_match(step56Confidence=0.77, step56OfficialCategory="STEP56_PROCHE", oddA=1.65)
    v4 = analyze_match_v4(m)
    assert v4["v3Category"] == "PROCHE"
    assert v4["v4Decision"] == "BET"
    assert v4["v4Action"].startswith("UPGRADE_PROCHE")
    assert v4["edge"] > 0


def test_refuse_can_be_refuse_value_candidate():
    m = base_match(step56Confidence=0.70, step56OfficialCategory="STEP56_REFUSE", oddA=1.90, refuseValueStrict=True)
    v4 = analyze_match_v4(m)
    assert v4["v3Category"] == "REFUSE"
    assert v4["v4Action"] in {"REFUSE_VALUE_STRONG", "REFUSE_VALUE_WATCH", "REFUSE_VALUE_HISTORICAL_WATCH"}
    assert v4["v4Decision"] in {"BET", "WATCH"}


def test_not_analyzed_is_blocked():
    m = base_match(nonAnalyzable=True, analysisStatus="not_analyzed", reason="points ATP manquants", playerAPoints=0)
    v4 = analyze_match_v4(m)
    assert v4["v4Decision"] == "BLOCKED"
    assert v4["v4Action"] == "BLOCKED_ATP_POINTS_MISSING"
    assert v4["grade"] == "X"


def test_attach_summary_counts_all_categories():
    payload = {"matches": [
        base_match(step56Confidence=0.86, step56OfficialCategory="STEP56_PREMIUM", oddA=1.12),
        base_match(step56Confidence=0.77, step56OfficialCategory="STEP56_PROCHE", oddA=1.65),
        base_match(step56Confidence=0.70, step56OfficialCategory="STEP56_REFUSE", oddA=1.90),
    ]}
    out = attach_v4_lab_to_payload(payload)
    assert out["v4Summary"]["scope"] == "all_categories"
    assert out["v4Summary"]["totalMatches"] == 3
    assert out["v4Summary"]["premiumDowngraded"] >= 1
    assert out["v4Summary"]["procheUpgraded"] >= 1
    assert out["v4Summary"]["refuseValueCandidates"] >= 1
    assert all("v4Lab" in m for m in out["matches"])


def test_status_exposes_full_candidate_logic():
    status = status_payload()
    assert status["status"] == "ok"
    assert status["scope"] == "all_categories"
    assert "REFUSE" in status["categoryLogic"]
