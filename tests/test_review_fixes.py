"""Regressions for the 2026-10-18 browser/code review; no external requests."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from ncore_up import categories, cli, duplicates, form, media, plan, release, requirements, rules, torrent, workflow
from ncore_up.meta import Meta
from test_cli_run import CliCase, FakeClient, LOCAL_TORRENT, SITE_TORRENT
from test_upload_seed import FakeQbit
from test_books_samples import png
from ncore_up.client import parse_duplicate_facts, rank_limit, NcoreClient, NcoreError, SessionExpired, UploadResult
from ncore_up import session
from test_plan_build import PlanCase, video_info, audio
from ncore_up.meta import Track


class AutoGates(CliCase):
    def test_definite_rejection_releases_reservation(self):
        root = self.book()
        client = FakeClient(result=UploadResult(ok=False, errors=["rejected"]))
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 9)
        client.result = UploadResult(ok=True, torrent_id=4242)
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 0)
        self.assertEqual(len(client.uploads), 2)

    def test_unknown_response_keeps_reservation(self):
        root = self.book()
        client = FakeClient(result=UploadResult(ok=False, uncertain=True, errors=["unknown response"]))
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 9)
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 12)
        self.assertEqual(len(client.uploads), 1)

    def test_batch_continues_and_collects_reviews(self):
        root = self.book()
        other = root.parent / "Unknown item"
        other.mkdir()
        (other / "data.blob").write_bytes(b"unknown")
        args = cli.build_parser().parse_args([str(root.parent), "--batch", "--auto", "--dry-run"])
        with mock.patch("ncore_up.resolve.books.resolve", return_value=self.book_meta(root)):
            self.assertEqual(cli.batch(args, self.config, FakeClient()), 10)
        summary = next(self.config.log_dir.glob("batch-*.json"))
        results = json.loads(summary.read_text())
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(r["exit_code"] for r in results), [0, 10])
        self.assertIn("review", next(r for r in results if r["exit_code"] == 10))

    def test_offline_preparation_never_needs_cookie_or_upload(self):
        root = self.book()
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--dry-run", client=workflow.OfflineClient(), meta=self.book_meta(root))
        self.assertEqual(code, 0)
        checks = json.loads((self.run_dir() / "requirements.json").read_text())
        self.assertEqual(next(r["status"] for r in checks if r["id"] == "online_checks"), "UNKNOWN")

    def test_required_short_sample_blocks(self):
        root = self.film()
        path = next(root.glob("*.mkv"))
        with path.open("r+b") as stream:
            stream.truncate(3 * 1024**3)
        default = media.probe.side_effect
        def probe(p):
            info = default(p)
            if "sample" in str(p).lower():
                info.duration = 10
            return info
        media.probe.side_effect = probe
        code, client = self.run_cli(root, "--auto", "--own", "--source", "BluRay videó és hang", group="video", meta=self.film_meta())
        self.assertEqual(code, 5)
        self.assertEqual(client.uploads, [])
        self.assertIn("rövidebb 1 percnél", self.output())

    def test_missing_proof_never_uploads_even_with_force(self):
        root = self.book()
        code, client = self.run_cli(root, "-c", "ebook_hun", "--auto", "--force", meta=self.book_meta(root, proof_links=[]))
        self.assertEqual(code, 5)
        self.assertEqual(client.uploads, [])
        checks = json.loads((self.run_dir() / "requirements.json").read_text())
        self.assertEqual(next(r["status"] for r in checks if r["id"] == "proof"), "FAIL")

    def test_final_description_can_supply_proof(self):
        root = self.book()
        desc = self.tmp / "description.txt"
        desc.write_text("Könyv címe: A könyv\nEgy könyv magyar leírása.\n\nIgazoló link: https://moly.hu/konyvek/a-konyv\n", encoding="utf-8")
        code, client = self.run_cli(root, "-c", "ebook_hun", "--auto", "--description", str(desc), meta=self.book_meta(root, proof_links=[]))
        self.assertEqual(code, 0)
        self.assertEqual(len(client.uploads), 1)

    def test_imdb_hit_requires_decision(self):
        root = self.film()
        client = FakeClient(imdb_hits=[{"torrent_id": 77, "release_name": "Another.Version", "category": "hd"}])
        code, _ = self.run_cli(root, "--auto", client=client, group="video", meta=self.film_meta())
        self.assertEqual(code, 10)
        self.assertEqual(client.uploads, [])

    def test_text_and_reader_can_coexist(self):
        root = self.book()
        client = FakeClient(by_name={"Szerző Elek - A könyv": [{"torrent_id": 7, "release_name": "Szerző Elek - A könyv", "category": "ebook_hun", "format": "pdf ocr"}]})
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 0)
        self.assertEqual(len(client.uploads), 1)

    def test_reader_formats_not_automatically_distinct(self):
        root = self.book()
        client = FakeClient(by_name={"Szerző Elek - A könyv": [{"torrent_id": 7, "release_name": "Szerző Elek - A könyv", "category": "ebook_hun", "format": "mobi"}]})
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 10)
        self.assertEqual(client.uploads, [])

    def test_probe_failure_is_not_success(self):
        root = self.film()
        media.probe.side_effect = media.MediaError("ffprobe failed")
        code, client = self.run_cli(root, "--auto", group="video", meta=self.film_meta())
        self.assertEqual(code, 10)
        self.assertEqual(client.uploads, [])

    def test_two_movies_are_blocked(self):
        root = self.film()
        (root / "another.mkv").write_bytes(b"film")
        code, client = self.run_cli(root, "--auto", group="video", meta=self.film_meta())
        self.assertEqual(code, 5)
        self.assertEqual(client.uploads, [])

    def test_each_series_episode_is_probed(self):
        root = self.film()
        second = root / "Show.S01E02.mkv"
        second.write_bytes(b"film")
        default = media.probe.side_effect
        media.probe.side_effect = lambda p: (_ for _ in ()).throw(media.MediaError("bad episode")) if Path(p) == second else default(p)
        code, client = self.run_cli(root, "-c", "hdser", "--auto", group="video", meta=self.film_meta())
        self.assertNotEqual(code, 0)
        self.assertEqual(client.uploads, [])
        self.assertIn("nem ellenőrizhető videó", self.output())

    def test_changed_source_cannot_upload(self):
        root = self.book()
        def mutate(*args):
            (root / "new.epub").write_bytes(b"new")
            return LOCAL_TORRENT
        self.create.side_effect = mutate
        code, client = self.run_cli(root, "-c", "ebook_hun", "--auto", meta=self.book_meta(root))
        self.assertEqual(code, 5)
        self.assertEqual(client.uploads, [])

    def test_bad_image_cannot_upload(self):
        root = self.book()
        images = []
        for i in range(3):
            p = self.tmp / f"fake{i}.png"
            p.write_bytes(b"not png")
            images.append(str(p))
        code, client = self.run_cli(root, "-c", "ebook_hun", "--auto", "--images", *images, meta=self.book_meta(root))
        self.assertEqual(code, 5)
        self.assertEqual(client.uploads, [])

    def test_resume_seed_does_not_upload_twice(self):
        root = self.book()
        self.add_torrent.side_effect = cli.qbit.QbitError("not ready")
        code, client = self.run_cli(root, "-c", "ebook_hun", "--auto", meta=self.book_meta(root))
        self.assertEqual(code, 11)
        self.add_torrent.side_effect = None
        self.assertEqual(cli.resume(self.run_dir(), self.config, client), 0)
        self.assertEqual(len(client.uploads), 1)
        self.assertEqual(self.add_torrent.call_args.kwargs["info_hash"], torrent.info_hash(SITE_TORRENT))
        self.assertFalse(self.add_torrent.call_args.kwargs["skip_checking"])

    def test_uncertain_upload_is_never_reposted(self):
        root = self.book()
        client = FakeClient()
        client.upload = mock.Mock(side_effect=requests.ConnectionError("lost response"))
        with self.assertRaises(requests.ConnectionError):
            self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        self.assertEqual(cli.resume(self.run_dir(), self.config, client), 12)
        self.assertEqual(client.upload.call_count, 1)

    def test_uncertain_upload_can_recover_by_pieces(self):
        root = self.book()
        client = FakeClient()
        client.upload = mock.Mock(side_effect=requests.ConnectionError("lost response"))
        with self.assertRaises(requests.ConnectionError):
            self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        client.by_name["Szerző Elek - A könyv"] = [{"torrent_id": 4242, "category": "ebook_hun", "release_name": "Szerző Elek - A könyv"}]
        self.assertEqual(cli.resume(self.run_dir(), self.config, client), 0)
        self.assertEqual(client.upload.call_count, 1)

    def test_completed_job_is_skipped_without_post(self):
        root = self.book()
        code, client = self.run_cli(root, "-c", "ebook_hun", "--auto", meta=self.book_meta(root))
        self.assertEqual(code, 0)
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 0)
        self.assertEqual(len(client.uploads), 1)

    def test_sidecar_and_relative_files(self):
        root = self.book()
        answers = {"category": "ebook_hun", "hints": {"language": "hu"}, "proof_links": ["https://moly.hu/konyvek/a-konyv"]}
        # a sheet next to downloaded content is never loaded (a pack could bring its own)
        root.with_name(root.name + ".ncore.json").write_text(json.dumps({"install_guide": "/etc/passwd"}), encoding="utf-8")
        with mock.patch.object(workflow, "ANSWERS_DIR", self.tmp / "answers"):
            target = workflow.answers_path(root)
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(answers), encoding="utf-8")
            code, _ = self.run_cli(root, "--auto", meta=self.book_meta(root))
        self.assertEqual(code, 0)

    def test_missing_game_installation_blocks(self):
        root = self.tmp / "Game v1.2"
        root.mkdir()
        (root / "game.exe").write_bytes(b"MZ")
        images = []
        for i in range(3):
            image = self.tmp / f"own{i}.png"
            image.write_bytes(png(400, 600))
            images.append(str(image))
        meta = Meta(title="Game Title", description_hu="Magyar játék leírása.", confidence=.95,
                    proof_links=["https://store.steampowered.com/app/400/"])
        code, client = self.run_cli(root, "-c", "game_rip", "--auto", "--images", *images, group="games", meta=meta)
        self.assertEqual(code, 5)
        self.assertEqual(client.uploads, [])
        self.assertIn("telepítési útmutató", self.output())


class CategoryMatrix(PlanCase):
    def test_all_54_variants_build_and_validate(self):
        modules = {"video": "video", "clip": "clip", "music": "music", "ebook": "books", "game": "games",
                   "program": "software", "console": "console", "mobil": "mobile"}
        for code, category in categories.CATEGORIES.items():
            if category.group == "xxx":
                continue
            for original in (False, True):
                with self.subTest(code=code, original=original):
                    group = category.group
                    ext = {"video": ".iso" if code.startswith("dvd") else ".mkv", "clip": ".mkv", "music": ".flac" if code.startswith("lossless") else ".mp3",
                           "ebook": ".epub", "game": ".iso" if code == "game_iso" else ".exe", "program": ".iso" if code == "iso" else ".exe",
                           "console": ".nsp", "mobil": ".apk"}[group]
                    name = f"Example.Title.{'S01.' if 'ser' in code else ''}2020-{code}-{'ORIG' if original else 'OWN'}"
                    files = {"content" + ext: 10}
                    if original:
                        files["release.nfo"] = b"https://imdb.com/title/tt1234567/"
                    rel = self.release(name, files)
                    self.info = video_info(width=720 if code.startswith(("xvid", "dvd")) else 1920,
                                           height=400 if code.startswith("xvid") else 576 if code.startswith("dvd") else 1080,
                                           codec="mpeg2video" if code.startswith("dvd") else "h264",
                                           audio=[audio("hun" if code.endswith("_hun") else "eng", "aac", 2, default=True)], subtitles=[])
                    meta = Meta(kind="tv" if "ser" in code else "movie" if group == "video" else "album" if group in {"music", "clip"} else "book" if group == "ebook" else "game",
                                title="Example Title", title_hu="Példacím", year="2020", creators=["Example Author"], description_hu="Egy részletes magyar leírás a kiadvány tartalmáról.",
                                identifiers={"imdb": "tt1234567"}, confidence=1, proof_links=["https://example.com/official"],
                                install_hu="1. Futtasd a telepítőt.\n2. Indítsd el a programot.", requirements="Windows 10 vagy a megadott konzol", region="Régiófüggetlen", languages=["magyar" if code.endswith("_hun") else "angol"],
                                tags=["szépirodalom" if group == "ebook" else "live" if group == "clip" else "rock"],
                                tracks=[Track("1", "First track")], extra={"format": "EPUB" if group == "ebook" else "FLAC" if code.startswith("lossless") else "MP3 320 kbps",
                                                                         "main_file": str(rel.path / ("content" + ext)),
                                                                         "min_os":"Android 8.0", "required_system":"A csomaghoz tartozó konzol"})
                    images = self.images() if group in {"game", "program", "console", "mobil"} else []
                    extracted = self.tmp / "analysis.vob"
                    extracted.write_bytes(b"probe")
                    with mock.patch.object(media, "dvd_image_stream", return_value=extracted):
                        built, _ = self.build(rel, modules[group], meta, None, category=code, original=original,
                                              source="Ellenőrzött videó és hangforrás", extras="nincs", menu=True, images=images)
                    self.assertEqual(built.category.code, code)
                    self.assertEqual(self.missing(built), [])
                    if code in {"dvd", "dvd_hun"}:
                        self.assertTrue(any("régi Film/DVD" in message for message in built.errors))
                        self.assertFalse(built.auto_ok)
                    else:
                        self.assertEqual(built.errors, [])
                        self.assertTrue(built.auto_ok, [r.as_dict() for r in built.requirements if r.status != "PASS"])


class RuleAndWorkflowTests(unittest.TestCase):
    def test_missing_default_hun_is_error(self):
        info = media.VideoInfo(Path("x"), codec="h264", width=1920, height=1080, audio=[{"lang": "hun", "format": "aac", "default": False}])
        errors, _ = rules.video(info, "hd_hun", remux=False, original=True, foreign_original=False)
        self.assertTrue(any("alapértelmezett" in e for e in errors))

    def test_hun_original_17_tracks_is_error(self):
        info = media.VideoInfo(Path("x"), codec="h264", width=1920, height=1080, subtitles=[{"lang": "eng"}] * 16)
        errors, _ = rules.video(info, "hd_hun", remux=False, original=True, foreign_original=False)
        self.assertTrue(any("16 sáv" in e for e in errors))

    def test_foreign_original_cannot_bypass_codec(self):
        info = media.VideoInfo(Path("x"), codec="mpeg4", width=1920, height=1080)
        errors, _ = rules.video(info, "hd", remux=False, original=True, foreign_original=True)
        self.assertTrue(any("H.264" in e for e in errors))

    def test_explicit_delay_is_error(self):
        info = media.VideoInfo(Path("x"), codec="h264", width=1920, height=1080, audio=[{"lang": "eng", "format": "aac", "delay_ms": -201}])
        errors, _ = rules.video(info, "hd", remux=False, original=True, foreign_original=True)
        self.assertTrue(any("200 ms" in e for e in errors))

    def test_foreign_exception_is_rule_specific(self):
        audio = [{"lang": "und", "format": "truehd", "channels": 8}]
        info = media.VideoInfo(Path("x"), codec="h264", width=1920, height=1080, audio=audio)
        errors, _ = rules.video(info, "hd", remux=False, original=True, foreign_original=True, source="Movie.BluRay.1080p")
        self.assertTrue(any("nyelvi címke" in e for e in errors))
        self.assertFalse(any("kompatibilitási" in e for e in errors))
        self.assertFalse(any("hangformátum" in e for e in errors))
        errors, _ = rules.video(info, "hd", remux=False, original=True, foreign_original=True, source="Movie.WEB-DL.1080p")
        self.assertTrue(any("hangformátum" in e for e in errors))

    def test_aac_multichannel_not_exempt(self):
        info = media.VideoInfo(Path("x"), codec="h264", width=1280, height=720, audio=[{"lang": "eng", "format": "aac", "channels": 6}])
        errors, _ = rules.video(info, "hd", remux=False, original=True, foreign_original=True)
        self.assertTrue(any("AAC hang" in e for e in errors))

    def test_mobile_console_must_be_unpacked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "release.nfo").write_bytes(b"nfo")
            (root / "content.rar").write_bytes(b"rar")
            rel = release.inspect(root)
            for code in ("console", "mobil", "game_iso"):
                self.assertTrue(any("ki kell bontani" in e for e in release.validate_original(rel, categories.CATEGORIES[code])))

    def test_new_category_signatures(self):
        for name, ext, expected in [("App", ".apk", "mobil"), ("Game", ".nsp", "console"), ("Book", ".m4b", "mp3"),
                                     ("Movie.DVD5", ".iso", "dvd9"), ("Book.HUN", ".prc", "ebook_hun")]:
            with self.subTest(name=name):
                self.assertEqual(categories.detect(name, [ext], False, False, None).code, expected)

    def test_book_facts_from_live_dom_structure(self):
        html = '<div class="torrent_leiras proba42"><span>Formátum</span>: pdf ocr<br>Nyelv: magyar<br>Kiadás dátuma: 2025<br><a href="?key=secret">download</a></div>'
        self.assertEqual(parse_duplicate_facts(html), {"format": "pdf ocr", "language": "magyar", "edition": "2025"})

    def test_download_network_error_does_not_expose_passkey(self):
        client = NcoreClient([])
        client.session.get = mock.Mock(side_effect=requests.ConnectionError("https://ncore.pro/torrents.php?key=PRIVATE_KEY"))
        with self.assertRaises(NcoreError) as ctx:
            client.download_torrent(1, "PRIVATE_KEY")
        self.assertNotIn("PRIVATE_KEY", str(ctx.exception))

    def test_rank_budget(self):
        for rank, limit in (("User", 5), ("Tag", 10), ("Elite", 20), ("Legend", 30), ("VIP", 1_000_000_000)):
            self.assertEqual(rank_limit('<div id="infosav_adatok">user (' + rank + ')</div>'), limit)
        self.assertEqual(rank_limit('<body>Legend</body>'), 5)

    def test_dvd5_iso_size_and_disc_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "Movie.DVD.iso"
            image.write_bytes(b"iso")
            rel = release.inspect(image)
            self.assertEqual(plan._local_category(rel, "dvd9", {}), "dvd9")
            rel.files[0].size = 6_000_000_000
            self.assertEqual(plan._local_category(rel, "dvd", {}), "dvd9")

    def test_cookie_import_filters_domains_and_preserves_expired_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, destination = root / "source.txt", root / "private" / "cookies.txt"
            source.write_text('# Netscape HTTP Cookie File\n.ncore.pro\tTRUE\t/\tTRUE\t0\tuid\tFAKE\n.example.com\tTRUE\t/\tTRUE\t0\tother\tFAKE\n')
            with mock.patch.object(session.NcoreClient, "upload_token", return_value="token"):
                session.import_cookies(source, destination)
            self.assertNotIn("example.com", destination.read_text())
            before = destination.read_bytes()
            with mock.patch.object(session.NcoreClient, "upload_token", side_effect=SessionExpired("expired")), self.assertRaises(SessionExpired):
                session.import_cookies(source, destination)
            self.assertEqual(destination.read_bytes(), before)
            if os.name != "nt":
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_dvd_entry_listing_is_static(self):
        text = 'Path = VIDEO_TS/VTS_01_1.VOB\nSize = 1024\n\nPath = run.exe\nSize = 90\n'
        import subprocess
        with mock.patch.object(media.shutil, "which", return_value="7z"), mock.patch.object(media, "_run", return_value=subprocess.CompletedProcess([], 0, text.encode(), b"")):
            self.assertEqual(media.dvd_image_entries(Path("movie.iso")), [("VIDEO_TS/VTS_01_1.VOB", 1024)])

    def test_proof_rejects_forbidden_domains(self):
        for url in ("https://en.wikipedia.org/wiki/X", "https://youtube.com/x", "https://example.com/forum/x", "https://videa.hu/x"):
            self.assertEqual(requirements.proof_urls("Igazoló link: " + url), [])

    def test_staging_reflects_replaced_and_removed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Book"
            root.mkdir()
            (root / "a.epub").write_bytes(b"old")
            (root / "gone.srt").write_bytes(b"sub")
            def stage():
                rel = release.inspect(root)
                return plan.Plan(rel, categories.CATEGORIES["ebook"], form.UploadData("ebook", "Book", False), options=plan.Options(ncup_dir=Path(tmp) / "NCUP")).stage_dir()
            first = stage()
            (root / "a.epub").unlink()
            (root / "a.epub").write_bytes(b"new")
            (root / "gone.srt").unlink()
            second = stage()
            self.assertEqual(first, second)  # the never uploaded leftover is rebuilt from the current source
            self.assertEqual((second / "a.epub").read_bytes(), b"new")
            self.assertFalse((second / "gone.srt").exists())

    def test_ledger_reservations_and_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = workflow.Ledger(Path(tmp))
            try:
                ledger.reserve("one", Path(tmp) / "one", 1)
                with self.assertRaisesRegex(ValueError, "megkezdett"):
                    ledger.reserve("one", Path(tmp) / "other", 1)
                with self.assertRaisesRegex(ValueError, "korlát"):
                    ledger.reserve("two", Path(tmp) / "two", 1)
            finally:
                ledger.close()

    def test_stopped_checking_and_incomplete_are_not_seeding(self):
        for state in ("stoppedUP", "pausedUP", "checkingUP", "downloading", "missingFiles"):
            with self.subTest(state=state), self.assertRaises(cli.qbit.QbitError):
                cli.qbit.add_torrent("http://qb", "", "", LOCAL_TORRENT, "/data", False,
                                     info_hash="ab" * 20, wait=0, session=FakeQbit(items=[{"state": state, "progress": .5}]))

    def test_all_non_xxx_categories_have_builders(self):
        self.assertEqual(len([c for c in categories.CATEGORIES.values() if c.group != "xxx"]), 27)
        for code, cat in categories.CATEGORIES.items():
            with self.subTest(code=code):
                if cat.group == "xxx":
                    with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "XXX"):
                        plan.build(release.inspect(tmp), plan.Options(category=code), None, Path(tmp) / "out")
                else:
                    self.assertIn(cat.group, {"video", "clip", "music", "ebook", "game", "console", "mobil", "program"})
