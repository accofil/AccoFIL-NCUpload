"""Read-only Blu-ray loop mount in a private Linux mount namespace.

The elevated helper mounts/unmounts only. A child drops supplementary groups,
GID and UID before copying disc files. Exiting the namespace releases mounts
even if the outer upload process is interrupted. Never execute disc content.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


def unpack_bluray(image: Path, output: Path, expected: int) -> None:
    from . import safeio
    from .preparation import _space
    if sys.platform != "linux" or not all(shutil.which(tool) for tool in ("sudo", "unshare", "mount", "umount")):
        raise ValueError("Blu-ray ISO mountolásához Linux, sudo, unshare és mount szükséges")
    _space(output, expected)
    args = ["sudo", "-n", "unshare", "--mount", "--propagation", "private", "--fork", "--kill-child",
            # -I -S: as root, ignore the environment, user site and the venv's site-packages (stdlib only).
            sys.executable, "-I", "-S", str(Path(__file__).resolve()), str(image.resolve()), str(output.resolve()),
            str(os.getuid()), str(os.getgid()), str(expected)]
    result = safeio.run(args, timeout=4 * 3600, file_limit=None)
    if result.returncode:
        raise ValueError("Blu-ray ISO mount/kibontás sikertelen: " + result.stderr.decode("utf-8", "replace")[-700:])
    report = json.loads(result.stdout)
    if not report.get("unmounted") or not report.get("readonly") or not report.get("files"):
        raise ValueError("a Blu-ray mount lezárása nem igazolt")


def _copy(root: Path, output: Path, maximum: int) -> tuple[int, int]:
    allowed = {"bdmv", "certificate", "aacs", "bdsvm", "any!", "fab!"}
    dirs = [p for p in root.iterdir() if p.name.casefold() in allowed]
    bdmv = next((p for p in dirs if p.name.casefold() == "bdmv"), None)
    if bdmv is None or not any(p.name.casefold() == "index.bdmv" for p in bdmv.iterdir()):
        raise ValueError("a lemezkép nem szabványos Blu-ray (BDMV/index.bdmv hiányzik)")
    count, size = 0, 0
    for directory in dirs:
        for current, subdirs, names in os.walk(directory, followlinks=False):
            for name in subdirs + names:
                path = Path(current) / name
                mode = path.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ValueError("nem szabályos fájl a Blu-ray lemezképben")
            for name in names:
                source = Path(current) / name
                count += 1
                size += source.stat().st_size
                if count > 100_000 or size > maximum:
                    raise ValueError("Blu-ray kibontási méretkorlát")
                relative = source.relative_to(root)
                # Normalize standard root capitalization for BDInfo.
                parts = relative.parts
                top = {"bdmv": "BDMV", "certificate": "CERTIFICATE"}.get(parts[0].casefold(), parts[0])
                standard = {"stream", "playlist", "clipinf", "backup", "auxdata", "meta", "bdjo", "jar", "ssif"}
                rest = [part.upper() if part.casefold() in standard else part for part in parts[1:-1]] + [parts[-1]]
                target = output.joinpath(top, *rest)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open("rb") as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst, 1024**2)
                shutil.copystat(source, target)
    return count, size


def _helper() -> None:
    image, output = Path(sys.argv[1]), Path(sys.argv[2])
    uid, gid, maximum = map(int, sys.argv[3:6])
    if os.geteuid() != 0 or uid == 0 or not image.is_absolute() or not output.is_absolute():
        raise ValueError("hibás mount helper paraméter")
    if image.is_symlink() or not image.is_file() or output.is_symlink() or output.stat().st_uid != uid or any(output.iterdir()):
        raise ValueError("hibás mount forrás/cél")
    mountpoint = Path(tempfile.mkdtemp(prefix="ncup-disc-", dir="/tmp"))
    mountpoint.chmod(0o755)
    mounted = False
    try:
        subprocess.run(["mount", "-t", "udf", "-o", "loop,ro,nosuid,nodev,noexec", "--", str(image), str(mountpoint)],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=120)
        mounted = True
        if not os.statvfs(mountpoint).f_flag & os.ST_RDONLY:
            raise ValueError("a Blu-ray mount nem csak olvasható")
        child = os.fork()
        if child == 0:
            try:
                os.setgroups([])
                os.setgid(gid)
                os.setuid(uid)
                count, size = _copy(mountpoint, output, maximum)
                # Receipt goes outside the torrent directory.
                receipt = output.with_name(output.name + ".mount.json")
                with receipt.open("x", encoding="utf-8") as stream:
                    json.dump({"files": count, "bytes": size}, stream)
                os._exit(0)
            except BaseException as exc:
                os.write(2, (str(exc) + "\n").encode())
                os._exit(1)
        _, status = os.waitpid(child, 0)
        if status:
            raise ValueError("a Blu-ray másolása sikertelen")
        report = json.loads(output.with_name(output.name + ".mount.json").read_text())
    finally:
        if mounted:
            subprocess.run(["umount", "--", str(mountpoint)], check=True, stdin=subprocess.DEVNULL, timeout=120)
        mountpoint.rmdir()
    print(json.dumps({**report, "unmounted": True, "readonly": True}))


if __name__ == "__main__":
    _helper()
