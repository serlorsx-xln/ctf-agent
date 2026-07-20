from backend.continue_prompt import build_continue_prompt


def test_partial_flags_keep_working_path():
    text = build_continue_prompt(
        accepted_flags=["aaa"],
        flags_required=2,
        bump_insights="try kerberos",
    )
    assert "1/2" in text
    assert "remaining" in text.lower()
    assert "Refine the path that worked" in text
    assert "Do NOT repeat what was tried" not in text


def test_no_progress_allows_adjust_then_switch():
    text = build_continue_prompt(bump_insights="sibling found ESC13")
    assert "insights" in text.lower() or "Insights" in text
    assert "Prefer adjusting parameters" in text


def test_plain_continue():
    text = build_continue_prompt()
    assert "Continue solving" in text
    assert "different approach" not in text.lower()
