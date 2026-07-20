"""Resource-loop detector must not treat lab timeouts as OOM stuckness."""

from backend.loop_detect import LoopDetector, classify_resource_failure


def test_timeout_classified_but_not_oom_break():
    assert classify_resource_failure("foo\n[exit 124]\n") == "timeout"
    d = LoopDetector(fail_break_threshold=3)
    for _ in range(5):
        assert d.check_result("bash", "RPC timed out\n[exit 124]\n") is None


def test_oom_still_breaks():
    assert classify_resource_failure("killed\n[exit 137]\n") == "oom"
    d = LoopDetector(fail_break_threshold=3)
    assert d.check_result("bash", "[exit 137]") is None
    assert d.check_result("bash", "[exit 137]") is None
    assert d.check_result("bash", "[exit 137]") == "oom_break"


def test_bare_timed_out_no_longer_classifies():
    # Avoid matching "Error checking web enrollment: timed out" as a resource class.
    assert classify_resource_failure("Error checking web enrollment: timed out") is None


def test_exit_124_hint_does_not_say_abandon():
    from backend.loop_detect import RESOURCE_HINTS

    hint = RESOURCE_HINTS[124]
    assert "timeout" in hint.lower()
    assert "abandon" in hint.lower()
    assert "faster/lighter tool" not in hint
