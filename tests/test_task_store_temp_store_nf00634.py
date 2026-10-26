from __future__ import annotations

from aiworkhub import task_store

TEMP_STORE_MEMORY = 2


def test_connect_keeps_temp_btrees_in_memory_for_write_and_readonly(tmp_path):
    db = tmp_path / "t.sqlite"
    w = task_store._connect(db)
    try:
        assert w.execute("PRAGMA temp_store").fetchone()[0] == TEMP_STORE_MEMORY
    finally:
        w.close()
    r = task_store._connect(db, readonly=True)
    try:
        assert r.execute("PRAGMA temp_store").fetchone()[0] == TEMP_STORE_MEMORY
    finally:
        r.close()
