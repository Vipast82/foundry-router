

def test_the_reviewer_never_demands_a_self_referential_gate_pointer():
    """2026-11-02 GF-MUT1: every review flagged (major) the summary's 'Gate head: <sha>' line as
    stale; each fix commit ran a newer gate, so the line was stale again at the next review and
    the review budget ran out with the code done. The system prompt caps that at minor when the
    mechanical gate binding passes."""
    from foundry_coordinator import review as R
    assert "at most minor" in R.SYSTEM and "never require a document to name the final commit" in R.SYSTEM
