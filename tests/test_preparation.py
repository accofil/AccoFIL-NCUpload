"""NCUP file policy, non-destructive assembly and real offline 7z extraction."""
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import struct
import unittest
from unittest import mock
import zipfile
import zlib

from ncore_up import categories as cats, payload, preparation as prep, release as rel
from ncore_up.config import Config


class NCUPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ncup = self.root / "NCUP"
        patcher = mock.patch.object(rel, "hungarian_audio", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def release(self, files, name="Artist-Album-2026-GRP"):
        root = self.root / name
        root.mkdir(exist_ok=True)
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return rel.inspect(root)

    def assemble(self, release, category="lossless", original=False):
        workspace = prep.Workspace(release, self.ncup)
        prepared, errors = workspace.assemble(release, cats.CATEGORIES[category], original)
        workspace.finalize()
        return workspace, prepared, errors

    def test_clean_release_does_not_need_copy(self):
        release = self.release({"01.flac": b"audio"})
        work, prepared, errors = self.assemble(release)
        self.assertIs(prepared, release)
        self.assertIsNone(work.job)
        self.assertFalse(self.ncup.exists())
        self.assertFalse(errors)

    def test_music_covers_kept_nfo_parsed_txt_omitted(self):
        release = self.release({"01.flac": b"audio", "cover.jpg": b"image", "info.nfo": b"Artist: Example\nAlbum: Album", "readme.txt": b"advert"})
        work, prepared, errors = self.assemble(release)
        self.assertEqual({f.relpath for f in prepared.files}, {"01.flac", "cover.jpg"})
        self.assertEqual(prepared.nfo_data["fields"]["artist"], "Example")
        self.assertTrue(release.nfo.exists())
        self.assertTrue(os.path.samefile(release.path / "01.flac", prepared.path / "01.flac"))
        self.assertFalse(errors)
        manifest = json.loads((work.job / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "ready")
        self.assertNotIn("manifest.json", {f.relpath for f in prepared.files})
        work.verify()

    def test_mp3_txt_and_log_removed_but_pdf_cover_kept(self):
        release = self.release({"01.mp3": b"audio", "rip.log": b"log", "log.txt": b"Exact Audio Copy", "booklet.pdf": b"pdf", "cover.jpg": b"jpg"})
        _, result, errors = self.assemble(release, "mp3")
        self.assertEqual({f.relpath for f in result.files}, {"01.mp3", "booklet.pdf", "cover.jpg"})
        self.assertFalse(errors)

    def test_lossless_real_txt_log_and_audiochecker(self):
        release = self.release({"01.flac": b"audio", "rip.txt": b"Exact Audio Copy extraction logfile", "audiochecker.log": b"log"})
        _, result, errors = self.assemble(release)
        self.assertEqual(len(result.files), 3)
        self.assertFalse(errors)

    def test_unsupported_album_track_is_never_silently_dropped(self):
        release = self.release({"01.flac": b"audio", "02.wv": b"audio", "readme.txt": b"advert"})
        _, result, errors = self.assemble(release)
        self.assertTrue((result.path / "02.wv").exists())
        self.assertTrue(errors)

    def test_ebook_covers_txt_removed_native_container_kept(self):
        release = self.release({"a.cbz": b"native", "cover.jpg": b"image", "readme.txt": b"text"})
        _, result, errors = self.assemble(release, "ebook")
        self.assertEqual([f.relpath for f in result.files], ["a.cbz"])
        self.assertFalse(errors)

    def test_common_wrapper_removed_empty_extras_disappear(self):
        release = self.release({"download/wrapper/01.flac": b"audio", "promo/readme.txt": b"advert"})
        work, result, errors = self.assemble(release)
        self.assertEqual([f.relpath for f in result.files], ["01.flac"])
        self.assertTrue((release.path / "download/wrapper/01.flac").exists())
        self.assertTrue(any(op["action"] == "remove-wrapper" for op in work.operations))
        self.assertFalse(errors)

    def test_album_disc_and_bluray_roots_not_flattened(self):
        self.assertEqual(payload.common_wrapper(["CD1/01.flac"]), ())
        self.assertEqual(payload.common_wrapper(["BDMV/STREAM/00000.m2ts"]), ())
        self.assertEqual(payload.common_wrapper(["outer/BDMV/STREAM/00000.m2ts"]), ("outer",))

    def test_bluray_structure_allowed_unrelated_nested_files_omitted(self):
        release = self.release({"BDMV/index.bdmv": b"index", "BDMV/STREAM/00000.m2ts": b"video", "extras/ad.txt": b"ad"})
        _, result, errors = self.assemble(release, "hd")
        self.assertTrue(result.has_bdmv)
        self.assertFalse((result.path / "extras").exists())
        self.assertFalse(errors)

    def test_loose_dvd_not_discarded(self):
        release = self.release({"VIDEO_TS/VTS_01_1.VOB": b"video"})
        _, result, errors = self.assemble(release, "dvd9")
        self.assertTrue(errors)
        self.assertTrue(result.files)

    def test_game_dependencies_preserved_or_blocked_not_discarded(self):
        release = self.release({"setup.exe": b"exe", "setup-1.bin": b"data", "data.pak": b"data", "crack/tool.dll": b"dll", "readme.txt": b"ad"})
        _, result, errors = self.assemble(release, "game_rip")
        self.assertTrue((result.path / "data.pak").exists())
        self.assertTrue((result.path / "crack/tool.dll").exists())
        self.assertTrue((result.path / "setup-1.bin").exists())
        self.assertTrue(any("data.pak" in error for error in errors))

    def test_mobile_necessary_data_kept(self):
        release = self.release({"app.apk": b"apk", "SDDATA/part.dat": b"data", "readme.txt": b"ad"})
        _, result, errors = self.assemble(release, "mobil")
        self.assertEqual({f.relpath for f in result.files}, {"app.apk", "SDDATA/part.dat"})
        self.assertFalse(errors)

    def test_original_names_and_nfo_preserved(self):
        release = self.release({"01.flac": b"audio", "info.nfo": b"Artist: Artist", "readme.txt": b"text"})
        _, result, errors = self.assemble(release, original=True)
        self.assertIs(result, release)
        self.assertEqual(result.name, release.name)
        self.assertTrue(result.nfo.exists())
        self.assertFalse(errors)

    def test_cross_device_copy_and_source_preserved(self):
        release = self.release({"01.flac": b"audio", "readme.txt": b"text"})
        with mock.patch.object(os, "link", side_effect=OSError(errno.EXDEV, "cross device")):
            work, result, errors = self.assemble(release)
        self.assertEqual((result.path / "01.flac").read_bytes(), b"audio")
        self.assertFalse(os.path.samefile(release.path / "01.flac", result.path / "01.flac"))
        self.assertTrue(any(op["action"] == "copy" for op in work.operations))
        work.verify()

    def test_sfv_and_playlist_references_no_source_mutation(self):
        sfv = b"01.flac 12345678\ninfo.nfo 87654321\n"
        playlist = b"#EXTM3U\n01.flac\ninfo.nfo\n"
        release = self.release({"01.flac": b"audio", "info.nfo": b"info", "all.sfv": sfv, "all.m3u": playlist})
        _, result, errors = self.assemble(release)
        self.assertEqual((release.path / "all.sfv").read_bytes(), sfv)
        self.assertEqual((release.path / "all.m3u").read_bytes(), playlist)
        self.assertEqual((result.path / "all.sfv").read_bytes(), sfv.splitlines(keepends=True)[0])
        self.assertNotIn(b"info.nfo", (result.path / "all.m3u").read_bytes())

    def test_each_attempt_is_new_and_payload_change_detected(self):
        release = self.release({"01.flac": b"audio", "readme.txt": b"text"})
        first, result, _ = self.assemble(release)
        second, _, _ = self.assemble(release)
        self.assertNotEqual(first.content, second.content)
        (result.path / "extra.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "NCUP kiadás"):
            first.verify()

    def test_source_change_detected(self):
        release = self.release({"01.flac": b"audio", "readme.txt": b"text"})
        work, _, _ = self.assemble(release)
        (release.path / "readme.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "forrása"):
            work.verify()

    def test_source_and_ncup_overlap_rejected(self):
        release = self.release({"01.flac": b"audio"})
        for root in (release.path, release.path / "NCUP", self.root):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, "fedheti"):
                prep.Workspace(release, root).stage(release)

    def test_missing_ncup_config_and_disk_full(self):
        release = self.release({"01.flac": b"audio"})
        with self.assertRaisesRegex(ValueError, "nincs beállítva"):
            prep.Workspace(release, None).stage(release)
        with mock.patch.object(shutil, "disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaisesRegex(ValueError, "elegendő hely"):
                prep._space(self.root, 1)

    def test_archive_path_safety(self):
        for name in ("../x", "/x", "C:\\x", "a/../../x", "a//x", "a/./x", "a\nx"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                prep._member(name)

    def test_config_load_no_directory_creation(self):
        path = self.root / "config.env"
        path.write_text("NCORE_NCUP_DIR=" + str(self.ncup))
        with mock.patch.dict(os.environ, {}, clear=True):
            config = Config.load(path)
        self.assertEqual(config.ncup_dir, self.ncup)
        self.assertFalse(self.ncup.exists())

    def test_disc_copy_preserves_full_structure_and_rejects_symlink(self):
        from ncore_up.discmount import _copy
        disc = self.root / "disc"
        (disc / "BDMV/STREAM").mkdir(parents=True)
        (disc / "BDMV/index.bdmv").write_bytes(b"index")
        (disc / "BDMV/STREAM/00000.m2ts").write_bytes(b"video")
        (disc / "unrelated.txt").write_bytes(b"ad")
        output = self.root / "output"
        output.mkdir()
        self.assertEqual(_copy(disc, output, 10), (2, 10))
        self.assertFalse((output / "unrelated.txt").exists())
        if os.name == "posix":
            (disc / "BDMV/link").symlink_to(disc / "unrelated.txt")
            other = self.root / "other"
            other.mkdir()
            with self.assertRaisesRegex(ValueError, "szabályos"):
                _copy(disc, other, 100)


@unittest.skipUnless(shutil.which("7z"), "7z is required")
class ArchiveTests(unittest.TestCase):
    setUp = NCUPTests.setUp
    def packed(self, entries):
        path = self.root / "Artist-Album-2026-GRP.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return rel.inspect(path)

    def test_real_zip_expansion_and_metadata(self):
        release = self.packed({"Artist-Album-2026-GRP/01.flac": b"audio", "Artist-Album-2026-GRP/info.nfo": b"Artist: Example"})
        before = release.path.read_bytes()
        work = prep.Workspace(release, self.ncup)
        expanded = work.expand(release, "lossless", False)
        result, errors = work.assemble(expanded, cats.CATEGORIES["lossless"], False)
        work.finalize()
        self.assertEqual([f.relpath for f in result.files], ["01.flac"])
        self.assertEqual(result.nfo_data["fields"]["artist"], "Example")
        self.assertEqual(release.path.read_bytes(), before)
        work.verify()

    def test_real_rar5_expansion(self):
        # Minimal uncompressed RAR5, generated from the published format:
        # https://www.rarlab.com/technote.htm (no downloaded binary fixture).
        def vint(value):
            result = bytearray()
            while value > 127:
                result.append((value & 127) | 128)
                value >>= 7
            return bytes(result + bytes([value]))
        def header(data):
            content = vint(len(data)) + data
            return struct.pack("<I", zlib.crc32(content)) + content
        blob = b"Rar!\x1a\x07\x01\x00" + header(b"\x01\x00\x00")
        for name, data in ((b"01.flac", b"audio"), (b"info.nfo", b"Artist: Example")):
            content = b"\x02\x02" + vint(len(data)) + b"\x04" + vint(len(data)) + vint(0o100644)
            content += struct.pack("<I", zlib.crc32(data)) + b"\x00\x01" + vint(len(name)) + name
            blob += header(content) + data
        blob += header(b"\x05\x00\x00")
        path = self.root / "album.rar"
        path.write_bytes(blob)
        release = rel.inspect(path)
        work = prep.Workspace(release, self.ncup)
        expanded = work.expand(release, "lossless", False)
        self.assertEqual((expanded.path / "01.flac").read_bytes(), b"audio")
        result, errors = work.assemble(expanded, cats.CATEGORIES["lossless"], False)
        self.assertEqual(result.nfo_data["fields"]["artist"], "Example")
        self.assertEqual(path.read_bytes(), blob)
        self.assertFalse(errors)

    def test_real_original_zip_preserves_name_and_nfo(self):
        release = self.packed({"Artist-Album-2026-GRP/01.flac": b"audio", "Artist-Album-2026-GRP/info.nfo": b"Artist: Example"})
        work = prep.Workspace(release, self.ncup)
        expanded = work.expand(release, "lossless", True)
        result, errors = work.assemble(expanded, cats.CATEGORIES["lossless"], True)
        self.assertEqual(result.path.name, release.name)
        self.assertTrue(result.nfo.exists())
        self.assertEqual({f.relpath for f in result.files}, {"01.flac", "info.nfo"})

    def test_archive_traversal_is_rejected_before_extraction(self):
        release = self.packed({"../escape.txt": b"bad", "01.flac": b"audio"})
        with self.assertRaisesRegex(ValueError, "biztonságos"):
            prep.Workspace(release, self.ncup).expand(release, "lossless", False)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_archive_symlink_is_rejected(self):
        path = self.root / "link.zip"
        item = zipfile.ZipInfo("link")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(item, "../outside")
        with self.assertRaisesRegex(ValueError, "linket"):
            prep.archive_entries(path)

    def test_real_split_7z(self):
        content = self.root / "01.flac"
        content.write_bytes(os.urandom(6000))
        archive = self.root / "album.7z"
        subprocess.run(["7z", "a", "-v1k", str(archive), str(content)], stdout=subprocess.DEVNULL, check=True)
        folder = self.root / "packed"
        folder.mkdir()
        for path in self.root.glob("album.7z.*"):
            path.rename(folder / path.name)
        release = rel.inspect(folder)
        work = prep.Workspace(release, self.ncup)
        result = work.expand(release, "lossless", False)
        self.assertEqual((result.path / "01.flac").read_bytes(), content.read_bytes())
        self.assertEqual(len([op for op in work.operations if op["action"] == "unpack"]), 1)

    def test_native_cbz_and_apk_never_unpacked(self):
        for suffix, category in (("cbz", "ebook"), ("apk", "mobil")):
            path = self.root / ("native." + suffix)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("native/data", b"data")
            release = rel.inspect(path)
            work = prep.Workspace(release, self.ncup)
            self.assertIs(work.expand(release, category, False), release)
            self.assertIsNone(work.job)

    def test_password_archive_fails(self):
        content = self.root / "01.flac"
        content.write_bytes(b"audio")
        archive = self.root / "password.7z"
        subprocess.run(["7z", "a", "-pSECRET", str(archive), str(content)], check=True, stdout=subprocess.DEVNULL)
        with self.assertRaisesRegex(ValueError, "jelszavas"):
            prep.archive_entries(archive)

    def test_iso_listing_empty_link_fields_and_directory_size(self):
        listing = b"Path = BDMV\nFolder = +\nSize = \nSymbolic Link = \n\nPath = BDMV/index.bdmv\nFolder = -\nSize = 5\nSymbolic Link = \n"
        result = subprocess.CompletedProcess([], 0, listing, b"")
        with mock.patch.object(prep.safeio, "run", return_value=result):
            entries = prep.archive_entries(self.root / "disc.iso")
        self.assertEqual(entries, [("BDMV", 0, True), ("BDMV/index.bdmv", 5, False)])
