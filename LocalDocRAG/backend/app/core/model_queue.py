import asyncio
import os
import time
from contextlib import asynccontextmanager

from app.core.errors import AppError
from app.db.database import uid


def process_alive(owner):
    """Only reap proven dead local owners; permission/format errors keep tickets.

    os.kill(pid, 0) is not a safe liveness probe on Windows (TerminateProcess).
    """
    try:
        pid = int(owner.split(':', 1)[0])
        if pid <= 0:
            return True
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no process
            try:
                code = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (ValueError, PermissionError, OSError):
        return True


class ModelQueue:
    """SQLite tickets + renewable lease serialize GPU calls across processes.

    Priority 0 is interactive; background images/embedding batches use 10.
    A running call is never preempted. Dead processes expire, live calls renew.
    """
    def __init__(self, db, timeout):
        self.db, self.timeout = db, timeout
        self.owner = f"{os.getpid()}:{uid()}"

    @asynccontextmanager
    async def acquire(self, priority=10):
        ticket = uid()
        created = time.time()
        self.db.execute("INSERT INTO model_queue VALUES(?,?,?,?,?)", (ticket, self.owner, priority, created, created + self.timeout * 4))
        # A crashed process leaves waiting tickets behind for up to 12 minutes.
        # Reap only dead local owners and never shorten an active GPU lease.
        for row in self.db.all("SELECT id,owner FROM model_queue WHERE id NOT IN (SELECT owner FROM leases WHERE name='gpu' AND expires>?)", (created,)):
            if not process_alive(row['owner']):
                self.db.execute('DELETE FROM model_queue WHERE id=?', (row['id'],))
        locked = False
        heartbeat = None
        try:
            while not locked:
                clock = time.time()
                with self.db.tx(immediate=True) as conn:
                    conn.execute("DELETE FROM model_queue WHERE expires<?", (clock,))
                    conn.execute("DELETE FROM leases WHERE name='gpu' AND expires<?", (clock,))
                    conn.execute("UPDATE model_queue SET expires=? WHERE id=?", (clock + self.timeout * 4, ticket))
                    first = conn.execute("SELECT id FROM model_queue ORDER BY priority,created,id LIMIT 1").fetchone()
                    if first and first[0] == ticket and not conn.execute("SELECT 1 FROM leases WHERE name='gpu'").fetchone():
                        conn.execute("INSERT INTO leases VALUES('gpu',?,?)", (ticket, clock + 60))
                        locked = True
                if not locked:
                    if clock - created > self.timeout * 4:
                        raise AppError("MODEL_QUEUE_TIMEOUT", "本地模型队列繁忙，请稍后重试。", 503)
                    await asyncio.sleep(0.15)

            async def renew():
                while True:
                    await asyncio.sleep(10)
                    self.db.execute("UPDATE leases SET expires=? WHERE name='gpu' AND owner=?", (time.time() + 60, ticket))
                    self.db.execute("UPDATE model_queue SET expires=? WHERE id=?", (time.time() + self.timeout * 4, ticket))
            heartbeat = asyncio.create_task(renew())
            yield
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            with self.db.tx(immediate=True) as conn:
                conn.execute("DELETE FROM leases WHERE name='gpu' AND owner=?", (ticket,))
                conn.execute("DELETE FROM model_queue WHERE id=?", (ticket,))

