"""NFO metadata applies to own uploads too; reading it never implies an original upload."""
from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ncore_up import categories, plan, release as rel, safeio
from ncore_up.local import audiotags, consoleid, gamefiles, nfo
from ncore_up.meta import Meta
from ncore_up.resolve import books, clip, console, games, mobile, music, software, video
from test_plan_build import PlanCase


class NfoReadingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.object(rel, "hungarian_audio", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def test_single_file_matched_sidecar_case_insensitive(self):
        content = self.write("Movie.mkv", b"fixture")
        self.write("MOVIE.NFO", "Title: The Movie\nhttps://imdb.com/title/tt1234567/")
        self.write("Another.nfo", "https://imdb.com/title/tt9999999/")
        item = rel.inspect(content)
        self.assertEqual(item.imdb_id, "tt1234567")
        self.assertEqual(nfo.value(item, "title"), "The Movie")
        self.assertEqual([f.relpath for f in item.files], ["Movie.mkv"])
        self.assertIsNone(item.nfo)
        self.assertFalse(plan.looks_original(item))

    def test_all_depths_and_multiple_root_nfos(self):
        self.write("a.nfo", "Title: Example\n")
        self.write("b.nfo", "Year: 1999\n")
        self.write("CD1/docs/extra.nfo", "Genre: Rock\n")
        item = rel.inspect(self.root)
        self.assertEqual(len(item.nfo_data["files"]), 3)
        self.assertEqual(nfo.value(item, "title"), "Example")
        self.assertEqual(nfo.year(item), "1999")
        self.assertFalse(item.problems)
        self.assertTrue(any("több NFO" in p for p in rel.validate_original(item, categories.CATEGORIES["mp3"])))

    def test_snapshot_reads_once(self):
        self.write("info.nfo", "Artist: Artist\nAlbum: Album\nISBN: 9789631420499\n")
        with mock.patch.object(safeio, "read_file", wraps=safeio.read_file) as read:
            item = rel.inspect(self.root)
            nfo.read_release(item)
            nfo.value(item, "artist")
            self.assertEqual(read.call_count, 1)
        self.assertIn("isbn", item.nfo_data["fields"])

    def test_conflicts_not_first_wins(self):
        self.write("a.nfo", "Title: First\nhttps://imdb.com/title/tt1234567/\n")
        self.write("b.nfo", "Title: Second\nhttps://imdb.com/title/tt7654321/\n")
        item = rel.inspect(self.root)
        self.assertEqual(nfo.value(item, "title"), "")
        self.assertIsNone(item.imdb_id)
        meta = Meta(confidence=0.9)
        nfo.annotate(meta, item)
        self.assertIn("IMDb", item.nfo_data["conflicts"])
        self.assertTrue(meta.extra["review_reasons"])
        self.assertEqual(meta.confidence, 0.9)

    def test_pre_date_not_publication_year(self):
        self.write("info.nfo", "Release Date: 2026-10-18\n")
        self.assertEqual(nfo.year(rel.inspect(self.root)), "")

    def test_utf16_and_hungarian_labels(self):
        self.write("info.nfo", "Cím: Árvíztűrő\nSzerző: Teszt Elek\nÉv: 2001\n".encode("utf-16"))
        item = rel.inspect(self.root)
        self.assertEqual(nfo.value(item, "cím"), "Árvíztűrő")
        self.assertEqual(nfo.year(item), "2001")

    def test_large_nfo_warns_without_partial_parsing(self):
        self.write("info.nfo", b"Title: Fake\n" + b"x" * 2_000_001)
        item = rel.inspect(self.root)
        self.assertEqual(nfo.value(item, "title"), "")
        self.assertTrue(item.nfo_data["warnings"])

    def test_aggregate_count_is_bounded(self):
        for index in range(65):
            self.write(f"{index:02}.nfo", "Title: Same\n")
        item = rel.inspect(self.root)
        self.assertEqual(len(item.nfo_data["records"]), 64)
        self.assertTrue(item.nfo_data["warnings"])

    def test_symlink_sidecar_is_ignored(self):
        content = self.write("app.exe", b"fixture")
        target = self.write("unrelated.txt", "Title: Unrelated\n")
        try:
            (self.root / "app.nfo").symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertFalse(nfo.read_release(rel.inspect(content)))

    def test_description_provenance_and_priority(self):
        prose = "This is a sufficiently long description of the game from the accompanying NFO file."
        self.write("info.nfo", "Description\n" + prose)
        item = rel.inspect(self.root)
        meta = Meta()
        nfo.description(meta, item)
        self.assertEqual(meta.description_en, prose)
        self.assertEqual(meta.description_source, "NFO")
        meta.description_en, meta.description_source = "existing", "Steam"
        nfo.description(meta, item)
        self.assertEqual(meta.description_source, "Steam")

    def test_game_reads_complementary_nested_nfos(self):
        self.write("a.nfo", "Title: Game\n")
        self.write("docs/extra.nfo", "Version: 1.2\n")
        info = gamefiles.inspect(rel.inspect(self.root), deep=False)
        self.assertEqual((info.nfo["title"], info.nfo["version"]), ("Game", "1.2"))

    def test_own_software_single_file_reads_nfo(self):
        content = self.write("app.exe", b"fixture")
        self.write("app.nfo", "Title: Example App\nVersion: 1.2\n")
        item = rel.inspect(content)
        with mock.patch.object(software, "inspect_local", return_value={"kind": "other", "nfo": nfo.read_release(item)}), \
             mock.patch.object(software, "_resolve_program"):
            meta = software.resolve(item, "misc", original=False, hints={})
        self.assertEqual(meta.extra["nfo_files"], ["app.nfo"])
        self.assertIn("NFO", meta.sources)

    def test_music_missing_tags_uses_nfo_and_numbered_tracks(self):
        self.write("01.mp3", b"fixture")
        self.write("info.nfo", "Artist: NFO Artist\nAlbum: NFO Album\nYear: 2002\nGenre: Rock\n01. First Song   03:21\n")
        item = rel.inspect(self.root)
        tags = audiotags.AudioTags(path=self.root / "01.mp3", codec="mp3", duration=201)
        with mock.patch.object(audiotags, "read", return_value=tags):
            local = music.read_local(item, {})
            explicit = music.read_local(item, {"artist": "User", "album": "User Album", "year": "2003"})
            tags.artist, tags.album, tags.date = "Tag Artist", "Tag Album", "2004"
            tagged = music.read_local(item, {})
        self.assertEqual((local.artist, local.album, local.year), ("NFO Artist", "NFO Album", "2002"))
        self.assertEqual(local.tracks[0].title, "First Song")
        self.assertEqual((explicit.artist, explicit.album, explicit.year), ("User", "User Album", "2003"))
        self.assertEqual((tagged.artist, tagged.album, tagged.year), ("Tag Artist", "Tag Album", "2004"))

    def test_conflicting_tracklists_are_not_used(self):
        self.write("a.nfo", "01. First Song  03:21\n")
        self.write("b.nfo", "01. Other Song  03:21\n")
        self.assertEqual(nfo.track_titles(rel.inspect(self.root)), [])

    def test_book_nfo_seeds_isbn_lookup_preserves_embedded(self):
        self.write("book.epub", b"fixture")
        self.write("info.nfo", "Title: NFO Book\nAuthor: NFO Author\nISBN: 9780140328721\nYear: 1988\n")
        item = rel.inspect(self.root)
        minimal = {"format": "epub"}
        with mock.patch.object(books.ebookmeta, "read", return_value=minimal), \
             mock.patch.object(books.ebookmeta, "cover", return_value=b""), \
             mock.patch.object(books, "_book") as process:
            meta = books.resolve(item, "ebook", original=False, hints={})
        local = process.call_args.args[1]
        self.assertEqual((local["title"], local["creators"], local["isbns"], local["year"]),
                         ("NFO Book", ["NFO Author"], ["9780140328721"], "1988"))
        self.assertIn("NFO", meta.sources)
        with mock.patch.object(books.ebookmeta, "read", return_value={"format": "epub", "title": "Embedded", "creators": ["Author"]}), \
             mock.patch.object(books.ebookmeta, "cover", return_value=b""), \
             mock.patch.object(books, "_book") as process:
            books.resolve(item, "ebook", original=False, hints={})
        self.assertEqual(process.call_args.args[1]["title"], "Embedded")

    def test_mobile_header_priority(self):
        self.write("app.apk", b"fixture")
        self.write("info.nfo", "Title: NFO App\nVersion: 1.2\nPackage Name: org.example.app\n")
        item = rel.inspect(self.root)
        with mock.patch.object(mobile, "_android") as android:
            meta = mobile.resolve(item, original=False, hints={"version": "9.0"})
        self.assertEqual(android.call_args.args[2]["version"], "9.0")
        self.assertEqual(android.call_args.args[2]["package"], "org.example.app")
        self.assertIn("NFO", meta.sources)

    def test_console_header_title_priority(self):
        self.write("game.pkg", b"fixture")
        self.write("info.nfo", "Title: NFO Game\nVersion: 1.2\n")
        item = rel.inspect(self.root)
        info = consoleid.ConsoleInfo(title="Header Game", version="3.0")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(consoleid, "identify", return_value=info))
            for method in ("_igdb", "_fill_meta", "_description", "_naming", "_confidence"):
                stack.enter_context(mock.patch.object(console, method))
            meta = console.resolve(item, original=False, hints={})
        self.assertEqual((info.title, info.version), ("Header Game", "3.0"))
        self.assertIn("NFO", meta.sources)

    def test_video_own_annotates_nfo(self):
        self.write("info.nfo", "Title: Film\nhttps://imdb.com/title/tt1234567/\n")
        item = rel.inspect(self.root)
        with mock.patch.object(video, "_resolve"):
            meta = video.resolve(item, "hd", original=False, hints={})
        self.assertEqual(meta.extra["nfo_files"], ["info.nfo"])
        self.assertEqual(item.imdb_id, "tt1234567")


class OwnNfoPlanTests(PlanCase):
    def test_own_nfo_is_metadata_without_original_attachment(self):
        release = self.release("Example.2001.1080p-GRP", {"film.mkv": b"fixture", "info.nfo": b"Title: Example\n"})
        meta = Meta(title="Example", year="2001", confidence=0.9)
        nfo.annotate(meta, release)
        built, calls = self.build(release, "video", meta, category="hd", original=False, screenshots=False, make_sample=False)
        self.assertFalse(built.data.original)
        self.assertNotIn("nfo_fajl", built.attachments)
        self.assertFalse(calls[0]["original"])
        self.assertTrue(any("feldolgozott NFO" in note for note in built.notes))
