"""Regression tests for the 0.7.2 review fixes (seeding safety, completeness, scene names, dry-run cleanup, ...)."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ncore_up import net, plan as plans, preparation, privacy, qbit, release as rel, workflow
from ncore_up.client import NcoreClient, SearchTruncated
from ncore_up.resolve import music


class Response(SimpleNamespace):
    def json(self):
        return self.data


class FakeSession:
    def __init__(self, items, status=200):
        self.items, self.status, self.posts = items, status, []

    def post(self, url, data=None, **kwargs):
        self.posts.append((url, data))
        return SimpleNamespace(status_code=200, text="Ok.")

    def get(self, url, params=None, **kwargs):
        return Response(status_code=self.status, data=self.items, text="")


class IncompleteDownloadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "Release-GRP"
        self.source.mkdir()

    def test_unfinished_torrent_covering_the_source_is_reported(self):
        items = [{"name": "Release-GRP", "content_path": str(self.source), "progress": 0.99, "state": "stalledDL"},
                 {"name": "Other", "content_path": str(self.root / "Other"), "progress": 0.1, "state": "downloading"}]
        busy = qbit.incomplete_at("http://qb", "u", "p", str(self.source), session=FakeSession(items))
        self.assertEqual(len(busy), 1)
        self.assertIn("Release-GRP", busy[0])

    def test_finished_torrent_passes_and_file_inside_counts(self):
        done = [{"name": "Release-GRP", "content_path": str(self.source), "progress": 1, "state": "stalledUP"}]
        self.assertEqual(qbit.incomplete_at("http://qb", "", "", str(self.source), session=FakeSession(done)), [])
        inner = [{"name": "x", "content_path": str(self.source / "a.mkv"), "progress": 0.5, "state": "pausedDL"}]
        self.assertTrue(qbit.incomplete_at("http://qb", "", "", str(self.source), session=FakeSession(inner)))

    def test_unreadable_list_raises(self):
        with self.assertRaises(qbit.QbitError):
            qbit.incomplete_at("http://qb", "", "", str(self.source), session=FakeSession(None, status=500))


class SceneNameTests(unittest.TestCase):
    def test_int_suffix_is_an_original(self):
        for name in ("VA-Brazil_Top_100_Single_Charts_18.10.2026-AUDiAL_iNT", "Artist-Album-WEB-2026-ENRiCH_iNT",
                     "Movie.2020.1080p.BluRay.x264-OFT", "Show.S01E01.1080p.WEB.h264-GRP_INT"):
            self.assertTrue(plans.looks_original(SimpleNamespace(nfo=Path("x.nfo"), name=name)), name)
        self.assertFalse(plans.looks_original(SimpleNamespace(nfo=Path("x.nfo"), name="Az a bizonyos első év 1080p REMUX")))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "data" / "Album-GRP"
        self.source.mkdir(parents=True)
        (self.source / "01.flac").write_bytes(b"x" * 10)

    def test_discard_removes_only_the_own_job(self):
        ncup = self.root / "NCUP"
        other = ncup / "job-other"
        other.mkdir(parents=True)
        space = preparation.Workspace(rel.inspect(self.source), ncup)
        content = space.stage(rel.inspect(self.source))
        self.assertTrue(os.path.samefile(content / "01.flac", self.source / "01.flac"))
        job = space.job
        space.discard()
        self.assertFalse(job.exists())
        self.assertFalse(content.exists())
        self.assertFalse(space.manifest.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.source / "01.flac").exists())

    def test_prune_keeps_the_payload_and_mark_records_the_upload(self):
        space = preparation.Workspace(rel.inspect(self.source), self.root / "NCUP")
        content = space.stage(rel.inspect(self.source))
        work = space.job
        (work / "expanded-0").mkdir()
        space.prune()
        self.assertFalse(work.exists())
        self.assertEqual(content, self.root / "NCUP" / "Album-GRP")
        self.assertTrue((content / "01.flac").exists())
        space.mark("uploaded", torrent_id=4242)
        manifest = json.loads(space.manifest.read_text(encoding="utf-8"))
        self.assertEqual((manifest["status"], manifest["torrent_id"]), ("uploaded", 4242))

    def test_root_on_another_filesystem_moves_next_to_the_source(self):
        space = preparation.Workspace(rel.inspect(self.source), self.root / "NCUP")
        real_stat = Path.stat

        def fake_stat(path, *a, **kw):
            result = real_stat(path, *a, **kw)
            if Path(path) == self.root / "NCUP":
                return os.stat_result((result.st_mode, result.st_ino, result.st_dev + 999) + tuple(result)[3:])
            return result

        (self.root / "NCUP").mkdir()
        with mock.patch.object(Path, "stat", fake_stat), mock.patch.object(Path, "home", return_value=self.root / "elsewhere"):
            (self.root / "elsewhere").mkdir()
            chosen = space._root_for_source(self.root / "NCUP")
        self.assertNotEqual(chosen, self.root / "NCUP")
        self.assertEqual(chosen.name, "NCUP" if chosen.parent == self.root / "elsewhere" else ".ncup")


class SearchTruncationTests(unittest.TestCase):
    def test_truncated_search_keeps_the_pages_read(self):
        client = NcoreClient.__new__(NcoreClient)
        page = {"results": [{"torrent_id": str(i)} for i in range(100)], "total_results": "1000", "perpage": "100"}
        client._get = lambda path, params=None: SimpleNamespace(headers={"content-type": "application/json"}, json=lambda: page, text="")
        with self.assertRaises(SearchTruncated) as caught:
            client.search("tt0903747", by="imdb")
        self.assertEqual(len(caught.exception.results), 400)


class RedactionTests(unittest.TestCase):
    def test_netscape_cookie_lines_and_login_cookies(self):
        text = "ncore.pro\tFALSE\t/\tTRUE\t1999999999\tPHPSESSID\tabc123secret\n#HttpOnly_ncore.pro\tFALSE\t/\tTRUE\t0\tpass\tdeadbeef\nnick=someone; pass=abcdef"
        out = privacy.redact(text)
        for secret in ("abc123secret", "deadbeef", "abcdef"):
            self.assertNotIn(secret, out)


class EditionTests(unittest.TestCase):
    def test_other_edition_is_reported(self):
        local = SimpleNamespace(catno="AP-01", label="Spectacles Bonzaï")
        candidate = SimpleNamespace(data={"catno": "AP1J", "label": "Discunion Records"})
        found = music._edition_mismatch(local, candidate)
        self.assertEqual(len(found), 2)
        same = SimpleNamespace(data={"catno": "ap 01", "label": "Spectacles Bonzai"})
        self.assertEqual(music._edition_mismatch(local, same), [])


class AnswersTests(unittest.TestCase):
    def test_secret_files_cannot_be_referenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            sheet = Path(tmp) / "x.ncore.json"
            sheet.write_text(json.dumps({"install_guide": str(Path.home() / ".config" / "ncore-up" / "cookies.txt")}), encoding="utf-8")
            args = SimpleNamespace(path=tmp, answers=str(sheet))
            with self.assertRaises(ValueError):
                workflow.load_answers(args)
            sheet.write_text(json.dumps({"description": "creds.env"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                workflow.load_answers(args)

    def test_sibling_sheet_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Album"
            source.mkdir()
            source.with_name("Album.ncore.json").write_text(json.dumps({"category": "mp3"}), encoding="utf-8")
            with mock.patch.object(workflow, "ANSWERS_DIR", Path(tmp) / "answers"):
                self.assertEqual(workflow.load_answers(SimpleNamespace(path=str(source), answers=None)), {})


class CachePacingTests(unittest.TestCase):
    def test_expired_entry_is_not_a_cache_hit(self):
        response = SimpleNamespace(is_expired=True)
        cache = SimpleNamespace(create_key=lambda prepared: "k", get_response=lambda key: response)
        fake = SimpleNamespace(cache=cache, prepare_request=lambda request: request)
        with mock.patch.object(net, "session", return_value=fake):
            self.assertFalse(net._cached("GET", "https://example.org/x", None, None, None, None))
            response.is_expired = False
            self.assertTrue(net._cached("GET", "https://example.org/x", None, None, None, None))


if __name__ == "__main__":
    unittest.main()
