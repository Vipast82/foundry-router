

def test_the_reviewer_never_demands_a_self_referential_gate_pointer():
    """2026-11-02 GF-MUT1: every review flagged (major) the summary's 'Gate head: <sha>' line as
    stale; each fix commit ran a newer gate, so the line was stale again at the next review and
    the review budget ran out with the code done. The system prompt caps that at minor when the
    mechanical gate binding passes."""
    from foundry_coordinator import review as R
    assert "at most minor" in R.SYSTEM and "never require a document to name the final commit" in R.SYSTEM


def test_the_reviewer_is_told_docs_memory_is_not_the_sessions_to_fix():
    """v0.181.71: VR-AS34 (2026-11-06) was sent back twice for 'dedupe docs/memory/ASSETS.md
    rows' and 'merge the KNOWN_ISSUES row'; a session may never edit docs/memory (the
    coordinator's memory, MEMORY_NOTE lines), so it reported 'edit denied' and looped."""
    from foundry_coordinator import review as R
    assert "docs/memory/" in R.SYSTEM and "required_repairs" in R.SYSTEM.split("docs/memory/")[1]
