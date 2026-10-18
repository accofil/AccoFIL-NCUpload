"""Bounded static readers and subprocesses. Content from a release is never executed."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

TEXT_LIMIT = 2 << 20
META_LIMIT = 8 << 20
IMAGE_LIMIT = 32 << 20
PACKAGE_LIMIT = 2 << 30
MEMORY_LIMIT = 4 << 30


class LimitError(subprocess.SubprocessError, ValueError):
    pass


def read_file(path: str | Path, limit: int = TEXT_LIMIT) -> bytes:
    with Path(path).open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise LimitError('a fájl meghaladja az olvasási méretkorlátot')
    return data


def zip_info(archive, member: str, limit: int = META_LIMIT):
    info = archive.getinfo(member)
    if info.file_size > limit or info.file_size > max(1, info.compress_size) * 200:
        raise LimitError('a csomagtag túl nagy vagy túlzottan tömörített')
    return info


def read_zip(archive, member: str, limit: int = META_LIMIT) -> bytes:
    zip_info(archive, member, limit)
    with archive.open(member) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise LimitError('a kibontott csomagtag túl nagy')
    return data


def copy_zip(archive, member: str, target: Path, limit: int = PACKAGE_LIMIT) -> None:
    info = zip_info(archive, member, limit)
    if shutil.disk_usage(target.parent).free < info.file_size + (512 << 20):
        raise LimitError('nincs elég szabad hely a csomagtaghoz')
    count = 0
    created = False
    try:
        with archive.open(member) as source, target.open('xb') as sink:
            created = True
            while chunk := source.read(1 << 20):
                count += len(chunk)
                if count > limit or count > info.file_size:
                    raise LimitError('a kibontott csomagtag túl nagy')
                sink.write(chunk)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def command(args: list[str], file_limit: int | None = PACKAGE_LIMIT) -> list[str]:
    # prlimit applies limits before exec and is safe when other threads exist.
    tool = shutil.which('prlimit') if sys.platform.startswith('linux') else None
    if not tool:
        return list(args)
    prefix = [tool, f'--as={MEMORY_LIMIT}']
    if file_limit is not None:
        prefix.append(f'--fsize={file_limit}')
    return prefix + ['--', *map(str, args)]


def _kill(process) -> None:
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, OSError):
        pass


@contextmanager
def pipe(args: list[str], timeout: float = 120):
    """Time/memory bounded streaming for readers that skip large disc-image regions."""
    with subprocess.Popen(command(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, start_new_session=(os.name == 'posix')) as process:
        timer = threading.Timer(timeout, _kill, args=(process,))
        timer.daemon = True
        timer.start()
        try:
            yield process.stdout
        finally:
            timer.cancel()
            _kill(process)
            process.wait()


def run(args: list[str], timeout: float = 120, *, stdout_limit: int = 16 << 20,
        stderr_limit: int = 2 << 20, file_limit: int | None = PACKAGE_LIMIT,
        prefix_only: bool = False) -> subprocess.CompletedProcess:
    """Capture bounded output, kill the process group on timeout/overflow; stdin is always closed.

    Linux uses prlimit for address space and generated file size. Other systems
    retain time/output limits; they do not claim the Linux resource isolation.
    """
    output = [bytearray(), bytearray()]
    exceeded = threading.Event()
    with subprocess.Popen(command(args, file_limit), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, start_new_session=(os.name == 'posix')) as process:
        def drain(stream, index, limit):
            try:
                while True:
                    chunk = stream.read(min(65536, limit + 1 - len(output[index])))
                    if not chunk:
                        break
                    output[index].extend(chunk)
                    if len(output[index]) > limit:
                        exceeded.set()
                        _kill(process)
                        break
            except (OSError, ValueError):
                pass
        threads = [threading.Thread(target=drain, args=(stream, i, limit), daemon=True)
                   for i, (stream, limit) in enumerate(((process.stdout, stdout_limit), (process.stderr, stderr_limit)))]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=timeout)
        except BaseException:
            _kill(process)
            process.wait()
            raise
        finally:
            for thread in threads:
                thread.join(timeout=2)
            if any(t.is_alive() for t in threads):
                _kill(process)
                for thread in threads:
                    thread.join(timeout=2)
        if exceeded.is_set() and not prefix_only:
            raise LimitError('a segédprogram kimenete meghaladta a méretkorlátot')
        return subprocess.CompletedProcess(args, process.returncode,
                                           bytes(output[0][:stdout_limit]), bytes(output[1][:stderr_limit]))
