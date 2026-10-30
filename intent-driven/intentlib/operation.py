"""Serialize intent mutations and reconciliation across CLI/UI/timer processes."""
from contextlib import contextmanager
import errno
import fcntl
import os
import threading
import time


class BusyError(RuntimeError):
    pass


_local = threading.local()


@contextmanager
def exclusive(wait=True, timeout=900):
    if getattr(_local, "depth", 0):
        _local.depth += 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    home = os.environ.get("INTENT_HOME", "/home/ubuntu/intent-driven")
    directory = os.path.join(home, "state")
    os.makedirs(directory, exist_ok=True)
    fd = os.open(os.path.join(directory, "operation.lock"), os.O_CREAT | os.O_RDWR, 0o660)
    if os.geteuid() == 0:
        owner = os.stat(directory)
        os.fchown(fd, owner.st_uid, owner.st_gid)
        os.fchmod(fd, 0o660)
    start = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait or time.monotonic() - start >= timeout:
                    raise BusyError("其他意图或同步任务正在执行，请稍后重试")
                time.sleep(0.1)
        _local.depth = 1
        try:
            yield
        finally:
            _local.depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def legacy_hold_active():
    path = os.path.join(os.environ.get("INTENT_HOME", "/home/ubuntu/intent-driven"), "state", "reconcile.hold")
    try:
        with open(path) as stream:
            pid = int(stream.read().strip())
        if pid <= 0:
            raise ValueError("invalid pid")
        os.kill(pid, 0)
        return True
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    except (ValueError, ProcessLookupError):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return False
