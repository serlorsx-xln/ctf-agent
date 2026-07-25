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
    # An accepted flag is proof the path works — never push it off that path.
    assert "unproven" not in text


def test_no_progress_pushes_off_the_burned_technique():
    text = build_continue_prompt(bump_insights="sibling found ESC13")
    assert "insights" in text.lower()
    assert "unproven" in text
    assert "different surface or technique" in text
    assert "Prefer adjusting parameters" not in text


def test_plain_continue_pushes_off_the_burned_technique():
    text = build_continue_prompt()
    assert "Continue solving" in text
    assert "unproven" in text
    assert "different surface or technique" in text


def test_no_progress_keeps_prior_work_reusable():
    """Pivoting the technique must not read as "throw away your scripts"."""
    text = build_continue_prompt()
    assert "/challenge/workspace" in text
    assert "reuse the findings" in text.lower()


def test_infra_recovery_resumes_instead_of_pivoting():
    """A transport death is not evidence the approach was wrong."""
    for insights in ("", "sibling found ESC13"):
        text = build_continue_prompt(infra_recovery=True, bump_insights=insights)
        assert "Resume where you left off" in text
        assert "unproven" not in text
        assert "different surface or technique" not in text


def test_infra_recovery_with_partial_flags_still_credits_progress():
    text = build_continue_prompt(
        accepted_flags=["aaa"],
        flags_required=2,
        infra_recovery=True,
    )
    assert "transport/bridge timeout" in text
    assert "1/2" in text
    assert "unproven" not in text
