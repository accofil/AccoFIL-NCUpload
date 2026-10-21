"""Ordered upload queues with per-item configuration and durable progress.

TXT: one filesystem path per line. JSON: paths or {path, answers, category,
original, enabled, label} objects. No entry is interpreted as a shell command.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import requests

from . import privacy, safeio, workflow
from .client import NcoreError, SessionExpired

OPTIONS = {
    "category", "original", "imdb", "other_link", "name", "description", "source", "extras", "menu", "tags",
    "images", "cover", "no_screens", "no_make_sample", "title", "year", "artist", "album", "author", "isbn",
    "narrator", "mbid", "discogs", "spotify", "steam", "gog", "platform", "version_hint", "package", "language",
    "kind", "auto", "proof_link", "install_guide", "offline", "daily_limit", "request", "edit", "dry_run", "yes",
    "no_seed", "force",
}
ENTRY_FIELDS = {"path", "answers", "category", "original", "enabled", "label"}
DONE = {"complete", "already_complete", "uploaded_no_seed", "previewed", "disabled"}
MAX_ITEMS = 1000
LABELS = {"pending": "várakozik", "running": "folyamatban", "complete": "feltöltve és seedelve",
          "already_complete": "korábban feltöltve", "uploaded_no_seed": "feltöltve, seed kihagyva",
          "previewed": "előnézet kész", "disabled": "kihagyva", "skipped": "elhalasztva", "needs_review": "átnézendő",
          "failed": "hibás", "seed_pending": "seedelési hiba", "uncertain": "bizonytalan feltöltés",
          "needs_login": "új belépés szükséges", "waiting_daily_limit": "napi keretre vár", "interrupted": "megszakadt"}


def _path(value: str, base: Path) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\0"):
        raise ValueError("a lista elérési útja nem üres, egysoros szöveg legyen")
    path = Path(value).expanduser()
    return str((base / path).resolve())


def entries(values: list, base: Path) -> list[dict]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ITEMS:
        raise ValueError(f"a feltöltési listában 1–{MAX_ITEMS} tétel lehet")
    result, seen = [], set()
    for index, value in enumerate(values, 1):
        data = {"path": value} if isinstance(value, str) else copy.deepcopy(value)
        if not isinstance(data, dict) or set(data) - ENTRY_FIELDS or "path" not in data:
            raise ValueError(f"{index}. tétel: ismeretlen vagy hiányzó listamező")
        for key in ("enabled", "original"):
            if key in data and not isinstance(data[key], bool):
                raise ValueError(f"{index}. tétel: {key} true/false legyen")
        for key in ("category", "label"):
            if key in data and (not isinstance(data[key], str) or any(c in data[key] for c in "\r\n\0")):
                raise ValueError(f"{index}. tétel: {key} egysoros szöveg legyen")
        data["path"] = _path(data["path"], base)
        identity = os.path.normcase(data["path"])
        if identity in seen:
            raise ValueError(f"{index}. tétel: ugyanaz a bemenet kétszer szerepel: {data['path']}")
        seen.add(identity)
        if data.get("answers"):
            data["answers"] = _path(data["answers"], base)
        elif "answers" in data:
            raise ValueError(f"{index}. tétel: az answers elérési útja nem lehet üres")
        data.setdefault("enabled", True)
        result.append(data)
    if not any(item["enabled"] for item in result):
        raise ValueError("a listában nincs kiválasztott tétel")
    return result


def read_list(path: Path) -> list[dict]:
    path = path.expanduser().resolve()
    text = safeio.read_file(path).decode("utf-8-sig")
    if path.suffix.casefold() == ".json" or (path.suffix.casefold() != ".txt" and text.lstrip().startswith(("[", "{"))):
        data = json.loads(text)
        if isinstance(data, dict):
            if set(data) - {"version", "items"} or data.get("version", 1) != 1:
                raise ValueError("ismeretlen listaverzió vagy listamező")
            data = data.get("items")
        return entries(data, path.parent)
    return entries([line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")], path.parent)


def directory(root: Path, ncup_dir: Path | None = None) -> list[dict]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("--batch mellett PATH gyűjtőmappa legyen")
    paths = [str(p) for p in sorted(root.iterdir()) if not p.name.startswith(".") and p.name.casefold() != "ncup" and
             (ncup_dir is None or p.resolve() != ncup_dir.resolve()) and
             not p.name.casefold().endswith((".ncore.json", ".torrent", ".txt", ".jpg", ".jpeg", ".png", ".sfv", ".nfo", ".diz", ".m3u"))]
    return entries(paths, root)


def read_state(path: Path) -> dict:
    state = json.loads(safeio.read_file(path, 16 << 20).decode("utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("options"), dict):
        raise ValueError("hibás batch-állapotfájl")
    if set(state["options"]) - OPTIONS:
        raise ValueError("ismeretlen beállítás a batch-állapotban")
    items = state.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError("hibás tétellista a batch-állapotban")
    values = [item.get("entry") for item in items if isinstance(item, dict)]
    if len(values) != len(items):
        raise ValueError("hibás batch-tétel")
    canonical = entries(values, path.resolve().parent)
    for item, entry in zip(items, canonical):
        item["entry"] = entry
    return state


def restore(args) -> None:
    """Resume uses the saved operation mode; it cannot silently promote a preview."""
    state = read_state(Path(args.resume_batch))
    for key in ("dry_run", "offline", "auto", "yes", "force", "no_seed"):
        if getattr(args, key, False) and not state["options"].get(key, False):
            raise ValueError("a folytatás az eredeti batch beállításait használja; új módhoz indíts új listát")
    for key, value in state["options"].items():
        setattr(args, key, copy.deepcopy(value))


@contextmanager
def lock(folder: Path):
    """OS-owned lock, released even after a process crash; no stale PID-file bypass."""
    workflow.private_dir(folder)
    stream = (folder / "queue.lock").open("a+b")
    os.chmod(folder / "queue.lock", 0o600)
    held = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                if stream.tell() == 0:
                    stream.write(b"0"); stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except OSError:
            raise ValueError("ezt a batch-listát egy másik folyamat már kezeli") from None
        yield
    finally:
        if held:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _save(state: dict, target: Path, summary: Path) -> None:
    state["updated_at"] = time.time()
    workflow.write_json(target, state)
    workflow.write_json(summary, [{k: v for k, v in item.items() if k != "entry"} | {"path": item["entry"]["path"]} for item in state["items"]])


def _details(item: dict, child, code: int) -> None:
    item["exit_code"] = code
    if getattr(child, "_run_dir", None):
        item["run_dir"] = child._run_dir
        folder = Path(child._run_dir)
        for name, key in (("review.json", "review"), ("preview.json", "preview")):
            if (folder / name).exists():
                item[key] = json.loads(safeio.read_file(folder / name, 4 << 20).decode("utf-8"))
        if (folder / "state.json").exists():
            data = json.loads(safeio.read_file(folder / "state.json").decode("utf-8"))
            for key in ("torrent_id", "status"):
                if key in data:
                    item["torrent_id" if key == "torrent_id" else "upload_status"] = data[key]
            if str(data.get("torrent_id", "")).isdigit():
                item["url"] = "https://ncore.pro/torrents.php?action=details&id=" + str(data["torrent_id"])
    if code == 0:
        outcome = getattr(child, "_outcome", "")
        if outcome == "already_complete":
            item["status"] = "already_complete"
        elif item.get("upload_status") == "complete" and not child.dry_run:
            item["status"] = "complete"
        elif item.get("upload_status") == "uploaded" and child.no_seed and not child.dry_run:
            item["status"] = "uploaded_no_seed"
        elif child.dry_run:
            item["status"] = "needs_review" if item.get("preview", {}).get("auto_ok") is False else "previewed"
        else:
            item["status"] = "skipped" if outcome == "skipped" else "needs_review"
    else:
        item["status"] = {3: "needs_login", 10: "needs_review", 11: "seed_pending", 12: "uncertain"}.get(code, "failed")
    if item.get("upload_status") in {"uncertain", "uploading"}:
        item["status"] = "uncertain"


def execute(args, config, client, runner, say) -> int:
    """The same single-item runner enforces bans, duplicate checks, daily limits and seeding."""
    if args.answers:
        raise ValueError("batch mellett tételenként adj meg answers fájlt vagy ~/.config/ncore-up/answers/<név>.ncore.json adatlapot")
    if args.resume_batch:
        target = Path(args.resume_batch).resolve()
        state = read_state(target)
        folder = target.parent
        summary = config.log_dir / (folder.name + ".json")
    else:
        if args.batch_list:
            if args.path:
                raise ValueError("--batch-list mellett a bemenetek a listában szerepelnek; PATH nem szükséges")
            selected = read_list(Path(args.batch_list))
            origin = {"kind": "list", "path": str(Path(args.batch_list).resolve())}
        else:
            if not args.path:
                raise ValueError("--batch mellett gyűjtőmappa kell")
            selected = directory(Path(args.path), config.ncup_dir)
            origin = {"kind": "directory", "path": str(Path(args.path).resolve())}
        identifier = "batch-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        folder = config.log_dir / identifier
        target, summary = folder / "queue.json", config.log_dir / (identifier + ".json")
        state = {"version": 1, "id": identifier, "created_at": time.time(), "source": origin,
                 "options": {key: copy.deepcopy(getattr(args, key)) for key in OPTIONS if hasattr(args, key)},
                 "items": [{"id": f"{i:04}", "entry": entry, "status": "pending" if entry["enabled"] else "disabled", "attempts": 0}
                           for i, entry in enumerate(selected, 1)]}
        for key in ("description", "cover", "install_guide"):
            if state["options"].get(key):
                state["options"][key] = str(Path(state["options"][key]).expanduser().resolve())
        if state["options"].get("images"):
            state["options"]["images"] = [str(Path(value).expanduser().resolve()) for value in state["options"]["images"]]
    args._batch_file = str(target)
    with lock(folder):
        if args.resume_batch:
            state = read_state(target)  # a prior worker may have finished while the lock was acquired
        state["status"] = "running"
        state.pop("stop_reason", None)
        _save(state, target, summary)
        say(f"Feltöltési lista: {len(state['items'])} tétel. Állapot: {target}")
        for item in state["items"]:
            if item["status"] in DONE:
                continue
            child = argparse.Namespace(**{k: copy.deepcopy(v) for k, v in vars(args).items() if not k.startswith("_")})
            for key, value in state["options"].items():
                setattr(child, key, copy.deepcopy(value))
            entry = item["entry"]
            child.path, child.batch, child.batch_list, child.resume_batch = entry["path"], False, None, None
            child.answers = entry.get("answers")
            for key in ("category", "original"):
                if key in entry:
                    setattr(child, key, entry[key])
            prior = Path(item["run_dir"]) / "state.json" if item.get("run_dir") else None
            if prior and prior.exists() and not child.dry_run:
                prior_state = json.loads(safeio.read_file(prior).decode("utf-8"))
                if prior_state.get("status") in {"uploading", "uncertain", "uploaded", "seed_pending", "complete"}:
                    child._resume_run = str(prior.parent)
            for stale in ("error", "review", "preview", "upload_status", "torrent_id", "run_dir", "url"):
                item.pop(stale, None)
            item.update(status="running", attempts=item["attempts"] + 1, started_at=time.time())
            def track_run(path):
                item["run_dir"] = str(path)
                _save(state, target, summary)
            child._on_run_dir = track_run
            _save(state, target, summary)
            say(f"[{item['id']}/{len(state['items']):04}] {entry.get('label') or entry['path']}")
            stop = False
            try:
                code = runner(child, replace(config, log_dir=folder / "runs"), client)
            except SessionExpired as exc:
                code, stop = 3, True
                item["error"] = privacy.redact(str(exc))
            except workflow.DailyLimitReached as exc:
                code, stop = 10, True
                item["error"] = privacy.redact(str(exc))
                state["stop_reason"] = "daily_limit"
            except (NcoreError, requests.RequestException) as exc:
                code, stop = 4, True
                item["error"] = privacy.redact(str(exc))
            except (ValueError, OSError) as exc:
                code = 10
                item["error"] = privacy.redact(str(exc))
            except BaseException:
                item["status"] = "interrupted"
                if getattr(child, "_run_dir", None):
                    item["run_dir"] = child._run_dir
                state["status"] = "interrupted"
                _save(state, target, summary)
                raise
            _details(item, child, code)
            if state.get("stop_reason") == "daily_limit":
                item["status"] = "waiting_daily_limit"
            item["finished_at"] = time.time()
            say(f"  {LABELS.get(item['status'], item['status'])}" + (": " + item["error"] if item.get("error") else ""))
            if stop or item["status"] in {"seed_pending", "uncertain"}:
                state["status"] = "stopped"
                state.setdefault("stop_reason", item["status"])
                _save(state, target, summary)
                break
            _save(state, target, summary)
        else:
            state["status"] = "finished" if all(item["status"] in DONE for item in state["items"]) else "needs_review"
            _save(state, target, summary)
        counts = {name: sum(item["status"] == name for item in state["items"]) for name in sorted({item["status"] for item in state["items"]})}
        say("Lista eredménye: " + ", ".join(f"{LABELS.get(key, key)}: {value}" for key, value in counts.items()))
        say(f"Folytatás: ncore-up --resume-batch '{target}'")
        return 0 if state["status"] == "finished" else 3 if state.get("stop_reason") == "needs_login" else 10
