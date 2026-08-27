"""Single-host process lock keyed by the resolved Radar database path."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class ServiceAlreadyRunningError(RuntimeError):
    pass


class ServiceInstanceLock:
    """Hold a non-blocking advisory lock for the life of the service process."""

    def __init__(self, db_path: Path) -> None:
        resolved = db_path.resolve()
        self.db_path = resolved
        self.path = resolved.with_name(f".{resolved.name}.service.lock")
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ServiceAlreadyRunningError(
                f"another KC News Radar service already owns {self.path}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\ndatabase={self.db_path}\n")
        handle.flush()
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None

    def __enter__(self) -> "ServiceInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def service_instance_running(db_path: Path) -> bool:
    """Observe whether the existing per-database lock file is currently held."""
    resolved = db_path.resolve()
    lock_path = resolved.with_name(f".{resolved.name}.service.lock")
    if not lock_path.is_file():
        return False
    with lock_path.open("r", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


__all__ = [
    "ServiceAlreadyRunningError",
    "ServiceInstanceLock",
    "service_instance_running",
]
