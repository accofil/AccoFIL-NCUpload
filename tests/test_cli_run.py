"""cli.run end to end with a fake nCore client: exit codes, run-log files, the automatic mode and the upload itself.

The plan is built for real (plan.build) with a stub resolver and fake media tools; mkbrr, qBittorrent and the
interactive prompt are mocked, nothing leaves the machine.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import banlist, cli, media, net, qbit, torrent
from ncore_up import plan as plans
from ncore_up import release as rel
from ncore_up.client import UploadResult
from ncore_up.config import Config
from ncore_up.local import ebookmeta
from ncore_up.meta import Meta
from test_books_samples import png

BOOK = "Szerzo Elek - A konyv (2020)"
FILM = "Some.Film.2021.1080p.BluRay.x264-GRP"


def torrent_bytes(announce: str) -> bytes:
    info = b"d6:lengthi5e4:name3:abc12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"e"
    return b"d8:announce" + str(len(announce)).encode() + b":" + announce.encode() + b"4:info" + info + b"e"


LOCAL_TORRENT = torrent_bytes("https://ncore.pro/announce")
SITE_TORRENT = torrent_bytes("https://t.ncore.pro/announce.php?passkey=" + "f" * 32)


class FakeClient:
    def __init__(self, *, by_name: dict | None = None, imdb_hits: list | None = None, banned: str = "", result: UploadResult | None = None,
                 site: bytes = SITE_TORRENT):
        self.by_name = by_name or {}
        self.imdb_hits = imdb_hits or []
        self.banned = banned
        self.result = result or UploadResult(ok=True, torrent_id=4242, url="https://ncore.pro/torrents.php?action=details&id=4242", body="<html>ok</html>")
        self.site = site
        self.names: list[str] = []
        self.uploads: list[tuple[dict, dict]] = []
        self.downloads: list[int] = []

    def exact_matches(self, name):
        self.names.append(name)
        return list(self.by_name.get(name, []))

    def search(self, text, by="name", categories=None):
        return list(self.imdb_hits) if by == "imdb" else []

    def upload_token(self):
        return "0123456789abcdef0123"

    def banlist_page(self):
        return (Path(__file__).parent / "fixtures/banlist/wiki41.html").read_text(encoding="utf-8")

    def banned_notice(self, token, category, name, **kwargs):
        return self.banned

    def imdb_movie(self, imdb_id):
        return {}

    def upload(self, form, files):
        self.uploads.append((form, files))
        return self.result

    def passkey(self, torrent_id):
        return "f" * 32

    def download_torrent(self, torrent_id, passkey):
        self.downloads.append(torrent_id)
        return self.site


class CliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.config = Config(cookies=str(self.tmp / "cookies.txt"), qb_url="http://127.0.0.1:8080", qb_user="u", qb_pass="p", qb_category="ncore",
                             mkbrr="mkbrr", log_dir=self.tmp / "logs", work_dir=self.tmp / "cache", ncup_dir=self.tmp / "NCUP")
        banlist.refresh(FakeClient(), self.config.work_dir)
        self.out: list[str] = []
        self.pages = 3
        saved = list(net.FAILURES)
        net.FAILURES.clear()
        self.addCleanup(lambda: net.FAILURES.__setitem__(slice(None), saved))
        self.create = mock.Mock(return_value=LOCAL_TORRENT)
        self.add_torrent = mock.Mock(return_value="stalledUP")
        self.input = mock.Mock(side_effect=AssertionError("no prompt expected"))
        for target, name, value in (
            (cli, "say", mock.Mock(side_effect=lambda text="": self.out.append(text))),
            (rel, "hungarian_audio", mock.Mock(return_value=None)),
            (media, "probe", mock.Mock(side_effect=lambda path: media.VideoInfo(path=Path(path), duration=7200, width=1920, height=1080, codec="h264",
                 audio=[{"lang": "hun" if "HUN" in str(path).upper() else "eng", "format": "aac", "channels": 2, "default": True}]))),
            (media, "mediainfo_text", mock.Mock(side_effect=lambda path: f"MEDIAINFO {Path(path).name}\n")),
            (media, "screenshots", mock.Mock(side_effect=self._screenshots)),
            (media, "infobar_cover", mock.Mock(side_effect=self._cover)),
            (net, "get_bytes", mock.Mock(return_value=b"")),
            (ebookmeta, "proof_pages", mock.Mock(side_effect=self._pages)),
            (torrent, "create", self.create),
            (qbit, "add_torrent", self.add_torrent),
            (qbit, "incomplete_at", mock.Mock(return_value=[])),
            (cli, "input", self.input),
        ):
            patcher = mock.patch.object(target, name, value, create=name == "input")
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _screenshots(video, out_dir, count=3, image_format="png"):
        out_dir.mkdir(parents=True, exist_ok=True)
        shots = [out_dir / f"screen{i}.png" for i in range(1, count + 1)]
        for shot in shots:
            shot.write_bytes(png(1920, 1080))
        return shots

    @staticmethod
    def _cover(image, out, limit=300):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(image)
        return out

    def _pages(self, path, out_dir, count=3, warnings=None, layer=""):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = [out_dir / f"oldal{i}.png" for i in range(1, self.pages + 1)]
        for index, page in enumerate(pages):
            page.write_bytes(png(400, 600, shade=200-index))
        return pages

    # --- releases and resolvers ---

    def book(self) -> Path:
        root = self.tmp / "data" / BOOK
        root.mkdir(parents=True)
        (root / "a_konyv.epub").write_bytes(b"x" * 10)
        return root

    def book_meta(self, root: Path, **changes) -> Meta:
        meta = Meta(kind="book", title="A könyv", creators=["Szerző Elek"], year="2020", tags=["szépirodalom"], cover=b"IMG", confidence=0.9,
                    languages=["magyar"], proof_links=["https://moly.hu/konyvek/a-konyv"], extra={"format": "EPUB", "torrent_name": "Szerző Elek - A könyv",
                                                                           "main_file": str(root / "a_konyv.epub")})
        for key, value in changes.items():
            setattr(meta, key, value)
        return meta

    def film(self) -> Path:
        root = self.tmp / "data" / FILM
        (root / "Sample").mkdir(parents=True)
        (root / f"{FILM.lower()}.mkv").write_bytes(b"x" * 10)
        (root / "Sample" / "sample.mkv").write_bytes(b"x" * 3)
        (root / f"{FILM.lower()}.nfo").write_bytes(b"https://www.imdb.com/title/tt7654321/\n")
        return root

    @staticmethod
    def film_meta() -> Meta:
        return Meta(kind="movie", title="Some Film", year="2021", identifiers={"imdb": "tt7654321"}, confidence=1.0, cover=b"IMG",
                    description_hu="Magyar tartalom.")

    def run_cli(self, path: Path, *flags: str, client: FakeClient | None = None, group: str = "books", meta: Meta | None = None):
        args = cli.build_parser().parse_args([str(path), *flags])
        client = client or FakeClient()
        with mock.patch(f"ncore_up.resolve.{group}.resolve", return_value=meta or Meta()):
            code = cli.run(args, self.config, client)
        return code, client

    def run_dir(self) -> Path:
        dirs = list(self.config.log_dir.iterdir())
        self.assertEqual(len(dirs), 1)
        return dirs[0]

    def output(self) -> str:
        return "\n".join(self.out)


class ExitCodeTests(CliCase):
    def test_already_on_ncore_exit_6(self):
        root = self.book()
        client = FakeClient(by_name={BOOK: [{"torrent_id": 7, "release_name": BOOK, "category": "ebook_hun"}]})
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--auto", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 10)  # name alone cannot settle a book-format dupe
        self.assertEqual(client.uploads, [])
        self.assertTrue((self.run_dir() / "duplicates.json").exists())

    def test_missing_path_exit_2(self):
        code = cli.run(cli.build_parser().parse_args([str(self.tmp / "nincs")]), self.config, FakeClient())
        self.assertEqual(code, 2)
        self.assertIn("HIBA: nincs ilyen fájl vagy mappa", self.output())

    def test_dry_run_exit_0_writes_the_log(self):
        root = self.book()
        code, client = self.run_cli(root, "-c", "ebook_hun", "--dry-run", meta=self.book_meta(root))
        self.assertEqual(code, 0)
        run_dir = self.run_dir()
        self.assertTrue(run_dir.name.endswith("-" + BOOK))
        description = (run_dir / "leiras.txt").read_text(encoding="utf-8")
        self.assertTrue(description.startswith("Könyv címe: A könyv\n"))
        form = json.loads((run_dir / "form.json").read_text(encoding="utf-8"))
        self.assertNotIn("getUnique", form)
        self.assertEqual({k: form[k] for k in ("tipus", "torrent_nev", "eredeti", "konyv_cime", "ebook_style", "szoveg")},
                         {"tipus": "ebook_hun", "torrent_nev": "Szerző Elek - A könyv", "eredeti": "nem", "konyv_cime": "A könyv",
                          "ebook_style": "szépirodalom", "szoveg": description})
        self.assertEqual(client.uploads, [])
        self.create.assert_not_called()
        self.assertEqual(client.names, [BOOK, "Szerző Elek - A könyv"])  # release name, then the planned name
        self.assertIn("Szárazpróba: nem töltöttem fel.", self.output())
        self.assertFalse((run_dir / "atnezesre.txt").exists())

    def test_dry_run_writes_techinfo(self):
        root = self.tmp / "data" / "The.Long.Night.2020.1080p.WEB-DL.H.264.HUN"
        root.mkdir(parents=True)
        (root / "the.long.night.mkv").write_bytes(b"x" * 10)
        meta = Meta(kind="movie", title="The Long Night", title_hu="A hosszú éjszaka", identifiers={"imdb": "tt1111111"}, confidence=1.0,
                    description_hu="Ellenőrzött magyar filmismertető az offline tesztben.")
        client = FakeClient(imdb_hits=[{"torrent_id": 3, "release_name": "The.Long.Night.2020.720p-X", "category": "hd_hun"}])
        code, _ = self.run_cli(root, "--dry-run", client=client, group="video", meta=meta)
        self.assertEqual(code, 0)
        self.assertEqual((self.run_dir() / "techinfo_fajl.txt").read_text(encoding="utf-8"), "MEDIAINFO the.long.night.mkv\n")
        self.assertIn("azonos IMDb-vel 1 torrent van fent", self.output())
        self.assertIn("Fájlok: kep1=screen1.png, kep2=screen2.png, kep3=screen3.png, techinfo_fajl=techinfo_fajl.txt, torrent_fajl=(készül)", self.output())

    def test_plan_errors_exit_5(self):
        root = self.book()
        self.pages = 0
        code, client = self.run_cli(root, "-c", "ebook_hun", "--yes", meta=self.book_meta(root))
        self.assertEqual(code, 5)
        self.assertIn("HIBA: 3 kép kell", self.output())
        self.assertEqual(client.uploads, [])
        self.create.assert_not_called()
        self.assertTrue((self.run_dir() / "form.json").exists())
        self.assertTrue((self.run_dir() / "atnezesre.txt").exists())

    def test_force_turns_errors_into_warnings(self):
        root = self.book()
        self.pages = 0
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--dry-run", "--force", meta=self.book_meta(root))
        self.assertEqual(code, 5)  # missing mandatory images cannot be forced

    def test_same_name_and_banned_are_errors(self):
        root = self.book()
        client = FakeClient(by_name={"Szerző Elek - A könyv": [{"torrent_id": 9, "release_name": "x", "category": "ebook_hun"}]},
                            banned="Tiltott anyag: A könyv")
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--dry-run", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 5)
        self.assertIn("dupe #9", (self.run_dir() / "review.json").read_text(encoding="utf-8") if (self.run_dir() / "review.json").exists() else self.output())
        self.assertIn("HIBA: tiltott anyag találat az nCore szerint: Tiltott anyag: A könyv", self.output())

    def test_auto_with_errors_sets_aside_exit_5(self):
        root = self.book()
        self.pages = 0
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--auto", meta=self.book_meta(root))
        self.assertEqual(code, 5)
        aside = (self.run_dir() / "atnezesre.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(aside[0], "biztonság: 0.90")
        self.assertIn("HIBA: 3 kép kell", aside)

    def test_auto_low_confidence_exit_10(self):
        root = self.book()
        code, client = self.run_cli(root, "-c", "ebook_hun", "--auto", meta=self.book_meta(root, confidence=0.5, warnings=["bizonytalan cím"]))
        self.assertEqual(code, 10)
        aside = (self.run_dir() / "atnezesre.txt").read_text(encoding="utf-8")
        self.assertEqual(aside, "biztonság: 0.50\nFIGYELEM: bizonytalan cím\n")
        self.assertIn("az azonosítás nem elég biztos (0.50)", self.output())
        self.assertEqual(client.uploads, [])
        self.create.assert_not_called()

    def test_auto_doubt_exit_10(self):
        root = self.book()
        code, client = self.run_cli(root, "-c", "ebook", "--auto", meta=self.book_meta(root, languages=["magyar"]))
        self.assertEqual(code, 10)
        aside = (self.run_dir() / "atnezesre.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("KÉTSÉG: e-könyv: a nyelv (magyar) nem illik a kategóriához (ebook)", aside)
        self.assertIn("KÉTSÉG: e-könyv: a nyelv (magyar)", self.output())
        self.assertEqual(client.uploads, [])
        # without --auto the user decides: a doubt is only a warning
        code, _ = self.run_cli(root, "-c", "ebook", "--dry-run", meta=self.book_meta(root, languages=["magyar"]))
        self.assertEqual(code, 0)

    def test_auto_force_never_uploads_errors(self):
        root = self.book()
        self.pages = 0
        code, client = self.run_cli(root, "-c", "ebook_hun", "--auto", "--force", meta=self.book_meta(root))
        self.assertEqual(code, 5)
        self.assertIn("FAIL:", self.output())
        self.assertEqual(client.uploads, [])

    def test_mkbrr_failure_exit_2(self):
        root = self.book()
        self.create.side_effect = FileNotFoundError("mkbrr")
        code, client = self.run_cli(root, "-c", "ebook_hun", "--yes", meta=self.book_meta(root))
        self.assertEqual(code, 2)
        self.assertIn("HIBA: a torrent nem készült el", self.output())
        self.assertEqual(client.uploads, [])

    def test_upload_refused_exit_9(self):
        root = self.book()
        client = FakeClient(result=UploadResult(ok=False, url="https://ncore.pro/upload.php", errors=["Nem adtál meg torrent fájlt!"], body="<html>hiba</html>"))
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--yes", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 9)
        self.assertEqual((self.run_dir() / "response.html").read_text(encoding="utf-8"), "<html>hiba</html>")
        self.assertIn("  - Nem adtál meg torrent fájlt!", self.output())
        self.add_torrent.assert_not_called()


class UploadTests(CliCase):
    def test_changed_prepared_payload_never_posts(self):
        root = self.book()
        (root / "extra.txt").write_bytes(b"advert")
        def changed(mkbrr, content, target):
            self.assertTrue(content.is_relative_to(self.config.ncup_dir))
            (content / "unexpected.txt").write_bytes(b"changed after preparation")
            return LOCAL_TORRENT
        self.create.side_effect = changed
        client = FakeClient()
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--yes", client=client, meta=self.book_meta(root))
        self.assertEqual(code, 5)
        self.assertFalse(client.uploads)
        self.assertFalse((root / "unexpected.txt").exists())
        self.add_torrent.assert_not_called()

    def test_saved_response_redacts_tracker_credentials(self):
        root=self.book()
        body='<input value="fake-private-token" name="getUnique"><a href="?id=4242&amp;key=fake-private-passkey">download</a>'
        client=FakeClient(result=UploadResult(ok=True,torrent_id=4242,url='https://ncore.pro/torrents.php?action=details&id=4242',body=body))
        code,_=self.run_cli(root,'-c','ebook_hun','--auto',client=client,meta=self.book_meta(root))
        self.assertEqual(code,0)
        saved=(self.run_dir()/'response.html').read_text(encoding='utf-8')
        self.assertNotIn('fake-private-token',saved)
        self.assertNotIn('fake-private-passkey',saved)
        self.assertIn('id=4242',saved)

    def test_auto_upload_sends_the_multipart_files_and_seeds(self):
        root = self.book()
        code, client = self.run_cli(root, "-c", "ebook_hun", "--auto", meta=self.book_meta(root))
        self.assertEqual(code, 0)
        self.input.assert_not_called()
        self.create.assert_called_once()
        mkbrr, content, target = self.create.call_args.args
        # the torrent is made from the own hardlinked NCUP copy, which carries the release's own name
        self.assertEqual((mkbrr, content.name, target), ("mkbrr", root.name, self.run_dir() / "local.torrent"))
        self.assertTrue(content.is_relative_to(self.config.ncup_dir))
        run_dir = self.run_dir()
        (form, files), = client.uploads
        saved = json.loads((run_dir / "form.json").read_text(encoding="utf-8"))
        self.assertEqual(form, {"getUnique": "0123456789abcdef0123", **saved})
        self.assertEqual(sorted(files), ["infobar_kep", "kep1", "kep2", "kep3", "torrent_fajl"])
        self.assertEqual(files["torrent_fajl"], (f"{BOOK}.torrent", LOCAL_TORRENT, "application/x-bittorrent"))
        self.assertEqual(files["kep1"], ("oldal1.png", png(400, 600), "image/png"))
        self.assertEqual(files["infobar_kep"], ("borito.jpg", b"IMG", "image/jpeg"))
        self.assertEqual((run_dir / "result.txt").read_text(encoding="utf-8"),
                         "torrent_id=4242\nurl=https://ncore.pro/torrents.php?action=details&id=4242\n")
        self.assertEqual((run_dir / "ncore-4242.torrent").read_bytes(), SITE_TORRENT)
        self.assertEqual(client.downloads, [4242])
        content = self.create.call_args.args[1]
        self.assertTrue(content.exists())  # the uploaded job is kept: it is what qBittorrent seeds
        self.add_torrent.assert_called_once_with("http://127.0.0.1:8080", "u", "p", SITE_TORRENT, str(content.parent),
                                                 skip_checking=False, category="ncore", tags="ncupload",
                                                 info_hash=torrent.info_hash(SITE_TORRENT), wait=1200.0)
        self.assertIn("Feltöltve: https://ncore.pro/torrents.php?action=details&id=4242", self.output())

    def test_original_film_upload_files(self):
        root = self.film()
        code, client = self.run_cli(root, "--yes", group="video", meta=self.film_meta())
        self.assertEqual(code, 0)
        (form, files), = client.uploads
        self.assertEqual((form["tipus"], form["eredeti"], form["torrent_nev"], form["imdb_id"]), ("hd", "igen", FILM, "tt7654321"))
        self.assertEqual(sorted(files), ["infobar_kep", "kep1", "kep2", "kep3", "nfo_fajl", "torrent_fajl"])
        self.assertEqual(files["nfo_fajl"][0], f"{FILM.lower()}.nfo")
        self.assertEqual(files["nfo_fajl"][1], b"https://www.imdb.com/title/tt7654321/\n")
        self.assertEqual(client.names, [FILM])  # an original keeps its name: no second name search

    def test_own_film_upload_sends_techinfo(self):
        root = self.tmp / "data" / "The.Long.Night.2020.1080p.WEB-DL.H.264.HUN"
        root.mkdir(parents=True)
        (root / "the.long.night.mkv").write_bytes(b"x" * 10)
        from test_upload_seed import bencode
        info = {b"name": root.name.encode(), b"files": [{b"path": [b"the.long.night.mkv"], b"length": 10}],
                b"piece length": 16384, b"pieces": b"x" * 20}
        self.create.return_value = bencode({b"announce": b"https://ncore.pro/announce", b"info": info})
        client = FakeClient(site=bencode({b"announce": b"https://t.ncore.pro/announce", b"info": info}))
        meta = Meta(kind="movie", title="The Long Night", title_hu="A hosszú éjszaka", identifiers={"imdb": "tt1111111"}, confidence=1.0)
        code, client = self.run_cli(root, "--yes", "--source", "Hivatalos WEB-DL, eredeti hang", group="video", meta=meta, client=client)
        self.assertEqual(code, 0)
        (form, files), = client.uploads
        self.assertEqual(form["torrent_nev"], "A hosszú éjszaka 1080p")
        self.assertEqual(files["techinfo_fajl"], (f"{root.name}.txt", b"MEDIAINFO the.long.night.mkv\n", "text/plain"))
        self.assertNotIn("nfo_fajl", files)

    def test_prompt(self):
        root = self.book()
        self.input.side_effect = None
        self.input.return_value = "n"
        code, client = self.run_cli(root, "-c", "ebook_hun", meta=self.book_meta(root))
        self.assertEqual(code, 0)
        self.assertEqual(client.uploads, [])
        self.assertIn("Kihagyva.", self.output())
        self.input.return_value = "i"
        code, client = self.run_cli(root, "-c", "ebook_hun", meta=self.book_meta(root))
        self.assertEqual(code, 0)
        self.assertEqual(len(client.uploads), 1)

    def test_no_seed(self):
        root = self.book()
        code, client = self.run_cli(root, "-c", "ebook_hun", "--yes", "--no-seed", meta=self.book_meta(root))
        self.assertEqual(code, 0)
        self.assertEqual(len(client.uploads), 1)
        self.assertEqual(client.downloads, [])
        self.add_torrent.assert_not_called()
        self.assertIn("30 percen belül el kell indítani", self.output())

    def test_other_info_hash_rechecks(self):
        root = self.book()
        other = b"d8:announce1:x4:infod6:lengthi6e4:name3:abd12:piece lengthi16384e6:pieces20:" + b"y" * 20 + b"ee"
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--yes", client=FakeClient(site=other), meta=self.book_meta(root))
        self.assertEqual(code, 11)
        self.add_torrent.assert_not_called()  # changed pieces are not the same torrent

    def test_seeding_failure_after_upload_exit_11(self):
        root = self.book()
        self.add_torrent.side_effect = qbit.QbitError("qBittorrent bejelentkezés sikertelen: 403")
        code, _ = self.run_cli(root, "-c", "ebook_hun", "--yes", meta=self.book_meta(root))
        self.assertEqual(code, 11)
        self.assertIn("feltöltve, de a seedelés nem indult el: qBittorrent bejelentkezés sikertelen: 403", self.output())
        self.assertTrue((self.run_dir() / "ncore-4242.torrent").exists())

    def test_staged_sample_is_what_gets_seeded(self):
        root = self.book()
        stage = self.tmp / "data" / ".ncore-up" / BOOK
        stage.mkdir(parents=True)
        real_build = plans.build

        def build(release, options, client, work_dir):
            plan = real_build(release, options, client, work_dir)
            plan.content = stage
            return plan

        with mock.patch.object(plans, "build", side_effect=build):
            code, _ = self.run_cli(root, "-c", "ebook_hun", "--yes", meta=self.book_meta(root))
        self.assertEqual(code, 0)
        self.assertEqual(self.create.call_args.args[1], stage)
        self.assertEqual(self.add_torrent.call_args.args[4], str(stage.parent))


class ArgumentTests(CliCase):
    def test_options_from_the_command_line(self):
        root = self.book()
        args = cli.build_parser().parse_args([str(root), "-c", "hd", "--own", "--menu", "nincs", "--tags", "rock, pop ,", "--images", "a", "b", "c",
                                              "--no-make-sample", "--no-screens", "--request", "55", "--title", "T", "--version-hint", "1.2",
                                              "--package", "com.x", "--imdb", "tt1", "--other-link", "https://port.hu/x"])
        with mock.patch.object(plans, "build", side_effect=ValueError("stop")) as build:
            self.assertEqual(cli.run(args, self.config, FakeClient()), 2)
        options = build.call_args.args[1]
        self.assertEqual((options.category, options.original, options.menu, options.tags, options.images), ("hd", False, False, ["rock", "pop"], ["a", "b", "c"]))
        self.assertEqual((options.make_sample, options.screenshots, options.request_id, options.imdb, options.other_link),
                         (False, False, "55", "tt1", "https://port.hu/x"))
        self.assertEqual(options.hints, {"title": "T", "version": "1.2", "package": "com.x"})
        defaults = cli.build_parser().parse_args([str(root)])
        self.assertEqual(cli._hint_args(defaults), {})
        self.assertIsNone(defaults.original)

    def test_categories_listing_skips_xxx(self):
        self.assertEqual(cli.main(["--categories"]), 0)
        self.assertTrue(any(line.startswith("hd_hun") for line in self.out))
        self.assertFalse(any(line.startswith("xxx") for line in self.out))


if __name__ == "__main__":
    unittest.main()
