"""Atomic file replacement so readers never observe a partial JSON/SQL file."""
import os
import stat
import tempfile


def atomic_text(path, text, mode=0o600):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    try:
        prior = os.stat(path)
    except FileNotFoundError:
        prior = None
    fd, temporary = tempfile.mkstemp(prefix=".intent-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.S_IMODE(prior.st_mode) if prior else mode)
            if os.geteuid() == 0:
                owner = prior or os.stat(directory)
                os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
