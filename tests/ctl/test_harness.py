"""The scenario harness must not treat a failed or unfinished run as evidence (regression from review)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from run_scenario import require_completed  # noqa: E402

DONE = {"finished": True, "sim_minute": 11520, "end_minute": 11520}


def test_nonzero_controller_exit_fails_the_harness():
    with pytest.raises(SystemExit) as e:
        require_completed(7, DONE)
    assert "controller exited with 7" in str(e.value)


def test_unfinished_scenario_fails_the_harness():
    with pytest.raises(SystemExit) as e:
        require_completed(0, {"finished": False, "sim_minute": 600, "end_minute": 11520})
    assert "not finished" in str(e.value)


def test_unreadable_final_state_fails_the_harness():
    with pytest.raises(SystemExit):
        require_completed(0, None)


def test_clean_finished_run_passes():
    require_completed(0, DONE)
