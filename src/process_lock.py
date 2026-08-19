import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class AlreadyRunningError(RuntimeError):
    """Raised when another process already holds a named lock."""


def _try_lock(file_handle: BinaryIO) -> bool:
    file_handle.seek(0)

    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(
                file_handle.fileno(),
                msvcrt.LK_NBLCK,
                1,
            )
        except OSError:
            return False

        return True

    import fcntl

    try:
        fcntl.flock(
            file_handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        return False

    return True


def _unlock(file_handle: BinaryIO) -> None:
    file_handle.seek(0)

    if os.name == "nt":
        import msvcrt

        msvcrt.locking(
            file_handle.fileno(),
            msvcrt.LK_UNLCK,
            1,
        )
        return

    import fcntl

    fcntl.flock(
        file_handle.fileno(),
        fcntl.LOCK_UN,
    )


@contextmanager
def interprocess_lock(
    path: Path,
    *,
    description: str,
) -> Iterator[None]:
    """Hold a non-blocking lock that is released on process exit."""

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_handle = path.open("a+b")
    file_handle.seek(0, os.SEEK_END)

    if file_handle.tell() == 0:
        file_handle.write(b"\0")
        file_handle.flush()

    if not _try_lock(file_handle):
        file_handle.close()
        raise AlreadyRunningError(
            f"Another {description} is already running."
        )

    try:
        yield
    finally:
        _unlock(file_handle)
        file_handle.close()
