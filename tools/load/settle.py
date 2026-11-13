"""Share one bounded-rate close check across all workers in a load step."""
from __future__ import annotations

import asyncio


class SettlePollError(RuntimeError):
    pass


class SettlePoller:
    def __init__(self, runtime, *, interval_s=0.5):
        if interval_s < 0.5:
            raise ValueError("settle poll interval must be at least 0.5 s")
        self.runtime, self.interval_s = runtime, interval_s
        self.pending, self.closed = {}, set()
        self.wakeup = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def wait(self, session_id, timeout):
        if session_id in self.closed:
            return True
        if session_id not in self.pending:
            future = asyncio.get_running_loop().create_future()
            # A timed-out waiter can leave before the batch reports an error.
            future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            self.pending[session_id] = future
        future = self.pending[session_id]
        self.wakeup.set()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            return False

    async def _run(self):
        loop, next_poll = asyncio.get_running_loop(), 0.0
        while True:
            await self.wakeup.wait()
            await asyncio.sleep(max(0, next_poll - loop.time()))
            self.wakeup.clear()
            batch = tuple(self.pending)
            if not batch:
                continue
            try:
                ready = await self.runtime.settled_sessions(batch)
            except Exception as exc:
                error = SettlePollError(type(exc).__name__ + ": " + str(exc))
                for sid in batch:
                    self.pending.pop(sid).set_exception(error)
            else:
                for sid in set(ready).intersection(batch):
                    self.closed.add(sid)
                    self.pending.pop(sid).set_result(True)
            next_poll = loop.time() + self.interval_s
            if self.pending:
                self.wakeup.set()

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        for future in self.pending.values():
            future.cancel()
        self.pending.clear()
