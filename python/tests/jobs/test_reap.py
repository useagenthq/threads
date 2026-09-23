"""The drills' leak check sees a process left in a worker's group, and nothing once it is gone."""

import subprocess
import sys

from jobs.drill import live_groups


def test_a_leftover_group_member_is_reported() -> None:
    leaked = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        assert live_groups([leaked.pid]) == [leaked.pid]
    finally:
        leaked.kill()
        leaked.wait()
    assert live_groups([leaked.pid]) == []
