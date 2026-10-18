"""Non-destructive NCUP assembly, archive expansion and preparation receipts.

Only outer distribution archives are opened. Ebooks, APKs and other native
containers remain intact. Each attempt owns a fresh directory; existing seeded
payloads are never reused, overwritten or removed.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath

from . import categories as cats, payload, release as rel, safeio

MAX_FILES = 100_000
MAX_EXPANDED = 2 * 1024**4
MAX_DEPTH = 4
RESERVE = 128 * 1024**2
ARCHIVE = re.compile(r"(?i)\.(rar|zip|7z|r\d\d|s\d\d|(?:7z|zip)\.\d{3})$")


def archive(path: str) -> bool:
    return bool(ARCHIVE.search(path))


def _member(name: str) -> Path:
    # Treat Windows paths the same on Linux. No links, drive letters, UNC,
    # control characters, parent components or ambiguous normalization.
    name = name.replace("\\", "/")
    parts = name.split("/")
    if not name or name.startswith(("/", "@")) or any(p in {"", ".", ".."} or ":" in p or any(ord(c) < 32 for c in p) for p in parts):
        raise ValueError("nem biztonságos archívumútvonal: " + repr(name))
    return Path(*PurePosixPath(name).parts)


def _space(directory: Path, needed: int) -> None:
    if needed > MAX_EXPANDED or shutil.disk_usage(directory).free < needed + RESERVE:
        raise ValueError("nincs elegendő hely az NCUP kiadás összeállításához / kibontási méretkorlát")


def _same(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    def digest(path):
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024**2), b""):
                value.update(block)
        return value.digest()
    return digest(left) == digest(right)


def link_or_copy(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_file() and _same(source, target):
            return "identical"
        raise ValueError(f"ütköző fájlnevek az NCUP kiadásban: {target.name}")
    try:
        os.link(source, target)
        return "hardlink"
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EACCES}:
            raise ValueError(f"nem készíthető NCUP fájl: {exc}") from exc
    _space(target.parent, source.stat().st_size)
    before = source.stat()
    try:
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024**2)
        shutil.copystat(source, target)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or target.stat().st_size != before.st_size:
            raise ValueError("másolás közben megváltozott a forrás: " + str(source))
    except BaseException:
        # This target was exclusively created by this call, inside our job.
        target.unlink(missing_ok=True)
        raise
    return "copy"


def archive_entries(path: Path) -> list[tuple[str, int, bool]]:
    if not shutil.which("7z"):
        raise ValueError("az archívum kibontásához hiányzik a 7z")
    result = safeio.run(["7z", "l", "-slt", "-ba", "-sccUTF-8", "-pNCUP_NO_PASSWORD", "--", str(path)], timeout=180)
    if result.returncode:
        raise ValueError("nem olvasható / jelszavas / hiányos archívum: " + path.name)
    entries, seen = [], set()
    for block in re.split(r"\r?\n\s*\r?\n", result.stdout.decode("utf-8", "strict").strip()):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        if "Path" not in fields:
            continue
        name = fields["Path"]
        member = _member(name)
        if fields.get("Encrypted") == "+" or any(fields.get(k) for k in ("Symbolic Link", "Hard Link")) or re.search(r"(?:^|\s)[lbcps][rwx-]{9}", fields.get("Attributes", "") + " " + fields.get("Mode", "")):
            raise ValueError("jelszavas vagy linket/speciális fájlt tartalmazó archívum: " + path.name)
        token = member.as_posix().casefold()
        if token in seen:
            raise ValueError("ismétlődő archívumútvonal: " + name)
        seen.add(token)
        directory = fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D")
        try:
            size = 0 if directory else int(fields.get("Size", "0"))
        except ValueError as exc:
            raise ValueError("hiányos archívumméret: " + name) from exc
        if size < 0 or size > MAX_EXPANDED:
            raise ValueError("archívumméret-korlát: " + name)
        entries.append((name, size, directory))
        if len(entries) > MAX_FILES:
            raise ValueError("túl sok fájl az archívumban")
    if not entries or not any(not directory for _, _, directory in entries):
        raise ValueError("üres / nem értelmezhető archívum: " + path.name)
    if sum(size for _, size, _ in entries) > MAX_EXPANDED:
        raise ValueError("az archívum kibontott mérete túl nagy")
    return entries


def _extract_member(archive_path: Path, name: str, size: int, destination: Path) -> None:
    """Stream one validated member into an exclusively created bounded file.

    Never give an archive tool a writable output directory: traversal/link and
    overwrite semantics therefore cannot escape the member validation above.
    CRC errors, missing volumes and short/long output all fail the job.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    args = ["7z", "x", "-so", "-spd", "-sccUTF-8", "-pNCUP_NO_PASSWORD", "--", str(archive_path), name]
    with subprocess.Popen(safeio.command(args, file_limit=None), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, start_new_session=(os.name == "posix")) as process:
        timer = threading.Timer(3600, safeio._kill, args=(process,))
        timer.daemon = True
        timer.start()
        try:
            count = 0
            with destination.open("xb") as stream:
                while True:
                    block = process.stdout.read(min(1024**2, size - count + 1))
                    if not block:
                        break
                    count += len(block)
                    if count > size:
                        raise ValueError("archívumméret-eltérés: " + name)
                    stream.write(block)
            if process.wait(timeout=30) or count != size:
                raise ValueError("hibás / hiányos archívumtag: " + name)
        finally:
            timer.cancel()
            safeio._kill(process)
            process.wait()


def _leaders(paths: list[Path]) -> list[Path]:
    leaders = []
    for path in paths:
        name = path.name
        if re.search(r"(?i)\.[rs]\d\d$", name):
            # Old RAR volumes need the .rar or .r00 start volume.
            base = path.with_suffix(".rar")
            if not base.exists():
                raise ValueError("hiányzik az archívum kezdőkötete: " + base.name)
            continue
        part = re.search(r"(?i)\.part(\d+)\.rar$", name)
        if part and int(part[1]) != 1:
            first = re.sub(r"(?i)\.part\d+\.rar$", ".part" + "1".zfill(len(part[1])) + ".rar", name)
            if not path.with_name(first).exists():
                raise ValueError("hiányzik az archívum kezdőkötete: " + first)
            continue
        volume = re.search(r"(?i)\.(?:7z|zip)\.(\d{3})$", name)
        if volume and int(volume[1]) != 1:
            if not path.with_name(name[:-3] + "001").exists():
                raise ValueError("hiányzik az archívum kezdőkötete: " + name)
            continue
        leaders.append(path)
    return leaders


class Workspace:
    def __init__(self, source: rel.Release, root: Path | None):
        self.source, self.root = source, root
        self.job: Path | None = None
        self.content: Path | None = None
        self.operations: list[dict] = []
        self.excluded: dict[str, str] = {}
        self.expanded_bytes = 0
        self.expected = ""
        from .workflow import fingerprint
        self.source_key = fingerprint(source)

    def create(self) -> Path:
        if self.job is None:
            if self.root is None or not self.root.is_absolute():
                raise ValueError("NCUP célmappa nincs beállítva; telepítés: install.sh --ncup-dir /teljes/út/NCUP")
            root = self.root.expanduser()
            if root.is_symlink():
                raise ValueError("az NCUP gyökere nem lehet szimbolikus link")
            root.mkdir(parents=True, exist_ok=True)
            root = root.resolve()
            source = self.source.path.resolve()
            if root == source or (source.is_dir() and root.is_relative_to(source)) or source.is_relative_to(root):
                raise ValueError("az NCUP célmappa és a forrás nem fedheti egymást")
            self.job = Path(tempfile.mkdtemp(prefix="job-", dir=root))
            self.receipt("preparing")
        return self.job

    def receipt(self, status: str) -> None:
        if self.job is None:
            return
        from .workflow import write_json
        write_json(self.job / "manifest.json", {"version": 1, "rules": payload.RULE_VERSION, "status": status,
            "source": str(self.source.path), "source_name": self.source.name, "source_fingerprint": self.source_key,
            "content": str(self.content) if self.content else None, "payload_fingerprint": self.expected,
            "operations": self.operations, "excluded": self.excluded})

    def expand(self, release: rel.Release, category: str | None, original: bool | None) -> rel.Release:
        archives = [f for f in release.files if archive(f.relpath)]
        if archives and release.path.is_file():
            path = release.path
            multipart = bool(re.search(r"(?i)\.part\d+\.rar$|\.(?:7z|zip)\.\d{3}$", path.name))
            multipart = multipart or bool(list(path.parent.glob(path.stem + ".[rRsS][0-9][0-9]")))
            if multipart:
                raise ValueError("többrészes archívumnál a teljes release mappáját add meg, hogy minden kötet ellenőrizhető legyen")
        packed_ok = category in cats.CATEGORIES and cats.CATEGORIES[category].packed_ok
        if archives and not (original and packed_ok):
            release = self._expand_tree(release, 0)
            wrapper = payload.common_wrapper([f.relpath for f in release.files])
            if wrapper and wrapper[-1].casefold() == self.source.name.casefold():
                release = self._inspect(release.path.joinpath(*wrapper))
                self.operations.append({"action": "remove-release-wrapper", "path": "/".join(wrapper)})
        # Only Blu-ray images are expanded, never DVD, software, game or audio
        # disc images. Detect actual BDMV entries, not a filename heuristic.
        images = [f for f in release.files if f.ext in {".iso", ".img"}]
        if category in cats.CATEGORIES and cats.CATEGORIES[category].group not in {"video", "clip"}:
            return release
        for item in images:
            image = release.path / item.relpath if release.path.is_dir() else release.path
            try:
                entries = archive_entries(image)
            except ValueError:
                continue  # other disc formats keep their existing static readers
            if not any("bdmv" in {p.casefold() for p in _member(name).parts} for name, _, _ in entries):
                continue
            if original:
                raise ValueError("Blu-ray ISO kibontásához saját feltöltési mód kell (--own); az eredeti lemezképfájlt megőriztem")
            output = self.create() / ("bluray-" + str(len(self.operations)))
            output.mkdir()
            from .discmount import unpack_bluray
            unpack_bluray(image, output, sum(size for _, size, _ in entries))
            self.operations.append({"action": "mount-bluray-readonly", "source": str(image)})
            # Keep companions, not the ISO; a standard disc root must not
            # acquire another arbitrary wrapper from the image's file name.
            merged = self.create() / ("disc-" + str(len(self.operations)))
            merged.mkdir()
            for f in release.files:
                if f.relpath != item.relpath:
                    src = release.path / f.relpath if release.path.is_dir() else release.path
                    link_or_copy(src, merged / f.relpath)
            self._merge(output, merged)
            release = self._inspect(merged)
        return release

    def _inspect(self, path: Path) -> rel.Release:
        result = rel.inspect(path)
        result.name = self.source.name
        return result

    def _merge(self, source: Path, target: Path) -> None:
        rel.assert_safe_tree(source)
        children = list(source.iterdir())
        if len(children) == 1 and children[0].is_dir() and children[0].name.casefold() == self.source.name.casefold():
            source = children[0]
            self.operations.append({"action": "remove-release-wrapper", "path": source.name})
        for path in sorted(source.rglob("*")):
            if path.is_file():
                link_or_copy(path, target / path.relative_to(source))

    def _expand_tree(self, release: rel.Release, depth: int) -> rel.Release:
        if depth >= MAX_DEPTH:
            raise ValueError("túl sok egymásba csomagolt archívum")
        rel.assert_safe_tree(release.path)
        target = self.create() / ("expanded-" + str(depth))
        target.mkdir()
        archives = []
        for item in release.files:
            path = release.path / item.relpath if release.path.is_dir() else release.path
            if archive(item.relpath):
                archives.append(path)
            else:
                link_or_copy(path, target / item.relpath)
        for index, path in enumerate(_leaders(archives)):
            entries = archive_entries(path)
            total_size = sum(size for _, size, _ in entries)
            self.expanded_bytes += total_size
            if self.expanded_bytes > MAX_EXPANDED:
                raise ValueError("összesített kibontási méretkorlát")
            _space(target, total_size)
            out = self.create() / f"archive-{depth}-{index}"
            out.mkdir()
            for name, size, directory in entries:
                if not directory:
                    _extract_member(path, name, size, out / _member(name))
            relative_parent = path.parent.relative_to(release.path) if release.path.is_dir() else Path()
            self._merge(out, target / relative_parent)
            self.operations.append({"action": "unpack", "source": str(path), "bytes": total_size, "files": len(entries)})
        expanded = self._inspect(target)
        if len(expanded.files) > MAX_FILES:
            raise ValueError("túl sok fájl a kibontott kiadásban")
        if any(archive(f.relpath) for f in expanded.files):
            return self._expand_tree(expanded, depth + 1)
        return expanded

    def assemble(self, release: rel.Release, category: cats.Category, original: bool):
        kept, errors = [], []
        for item in release.files:
            action, reason = payload.decision(release, item, category, original)
            if action == "OMIT":
                self.excluded[item.relpath] = reason
            else:
                kept.append(item)
                if action == "BLOCK":
                    errors.append(f"{reason}: {item.relpath}")
        wrapper = payload.common_wrapper([f.relpath for f in kept]) if not original else ()
        if not kept:
            raise ValueError("nem maradt feltölthető tartalom a fájlszabályok alkalmazása után")
        if self.excluded or wrapper or self.job is not None:
            if original and self.excluded and any("rendszer" not in value for value in self.excluded.values()):
                raise ValueError("az eredeti release módosításához saját feltöltési mód szükséges")
            # Unwrap an original archive's redundant release-name directory,
            # preserving every relative filename inside the release itself.
            if original:
                candidate = payload.common_wrapper([f.relpath for f in kept])
                if candidate and candidate[-1].casefold() == self.source.name.casefold():
                    wrapper = candidate
            stage = self.stage(release, {f.relpath for f in release.files} - {f.relpath for f in kept}, wrapper, rewrite=not original)
            prepared = self._inspect(stage)
            prepared.nfo_data = release.nfo_data
            prepared.imdb_id = prepared.imdb_id or release.imdb_id
            prepared.problems = list(dict.fromkeys(release.problems + prepared.problems))
            # Metadata NFO was read before it was omitted. Keep its snapshot and
            # path for resolvers, but own-mode attachments never include it.
            if not original:
                prepared.nfo = release.nfo
            release = prepared
        return release, errors

    def stage(self, release: rel.Release, excluded: set[str] | None = None, wrapper: tuple[str, ...] = (), *, rewrite: bool = True) -> Path:
        if self.content:
            return self.content
        rel.assert_safe_tree(release.path)
        name = self.source.name
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("érvénytelen NCUP kiadásnév")
        stage = self.create() / name
        stage.mkdir()
        omitted = excluded or set()
        mapping = {}
        for item in release.files:
            if item.relpath in omitted:
                continue
            relative = Path(*Path(item.relpath).parts[len(wrapper):])
            mapping[item.relpath] = relative.as_posix()
            src = release.path / item.relpath if release.path.is_dir() else release.path
            method = link_or_copy(src, stage / relative)
            self.operations.append({"action": method, "from": item.relpath, "to": relative.as_posix()})
        if wrapper:
            self.operations.append({"action": "remove-wrapper", "path": "/".join(wrapper)})
        if rewrite:
            self._checksums(stage, mapping, omitted)
        self.content = stage
        self.receipt("assembled")
        return stage

    def _checksums(self, stage: Path, mapping: dict[str, str], omitted: set[str]) -> None:
        # Rewrite staged SFV/M3U only by atomic replacement, never through a
        # hardlink. Resolve references relative to their original parent.
        names = {key.replace("\\", "/").casefold(): value for key, value in mapping.items()}
        removed = {key.replace("\\", "/").casefold() for key in omitted}
        for old, new in mapping.items():
            target = stage / new
            if target.suffix.casefold() not in {".sfv", ".m3u"}:
                continue
            raw = safeio.read_file(target)
            lines = []
            for line in raw.splitlines(keepends=True):
                stripped = line.strip()
                match = re.fullmatch(rb"(.+?)\s+[0-9a-fA-F]{8}", stripped) if target.suffix.casefold() == ".sfv" else None
                token = match[1] if match else (stripped if target.suffix.casefold() == ".m3u" and not stripped.startswith(b"#") else b"")
                ref = (Path(old).parent / token.decode("utf-8", "replace").strip('"').replace("\\", "/")).as_posix().casefold()
                if token and (ref in removed or ref not in names):
                    continue
                if token and ref in names:
                    relative = os.path.relpath(names[ref], str(Path(new).parent)).replace("\\", "/").encode("utf-8")
                    if relative != token:
                        line = line.replace(token, relative, 1)
                lines.append(line)
            updated = b"".join(lines)
            if updated != raw:
                fd, temporary = tempfile.mkstemp(prefix=".ncup-", dir=target.parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(updated)
                os.replace(temporary, target)

    def finalize(self) -> None:
        if self.content:
            from .workflow import fingerprint
            self.expected = fingerprint(rel.inspect(self.content))
            self.receipt("ready")

    def verify(self) -> None:
        from .workflow import fingerprint
        if fingerprint(rel.inspect(self.source.path)) != self.source_key:
            raise ValueError("megváltozott az NCUP forrása; új előkészítés szükséges")
        if self.content and fingerprint(rel.inspect(self.content)) != self.expected:
            raise ValueError("megváltozott az előkészített NCUP kiadás; új előkészítés szükséges")
