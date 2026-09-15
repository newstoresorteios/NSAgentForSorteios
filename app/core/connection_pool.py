"""Small, lazy per-process pool; transactions are returned before remote I/O."""
from __future__ import annotations
from collections import OrderedDict
from threading import Lock
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


_pools: OrderedDict = OrderedDict()
_lock = Lock()


def connection_pool(url: str, max_size: int, timeout: float) -> ConnectionPool:
    key = (url, max_size, timeout)
    with _lock:
        if key in _pools:
            _pools.move_to_end(key)
            return _pools[key]
        pool = ConnectionPool(
            conninfo=url, min_size=0, max_size=max_size, timeout=timeout,
            max_idle=30, max_lifetime=300, reconnect_timeout=5, num_workers=1,
            kwargs={"row_factory": dict_row, "connect_timeout": 10, "prepare_threshold": None},
            open=True,
        )
        _pools[key] = pool
        if len(_pools) > 4:
            _, retired = _pools.popitem(last=False)
            retired.close()
        return pool
