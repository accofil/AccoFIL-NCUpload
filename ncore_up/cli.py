"""ncore-up: upload a release (original or own) to nCore, in any non-XXX category, and start seeding it.

usage:
  ncore-up PATH [options]      check everything, show the upload, ask, upload, seed
  ncupload --categories        list category codes
  ncupload --check             test the nCore session (cookies)
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

from . import __version__, categories as cats, form as forms, keys, net, plan as plans, qbit, release as rel, torrent
from .client import NcoreClient, NcoreError, SessionExpired, load_cookies
from .config import CONFIG_FILE, Config
from . import batch as batch_jobs
from . import banlist, duplicates, requirements, workflow, privacy, safeio


def say(text: str = "") -> None:
    print(privacy.redact(text), flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ncupload", description="AccoFIL NCUpload – feltöltés nCore-ra (XXX kivételével)")
    p.add_argument("--configure", action="store_true", help="interaktív beállítóvarázsló: útvonalak, qBittorrent és API-kulcsok")
    p.add_argument("path", nargs="?", help="a feltöltendő mappa vagy fájl")
    p.add_argument("--category", "-c", help="kategória kód (lásd --categories); alapból automatikus")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--original", dest="original", action="store_true", default=None, help="eredeti release-ként (NFO kell)")
    mode.add_argument("--own", dest="original", action="store_false", help="nem release-ként (saját feltöltés)")
    p.add_argument("--imdb", help="IMDb azonosító (tt...), ha az automatikus nem jó")
    p.add_argument("--other-link", help="egyéb filmadatbázis link (alapból port.hu / tvmaze / TMDB)")
    p.add_argument("--name", help="a torrent neve (nem release-nél alapból a sablon szerinti)")
    p.add_argument("--description", help="leírás fájl (BBCode) a generált helyett")
    p.add_argument("--source", help="a videó és hang ellenőrzött eredete a leírásban")
    p.add_argument("--extras", help="'Extra:' a leírásban")
    p.add_argument("--menu", choices=["van", "nincs"], help="lemez menüje (ismeretlen esetben pontosítás kell)")
    p.add_argument("--tags", help="stílus-címkék vesszővel (zene, e-könyv)")
    p.add_argument("--images", nargs=3, metavar="KÉP", help="3 saját kép a generáltak helyett")
    p.add_argument("--cover", help="infobar borítókép (≤300x300-ra kicsinyítem)")
    p.add_argument("--no-screens", action="store_true", help="eredeti release-nél ne készüljön kép")
    p.add_argument("--no-make-sample", action="store_true", help="ne vágjon sample-t, ha hiányzik (alapból vág, hardlinkes másolatba)")
    hint = p.add_argument_group("pontosítás (ha az automatikus azonosítás téved)")
    for flag, text in (("--title", "cím"), ("--year", "év"), ("--artist", "előadó"), ("--album", "album"), ("--author", "szerző"),
                       ("--isbn", "ISBN"), ("--narrator", "hangoskönyv felolvasója"), ("--mbid", "MusicBrainz release id vagy URL"),
                       ("--discogs", "Discogs release id vagy URL"), ("--spotify", "Spotify albumazonosító vagy URL"), ("--steam", "Steam app id"), ("--gog", "GOG product id"),
                       ("--platform", "konzol/platform (ps4, switch, ps2, windows, macos, ...)"), ("--version-hint", "verzió"),
                       ("--package", "Android csomagnév"), ("--language", "tartalom nyelve (hu/en/de)"), ("--kind", "tartalomfajta (audiobook, book, comic, manga, magazine)")):
        hint.add_argument(flag, help=text)
    p.add_argument("--auto", action="store_true", help="felügyelet nélkül: csak hibátlan és biztos azonosítású tervet tölt fel, a többit félreteszi")
    p.add_argument("--answers", help="kiegészítő JSON adatlap; alapból PATH.ncore.json")
    p.add_argument("--proof-link", action="append", default=[], help="igazoló link, ismételhető")
    p.add_argument("--install-guide", help="magyar telepítési útmutató UTF-8 fájlja")
    queue = p.add_mutually_exclusive_group()
    queue.add_argument("--batch", action="store_true", help="PATH közvetlen almappáinak/fájljainak soros feldolgozása")
    queue.add_argument("--batch-list", metavar="LISTA", help="TXT/JSON feltöltési lista, tételenkénti beállításokkal")
    queue.add_argument("--resume-batch", metavar="QUEUE_JSON", help="megszakadt lista folytatása az eredeti beállításokkal")
    p.add_argument("--resume", metavar="NAPLÓMAPPA", help="már feltöltött torrent seedelésének folytatása, új feltöltés nélkül")
    p.add_argument("--offline", action="store_true", help="--dry-run mellett sütifájl nélkül készít tervet; online ellenőrzések függőben")
    p.add_argument("--daily-limit", type=int, help="a rang napi kereténél szigorúbb helyi korlát; gördülő 24 óra, külső feltöltéseket nem számolja")
    p.add_argument("--request", help="kérés azonosítója, ha kérést teljesítesz")
    p.add_argument("--edit", action="store_true", help="feltöltés előtt megnyitja a leírást szerkesztésre ($EDITOR)")
    p.add_argument("--dry-run", action="store_true", help="mindent előkészít és kiír, de nem tölt fel")
    p.add_argument("--yes", "-y", action="store_true", help="ne kérdezzen rá a feltöltésre")
    p.add_argument("--no-seed", action="store_true", help="ne adja hozzá a qBittorrenthez")
    p.add_argument("--force", action="store_true", help="--dry-run mellett a nem kötelező hibákkal is kiírja a tervet; feltöltési kaput nem old fel")
    p.add_argument("--categories", action="store_true", help="kategória kódok listája")
    p.add_argument("--check", action="store_true", help="nCore munkamenet ellenőrzése")
    p.add_argument("--refresh-banlist", action="store_true", help="Wiki-tiltólista frissítése és változásnaplózása, feltöltés nélkül")
    p.add_argument("--import-cookies", metavar="FÁJL", help="csak ncore.pro sütik ellenőrzött, védett telepítése Netscape fájlból")
    p.add_argument("--import-keys", metavar="FÁJL", help="API-kulcsok védett beolvasása KEY=VALUE fájlból, értékek kiírása nélkül")
    p.add_argument("--doctor", action="store_true", help="adatforrások és kulcsok állapota (alapból hálózati hívás nélkül)")
    p.add_argument("--doctor-live", action="store_true", help="--doctor mellett kisméretű, élő adatforrás-próbák, feltöltés nélkül")
    p.add_argument("--json", action="store_true", help="--doctor jelentés JSON formátumban")
    p.add_argument("--version", action="version", version=f"AccoFIL NCUpload {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.configure:
        from .installer import configure_existing
        return configure_existing()
    if args.categories:
        for code, category in cats.CATEGORIES.items():
            if category.group != "xxx":
                say(f"{code:<14} {category.label}")
        return 0
    config = Config.load()
    try:
        if args.resume_batch:
            if args.path or args.resume:
                raise ValueError("--resume-batch mellett csak a queue.json állapotfájlt add meg")
            try:
                batch_jobs.restore(args)
            except OSError:
                raise ValueError("a --resume-batch állapotfájlja nem olvasható; a queue.json fájlt add meg") from None
        if args.resume and (args.batch or args.batch_list):
            raise ValueError("egy torrent --resume és a batch mód nem kombinálható")
        if args.doctor_live and not args.doctor:
            raise ValueError("--doctor-live mellett --doctor is szükséges")
        imported = []
        if args.import_keys:
            from .credentials import import_keys
            imported = import_keys(Path(args.import_keys))
            if not args.doctor:
                say("Védetten mentett kulcsmezők: " + ", ".join(imported))
                return 0
            config = Config.load()
        if args.doctor:
            from .diagnostics import report
            status = report(live=args.doctor_live)
            if imported:
                status["imported_fields"] = imported
            if args.json:
                say(json.dumps(status, ensure_ascii=False, indent=2))
            else:
                for source in status["sources"]:
                    needed = " vagy ".join(" + ".join(option) for option in source.get("credential_options", [])) or ", ".join(source["missing_keys"])
                    say(f"{source['name']:<20} {source['status']}" + (" — kell: " + needed if source["missing_keys"] else ""))
            return 0
        if args.import_cookies:
            from .session import import_cookies
            import_cookies(Path(args.import_cookies), Path(config.cookies))
            say("nCore munkamenet ellenőrizve és védett sütifájlba telepítve.")
            return 0
        if args.offline and (not args.dry_run or args.resume or args.check or args.refresh_banlist):
            raise ValueError("--offline csak --dry-run mellett használható")
        client = workflow.OfflineClient() if args.offline else NcoreClient(load_cookies(config.cookies))
    except ValueError as exc:
        say(f"HIBA: {exc}")
        return 2
    except requests.RequestException as exc:
        say(f"HIBA: a művelet megszakadt ({type(exc).__name__})")
        return 4
    except NcoreError as exc:
        say(f"HIBA: {exc}")
        return 3
    except OSError as exc:
        if args.import_keys:
            say(f"HIBA: az API-kulcsfájl nem olvasható vagy a konfiguráció nem menthető ({type(exc).__name__})")
            return 2
        say(f"HIBA: nem olvasható a sütifájl ({config.cookies}): {exc}\nExportáld a böngészőből Netscape (cookies.txt) formátumban; beállítás: {CONFIG_FILE}")
        return 2
    try:
        if args.refresh_banlist:
            snapshot = banlist.refresh(client, config.work_dir)
            if args.json:
                say(json.dumps({k: snapshot[k] for k in ("version", "checked_at", "sha256", "changes", "unknown_rules")}, ensure_ascii=False))
            else:
                say(f"Tiltólista frissítve: {snapshot['version']}, {len(snapshot['rules'])} szabály, "
                    f"{len(snapshot['unknown_rules'])} értelmezendő új szabály.")
            return 10 if snapshot["unknown_rules"] else 0
        if args.resume:
            return resume(Path(args.resume), config, client)
        if args.check:
            client.upload_token()
            say("nCore munkamenet rendben (a feltöltő oldal elérhető).")
            check_sources()
            return 0
        if not args.path and not (args.batch_list or args.resume_batch):
            build_parser().print_help()
            return 2
        return batch(args, config, client) if args.batch or args.batch_list or args.resume_batch else run(args, config, client)
    except SessionExpired as exc:
        say(f"HIBA: {exc}")
        return 3
    except NcoreError as exc:
        say(f"HIBA: {exc}")
        return 4
    except ValueError as exc:
        say(f"HIBA: {exc}")
        return 2
    except (OSError, requests.RequestException) as exc:
        say(f"HIBA: a művelet megszakadt ({type(exc).__name__})")
        return 4


def run(args: argparse.Namespace, config: Config, client: NcoreClient) -> int:
    if getattr(args, "_resume_run", None):
        _run_folder(args, Path(args._resume_run))
        return resume(Path(args._resume_run), config, client)
    answers = workflow.load_answers(args)
    try:
        release = rel.inspect(args.path)
    except FileNotFoundError:
        say(f"HIBA: nincs ilyen fájl vagy mappa: {args.path}")
        return 2
    say(f"Release: {release.name}  ({len(release.files)} fájl, {release.size / 1e9:.2f} GB)")
    job_key = workflow.fingerprint(release)
    ledger = workflow.Ledger(config.work_dir)
    try:
        existing = ledger.get(job_key)
    finally:
        ledger.close()
    if existing and not args.dry_run:
        _run_folder(args, Path(existing[1]))
        if existing[0] == "complete":
            args._outcome = "already_complete"
            say(f"Már feltöltve és igazoltan seedelve: {existing[1]}")
            return 0
        return resume(Path(existing[1]), config, client)

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    run_dir = config.log_dir / f"{stamp}-{release.name[:80]}"
    workflow.private_dir(run_dir)
    _run_folder(args, run_dir)
    options = plans.Options(
        category=args.category, original=args.original, imdb=args.imdb, name=args.name, description_file=args.description,
        source=args.source, extras=args.extras, menu=None if args.menu is None else args.menu == "van",
        tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()], images=args.images or [], cover=args.cover,
        screenshots=not args.no_screens, request_id=args.request or "", other_link=args.other_link, make_sample=not args.no_make_sample,
        hints={**answers.get("hints", {}), **_hint_args(args)}, proof_links=args.proof_link, install_guide=args.install_guide,
    )
    if config.ncup_dir is None or not config.ncup_dir.is_absolute():
        say("HIBA: az NCUP célmappát telepítéskor meg kell adni: install.sh --ncup-dir /teljes/út/NCUP")
        return 2
    options.ncup_dir = config.ncup_dir
    options.bdinfo = config.bdinfo
    if release.path.resolve().is_relative_to(config.ncup_dir.resolve()):
        say("HIBA: az NCUP előkészített kiadásait forrásként nem dolgozom fel újra; az eredeti anyagot add meg")
        return 2
    say("NCUP fájlszabályok, kibontás és adatok gyűjtése (PORT.hu, TMDB, MusicBrainz, moly.hu, Steam, ...), techinfo és képek ...")
    try:
        plan = plans.build(release, options, client, run_dir)
    except ValueError as exc:
        workflow.write_json(run_dir / "review.json", {"path": str(release.path), "status": "NEEDS_REVIEW", "reason": str(exc),
                            "answers_file": str(release.path.with_name(release.path.name + ".ncore.json")), "example": {"category": "", "hints": {}}})
        say(f"Átnézésre: {exc}\n  {run_dir / 'review.json'}")
        return 10 if args.auto else 2

    candidates = client.exact_matches(release.name)
    if plan.data.name != release.name:
        candidates += client.exact_matches(plan.data.name)
    if plan.meta and plan.category.group != "video":
        query = " ".join([*plan.meta.creators[:1], plan.meta.title]).strip() if plan.category.group in {"ebook", "music", "clip"} else plan.meta.title
        if query and len(query) >= 6:
            candidates += client.search(query, by="name")
    if plan.category.group == "ebook" and hasattr(client, "duplicate_facts"):
        if len(candidates) > 20:
            plan.doubts.append("túl sok könyvtalálat: pontosítani kell a műazonosítást")
        else:
            for item in candidates:
                if not item.get("format") and str(item.get("torrent_id", "")).isdigit():
                    item.update(client.duplicate_facts(int(item["torrent_id"])))
    if plan.data.infobar and plan.data.infobar.imdb_id:
        others = client.search(plan.data.infobar.imdb_id, by="imdb")
        if others:
            plan.notes.append(f"azonos IMDb-vel {len(others)} torrent van fent (nézd meg a dupe-szabályzat szerint):"
                              + "".join(f"\n      #{o['torrent_id']} {o['release_name']} ({o['category']})" for o in others[:8]))
            candidates += others
    decisions = duplicates.decide(plan, candidates, answers.get("dupe_decisions"))
    for decision in decisions:
        message = f"dupe #{decision.torrent_id}: {decision.reason} ({decision.name})"
        if decision.status == "BLOCK":
            plan.errors.append(message)
        elif decision.status == "REVIEW":
            plan.doubts.append(message)
        else:
            plan.notes.append(message)
    workflow.write_json(run_dir / "duplicates.json", [d.as_dict() for d in decisions])

    description_path = run_dir / "leiras.txt"
    description_path.write_text(plan.data.description, encoding="utf-8")
    for field_name, text in plan.texts.items():
        (run_dir / f"{field_name}.txt").write_text(text, encoding="utf-8")
    if args.edit and not args.dry_run:
        editor = os.environ.get("EDITOR") or ("nano" if subprocess.run(["which", "nano"], capture_output=True).returncode == 0 else "vi")
        subprocess.run(shlex.split(editor) + [str(description_path)], check=False)
        plan.data.description = description_path.read_text(encoding="utf-8")

    plan.data.description = forms.description_text(plan.data)
    description_path.write_text(plan.data.description, encoding="utf-8")
    requirements.evaluate(plan)  # final edited text, not just resolver metadata
    if getattr(client, "offline", False):
        plan.requirements.append(requirements.Requirement("online_checks", "UNKNOWN", "nCore munkamenet, tiltólista és dupe ellenőrzése még szükséges"))

    token = client.upload_token()
    minimum_size = getattr(client, "minimum_size", 0)
    if plan.category.group not in {"ebook", "mobil"} and plan.release.size < minimum_size:
        plan.errors.append(f"a ranghoz tartozó minimális tartalomméret {minimum_size // 1024**2} MB (Wiki 30)")
    ban_checks = banlist.check(plan, client, config.work_dir, run_dir, token, phase="prepare")
    plan.requirements.extend(ban_checks)
    plan.errors.extend(check.message for check in ban_checks if check.status == "FAIL")
    form = forms.build(token, plan.data)
    files_preview = {**{k: v.name for k, v in plan.attachments.items()}, **{k: f"{k}.txt" for k in plan.texts}, "torrent_fajl": "(készül)"}
    plan.errors.extend(forms.missing(form, files_preview, plan.has_sample))
    workflow.write_json(run_dir / "requirements.json", [r.as_dict() for r in plan.requirements])
    workflow.write_json(run_dir / "preview.json", {"name": plan.data.name, "category": plan.category.code,
                        "source": str(release.path), "content_path": str(plan.content_path),
                        "ncup_manifest": str(plan.preparation.job / "manifest.json") if plan.preparation and plan.preparation.job else None,
                        "original": plan.data.original, "confidence": plan.confidence, "auto_ok": plan.auto_ok,
                        "errors": [privacy.redact(v) for v in plan.errors], "doubts": [privacy.redact(v) for v in plan.doubts],
                        "warnings": [privacy.redact(v) for v in plan.warnings]})
    show(plan, form, files_preview, run_dir)
    (run_dir / "form.json").write_text(json.dumps({k: v for k, v in form.items() if k != "getUnique"}, ensure_ascii=False, indent=1), encoding="utf-8")

    mandatory_failed = [r for r in plan.requirements if r.status == "FAIL"]
    if mandatory_failed or (plan.errors and (not args.force or not args.dry_run)):
        say("\nLeállok a fenti kötelező hibák miatt.")
        _set_aside(run_dir, plan)
        return 5
    unresolved = plan.doubts or any(r.status == "UNKNOWN" for r in plan.requirements)
    if unresolved and not args.dry_run:
        _set_aside(run_dir, plan)
        say(f"\nKötelező döntés/ellenőrzés hiányzik: {run_dir / 'review.json'}")
        return 10
    if args.auto and not plan.auto_ok and not args.dry_run:
        _set_aside(run_dir, plan)
        why = ("hibák vannak (--force mellett sem tölt fel)" if plan.errors else "kétség: " + "; ".join(plan.doubts) if plan.doubts
               else f"az azonosítás nem elég biztos ({plan.confidence:.2f})")
        say(f"\nAutomatikus mód: {why} — félretettem átnézésre: {run_dir}")
        return 10
    if args.dry_run:
        say(f"\nSzárazpróba: nem töltöttem fel. Napló és fájlok: {run_dir}")
        return 0

    torrent_path = run_dir / "local.torrent"
    say("Torrent készítése (mkbrr)...")
    try:
        if plan.preparation:
            plan.preparation.verify()
        local_torrent = torrent.create(config.mkbrr, plan.content_path, torrent_path)
        if forms.needs_techinfo(plan.category.code, plan.data.original):
            meta = torrent.info(local_torrent)
            layout = {"/".join(part.decode("utf-8") for part in item[b"path"]): item[b"length"] for item in meta[b"files"]} if b"files" in meta else {meta[b"name"].decode("utf-8"): meta[b"length"]}
            if layout != plan.techinfo_files:
                raise ValueError("a torrent tényleges fájllistája és a techinfo eltér; feltöltés leállítva")
    except (RuntimeError, OSError, ValueError) as exc:  # changed payload or missing/failing mkbrr
        say(f"HIBA: a torrent nem készült el ({config.mkbrr}): {exc}")
        return 2
    local_hash = torrent.info_hash(local_torrent)
    torrent_path.write_bytes(local_torrent)
    torrent_path.chmod(0o600)
    say(f"Torrent kész: {len(local_torrent) / 1024:.0f} KiB, info-hash {local_hash}")
    if not args.yes and not args.auto:
        answer = input("Feltöltsem nCore-ra? [i/N] ").strip().lower()
        if answer not in {"i", "igen", "y", "yes"}:
            args._outcome = "skipped"
            say("Kihagyva.")
            return 0

    if workflow.fingerprint(rel.inspect(release.path)) != job_key:
        say("HIBA: a forrás a feldolgozás közben megváltozott; új előkészítés kell")
        return 5
    if plan.preparation:
        try:
            plan.preparation.verify()
        except ValueError as exc:
            say(f"HIBA: {exc}")
            return 5
    state = {"key": job_key, "status": "uploading", "source": str(release.path), "content_path": str(plan.content_path),
             "name": plan.data.name, "category": plan.category.code, "before_ids": [str(d.get("torrent_id", "")) for d in candidates],
             "local_hash": local_hash, "created": time.time(), "run_dir": str(run_dir)}
    # Load all attachments before reserving an upload. A local read failure did
    # not send a POST and must not create an uncertain server-side transaction.
    files = {"torrent_fajl": (f"{release.name}.torrent", local_torrent, "application/x-bittorrent")}
    for field_name, path in plan.attachments.items():
        files[field_name] = (path.name, safeio.read_file(path, safeio.IMAGE_LIMIT), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    for field_name, text in plan.texts.items():
        files[field_name] = (f"{release.name[:100]}.txt", text.encode("utf-8"), "text/plain")
    # Hashing and the user's review can take time. Re-check before reserving a
    # transaction: re-read the script's current mirror and repeat the live hint;
    # a new ban cannot be bypassed by an earlier successful preview.
    token = client.upload_token()
    form = forms.build(token, plan.data)
    final_bans = banlist.check(plan, client, config.work_dir, run_dir, token, phase="before-upload")
    plan.requirements = [r for r in plan.requirements if r.id != "banlist"] + final_bans
    workflow.write_json(run_dir / "requirements.json", [r.as_dict() for r in plan.requirements])
    denied = [r for r in final_bans if r.status in {"FAIL", "UNKNOWN"}]
    if denied:
        for check in denied:
            say(check.message)
        plan.errors.extend(r.message for r in denied if r.status == "FAIL")
        plan.doubts.extend(r.message for r in denied if r.status == "UNKNOWN")
        _set_aside(run_dir, plan)
        say("Feltöltés leállítva a beküldés előtti tiltólista-ellenőrzésnél.")
        return 5 if any(r.status == "FAIL" for r in denied) else 10
    ledger = workflow.Ledger(config.work_dir)
    try:
        rank_budget = getattr(client, "daily_limit", 5)
        ledger.reserve(job_key, run_dir, min(args.daily_limit if args.daily_limit is not None else rank_budget, rank_budget), local_hash)
    finally:
        ledger.close()
    workflow.write_json(run_dir / "state.json", state)

    try:
        result = client.upload(form, files)
    except (NcoreError, requests.RequestException):
        state["status"] = "uncertain"
        _save_state(state, config)
        raise
    response_path = run_dir / "response.html"
    response_path.write_text(privacy.redact(result.body), encoding="utf-8")
    response_path.chmod(0o600)
    if not result.ok:
        state["status"] = "uncertain" if result.uncertain else "rejected"
        _save_state(state, config)
        say("A feltöltés NEM sikerült:" + "".join(f"\n  - {e}" for e in result.errors) + f"\n  (válasz: {run_dir / 'response.html'})")
        return 9
    say(f"Feltöltve: https://ncore.pro/torrents.php?action=details&id={result.torrent_id}")
    (run_dir / "result.txt").write_text(privacy.redact(f"torrent_id={result.torrent_id}\nurl={result.url}\n"), encoding="utf-8")
    state.update(status="uploaded", torrent_id=result.torrent_id)
    _save_state(state, config)

    if args.no_seed:
        say("Seedelés kihagyva (--no-seed): 30 percen belül el kell indítani!")
        return 0
    return _seed(state, config, client)


def _save_state(state: dict, config: Config) -> None:
    folder = Path(state["run_dir"])
    workflow.write_json(folder / "state.json", state)
    ledger = workflow.Ledger(config.work_dir)
    try:
        ledger.mark(state["key"], state["status"], folder)
    finally:
        ledger.close()


def resume(folder: Path, config: Config, client) -> int:
    folder = folder.resolve()
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    state["run_dir"] = str(folder)
    if state["status"] == "rejected":
        say("Az oldal elutasította ezt a feltöltést; javítás után az eredeti bemenet újrafuttatható.")
        return 9
    if state["status"] in {"uploading", "uncertain"}:
        local = (folder / "local.torrent").read_bytes()
        recovered = []
        for item in client.exact_matches(state["name"]):
            tid = str(item.get("torrent_id", ""))
            if tid in state.get("before_ids", []) or item.get("category") != state["category"] or not tid.isdigit():
                continue
            site = client.download_torrent(int(tid), client.passkey(int(tid)))
            if torrent.same_content(site, local):
                recovered.append(int(tid))
        if len(recovered) != 1:
            say(f"A korábbi feltöltés kimenetele bizonytalan. Nem indítok új POST-ot. Ellenőrizd az nCore-on: {folder}")
            return 12
        state.update(status="uploaded", torrent_id=recovered[0])
        _save_state(state, config)
    return _seed(state, config, client)


def _seed(state: dict, config: Config, client) -> int:
    folder = Path(state["run_dir"])
    tid = int(state["torrent_id"])
    content = Path(state["content_path"])
    site_path = folder / f"ncore-{tid}.torrent"
    try:
        site = site_path.read_bytes() if site_path.exists() else client.download_torrent(tid, client.passkey(tid))
        local = (folder / "local.torrent").read_bytes()
        if not torrent.same_content(site, local):
            raise qbit.QbitError("az nCore torrent tartalma eltér az előkészítettől")
        if not content.exists():
            raise qbit.QbitError(f"az előkészített tartalom hiányzik: {content}")
        site_hash = torrent.info_hash(site)
        same = site_hash == state["local_hash"]
        site_path.write_bytes(site)
        site_path.chmod(0o600)
        status = qbit.add_torrent(config.qb_url, config.qb_user, config.qb_pass, site, str(content.parent),
                                  skip_checking=False, category=config.qb_category, info_hash=site_hash, wait=30)
        state.update(status="complete", site_hash=site_hash, qbit_state=status, seed_verified=time.time())
        _save_state(state, config)
        say(f"qBittorrent seedelés visszaellenőrizve ({status}; mentési hely: {content.parent}).")
        return 0
    except (qbit.QbitError, NcoreError, requests.RequestException, OSError, ValueError) as exc:
        state.update(status="seed_pending", seed_error=str(exc))
        _save_state(state, config)
        say(f"HIBA: feltöltve, de a seedelés nem indult el: {exc}\n  30 percen belül indítsd: ncupload --resume '{folder}'\n  Torrent: {site_path}")
        return 11


def _run_folder(args, folder: Path) -> None:
    args._run_dir = str(folder)
    notify = getattr(args, "_on_run_dir", None)
    if notify:
        notify(folder)


def batch(args, config: Config, client) -> int:
    return batch_jobs.execute(args, config, client, run, say)


PROBES = {
    "PORT.hu": "https://port.hu/search/suggest-list?q=matrix",
    "Wikidata": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=Matrix&language=en&format=json",
    "MusicBrainz": "https://musicbrainz.org/ws/2/artist/?query=artist:Omega&fmt=json&limit=1",
    "Steam": "https://store.steampowered.com/api/storesearch/?term=portal&l=hungarian&cc=HU",
    "Open Library": "https://openlibrary.org/search.json?q=network+effect&limit=1",
    "TVmaze": "https://api.tvmaze.com/lookup/shows?imdb=tt0903747",
}


def check_sources() -> None:
    """Reachability of the main keyless sources and the list of keys still missing from config.env."""

    for name, url in PROBES.items():
        ok = net.get(url, expire=0, retries=0, quiet=True) is not None
        say(f"  {name:<14} {'rendben' if ok else 'NEM ÉRHETŐ EL'}")
    missing = keys.missing()
    say("Hiányzó API-kulcsok (config.env): " + (", ".join(missing) if missing else "nincs"))
    if "DEEPL_KEY" not in missing:
        from . import translate

        used = translate.usage()
        if used:
            say(f"DeepL keret: {used[0]} / {used[1]} karakter")


def _set_aside(run_dir: Path, plan: plans.Plan) -> None:
    lines = ([f"biztonság: {plan.confidence:.2f}"] + [f"HIBA: {e}" for e in plan.errors] + [f"KÉTSÉG: {d}" for d in plan.doubts]
             + [f"FIGYELEM: {w}" for w in plan.warnings])
    (run_dir / "atnezesre.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    workflow.write_json(run_dir / "review.json", {
        "status": "NEEDS_REVIEW", "path": str(plan.release.path), "confidence": plan.confidence,
        "errors": plan.errors, "doubts": plan.doubts,
        "requirements": [r.as_dict() for r in plan.requirements if r.status != "PASS"],
        "answers_file": str(plan.release.path.with_name(plan.release.path.name + ".ncore.json")),
        "answer_fields": {"proof_links": [], "install_guide": "", "source": "", "images": [], "hints": {},
                          "dupe_decisions": {"TORRENT_ID": {"action": "allow", "reason": "szabályzat szerinti konkrét indok"}}},
    })


def _hint_args(args: argparse.Namespace) -> dict:
    names = {"title": "title", "year": "year", "artist": "artist", "album": "album", "author": "author", "isbn": "isbn",
             "narrator": "narrator", "mbid": "mbid", "discogs": "discogs", "steam": "steam", "gog": "gog", "platform": "platform",
             "version_hint": "version", "package": "package", "language": "language", "kind": "kind", "spotify": "spotify"}
    return {hint: getattr(args, attr) for attr, hint in names.items() if getattr(args, attr, None)}


def show(plan: plans.Plan, form: dict[str, str], files: dict[str, str], run_dir: Path) -> None:
    say("")
    for note in plan.notes:
        say(f"  {note}")
    if plan.sources:
        say("  adatforrások: " + ", ".join(dict.fromkeys(plan.sources)))
    say("\nŰrlap:")
    for key, value in form.items():
        if key in {"getUnique", "szoveg", "mindent_tud1", "mindent_tud3"} or not value:
            continue
        say(f"  {key}: {value}")
    say("Fájlok: " + ", ".join(f"{k}={v}" for k, v in files.items()))
    say("\nLeírás (" + str(run_dir / "leiras.txt") + "):\n" + "\n".join("  | " + line for line in form["szoveg"].splitlines()))
    for warning in plan.warnings:
        say(f"FIGYELEM: {warning}")
    for error in plan.errors:
        say(f"HIBA: {error}")
    for doubt in plan.doubts:
        say(f"KÉTSÉG: {doubt}")
    for item in plan.requirements:
        if item.status != "PASS":
            say(f"{item.status}: {item.message}")


if __name__ == "__main__":
    sys.exit(main())
