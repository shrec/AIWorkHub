from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from aiworkhub.server import _serialize_task_lifecycle_write


def test_task_lifecycle_write_wrapper_allows_only_one_active_call() -> None:
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    @_serialize_task_lifecycle_write
    def mutation(value: int) -> int:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with state_lock:
            active -= 1
        return value

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sorted(pool.map(mutation, range(3))) == [0, 1, 2]
    assert max_active == 1
