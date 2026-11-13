"""NF-2026-02240: hub-managed named resource with a persisted FIFO queue."""

from __future__ import annotations

import threading
import time

import pytest

from aiworkhub import hub_resource_queue as hrq

CPU = hrq.CPU_HEAVY


def _always_alive(_pid: int) -> bool:
    return True


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "runtime" / "task_queue.sqlite"


def test_equal_priority_waiters_are_served_strictly_fifo(db):
    a = hrq.enqueue(db, CPU, "req-a")
    b = hrq.enqueue(db, CPU, "req-b")
    c = hrq.enqueue(db, CPU, "req-c")
    assert a < b < c
    assert hrq.try_take(db, c, is_alive=_always_alive) is False
    assert hrq.try_take(db, b, is_alive=_always_alive) is False
    assert hrq.try_take(db, a, is_alive=_always_alive) is True
    assert hrq.try_take(db, a, is_alive=_always_alive) is True  # idempotent
    assert hrq.try_take(db, b, is_alive=_always_alive) is False  # a still holds
    assert hrq.release(db, a) is True
    assert hrq.try_take(db, c, is_alive=_always_alive) is False  # b is the head
    assert hrq.try_take(db, b, is_alive=_always_alive) is True
    assert hrq.release(db, a) is False


def test_higher_priority_is_served_first_then_fifo(db):
    low = hrq.enqueue(db, CPU, "low", priority=0)
    high = hrq.enqueue(db, CPU, "high", priority=5)
    assert hrq.try_take(db, low, is_alive=_always_alive) is False
    assert hrq.try_take(db, high, is_alive=_always_alive) is True


def test_status_shows_holder_and_ordered_waiters_with_wait_time(db):
    clock = iter([100.0, 101.0, 102.0, 103.0]).__next__
    a = hrq.enqueue(db, CPU, "req-a", clock=clock)
    assert hrq.try_take(db, a, is_alive=_always_alive, clock=clock)
    hrq.enqueue(db, CPU, "req-b", clock=clock)
    hrq.enqueue(db, CPU, "req-c", priority=1, clock=clock)
    view = hrq.status(db, CPU, clock=lambda: 110.0)
    assert view["schema_id"] == hrq.STATUS_SCHEMA_ID
    assert view["ok"] is True and view["readonly"] is True
    (resource,) = view["resources"]
    assert resource["holder"]["owner"] == "req-a"
    assert resource["holder"]["held_seconds"] == 9.0
    assert resource["holder"]["wait_seconds"] == 1.0
    assert [w["owner"] for w in resource["waiters"]] == ["req-c", "req-b"]
    assert [w["position"] for w in resource["waiters"]] == [1, 2]
    assert resource["waiters"][0]["priority"] == 1
    assert resource["waiters"][1]["wait_seconds"] == 8.0
    assert resource["waiter_count"] == 2


def test_status_on_a_missing_database_is_an_empty_queue(db):
    view = hrq.status(db)
    assert view["resources"] == [
        {"resource": CPU, "holder": None, "waiters": [], "waiter_count": 0}
    ]


def test_a_dead_holder_is_reclaimed_by_the_next_acquirer(db):
    dead_pid = 2_000_000_001
    dead = hrq.enqueue(db, CPU, "crashed", pid=dead_pid)
    assert hrq.try_take(db, dead, is_alive=_always_alive)
    live = hrq.enqueue(db, CPU, "live")
    assert hrq.try_take(db, live, is_alive=lambda pid: pid != dead_pid) is True
    owners = [hrq.status(db)["resources"][0]["holder"]["owner"]]
    assert owners == ["live"]


def test_two_concurrent_holders_run_fifo(db):
    order: list[str] = []
    first = hrq.acquire(db, CPU, "req-1", max_wait_seconds=5, is_alive=_always_alive)

    def contender(name: str) -> None:
        with hrq.held(
            db, CPU, name, max_wait_seconds=10, poll_seconds=0.01, is_alive=_always_alive
        ):
            order.append(name)
            time.sleep(0.05)

    def waiting_owners() -> list[str]:
        return [w["owner"] for w in hrq.status(db)["resources"][0]["waiters"]]

    second = threading.Thread(target=contender, args=("req-2",))
    second.start()
    deadline = time.monotonic() + 5
    while waiting_owners() != ["req-2"] and time.monotonic() < deadline:
        time.sleep(0.01)
    third = threading.Thread(target=contender, args=("req-3",))
    third.start()
    while waiting_owners() != ["req-2", "req-3"] and time.monotonic() < deadline:
        time.sleep(0.01)
    view = hrq.status(db)["resources"][0]
    assert view["holder"]["owner"] == "req-1"
    assert waiting_owners() == ["req-2", "req-3"]
    hrq.release(db, first.entry_id)
    second.join(10)
    third.join(10)
    assert order == ["req-2", "req-3"]
    assert hrq.status(db)["resources"][0]["holder"] is None


def test_a_wait_timeout_withdraws_the_waiter(db):
    holder = hrq.acquire(db, CPU, "holder", max_wait_seconds=1, is_alive=_always_alive)
    ticks = iter([0.0, 0.5, 2.0, 2.0])
    with pytest.raises(hrq.ResourceWaitTimeout, match="resource_wait_timeout:cpu_heavy:1s"):
        hrq.acquire(
            db,
            CPU,
            "late",
            max_wait_seconds=1,
            clock=lambda: next(ticks),
            sleep=lambda _s: None,
            is_alive=_always_alive,
        )
    view = hrq.status(db)["resources"][0]
    assert view["holder"]["entry_id"] == holder.entry_id
    assert view["waiters"] == []


def test_held_releases_on_an_exception(db):
    with pytest.raises(RuntimeError, match="boom"):
        with hrq.held(db, CPU, "req", max_wait_seconds=1, is_alive=_always_alive):
            raise RuntimeError("boom")
    assert hrq.status(db)["resources"][0]["holder"] is None


def test_the_default_liveness_keeps_this_live_process(db):
    a = hrq.enqueue(db, CPU, "mine")
    assert hrq.try_take(db, a) is True
    b = hrq.enqueue(db, CPU, "next")
    assert hrq.try_take(db, b) is False  # the live holder is never reclaimed


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"resource": "gpu"}, "resource_unknown:gpu"),
        ({"owner": " "}, "resource_owner_invalid"),
        ({"owner": "x" * 201}, "resource_owner_invalid"),
        ({"priority": 101}, "resource_priority_invalid"),
        ({"priority": True}, "resource_priority_invalid"),
    ],
)
def test_bad_inputs_are_refused_by_name(db, kwargs, code):
    args = {"resource": CPU, "owner": "req", "priority": 0, **kwargs}
    with pytest.raises(hrq.ResourceQueueError, match=code):
        hrq.enqueue(db, args["resource"], args["owner"], priority=args["priority"])


def test_unknown_resource_status_and_bad_wait_limit_are_refused(db):
    with pytest.raises(hrq.ResourceQueueError, match="resource_unknown"):
        hrq.status(db, "gpu")
    with pytest.raises(hrq.ResourceQueueError, match="resource_wait_limit_invalid"):
        hrq.acquire(db, CPU, "req", max_wait_seconds=0)
    with pytest.raises(hrq.ResourceQueueError, match="resource_queue_entry_missing"):
        hrq.try_take(db, 12345)
