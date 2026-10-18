"""Moderator regression: all 15 album files, not just its 12 FLAC tracks."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ncore_up import media, requirements, techinfo
from ncore_up.meta import Meta, Track
from test_plan_build import PlanCase


class FullPayloadTests(PlanCase):
    def album(self):
        files = {f"{i:02}.flac": b"A" for i in range(1, 13)}
        files.update({"cover.jpg": b"image", "all.m3u": b"01.flac\n", "checks.sfv": b"01.flac 12345678\n", "info.nfo": b"Artist: Artist"})
        release = self.release("Artist-Album-FLAC-2026-GRP", files)
        meta = Meta(kind="album", title="Album", creators=["Artist"], year="2026", confidence=1,
                    tracks=[Track(str(i), f"Track {i}", "3:00") for i in range(1, 13)],
                    proof_links=["https://www.discogs.com/release/1"], tags=["jazz"])
        return release, meta

    def test_all_fifteen_payload_files_and_no_nfo(self):
        release, meta = self.album()
        plan, _ = self.build(release, "music", meta, original=False)
        self.assertEqual(len(plan.techinfo_files), 15)
        self.assertEqual(len(self.calls["mediainfo"]), 15)
        self.assertNotIn("info.nfo", plan.techinfo_files)
        for name in plan.techinfo_files:
            self.assertIn("MEDIAINFO " + name, plan.texts["techinfo_fajl"])
        coverage = next(r for r in requirements.evaluate(plan) if r.id == "techinfo_coverage")
        self.assertEqual(coverage.status, "PASS")
        self.assertEqual(coverage.evidence, "15/15 fájl")

    def test_incomplete_or_edited_report_is_mandatory_failure(self):
        release, meta = self.album()
        plan, _ = self.build(release, "music", meta, original=False)
        plan.texts["techinfo_fajl"] = "MEDIAINFO 01.flac\n"
        self.assertEqual(next(r.status for r in requirements.evaluate(plan) if r.id == "techinfo_coverage"), "FAIL")

    def test_added_file_is_not_covered_by_previous_report(self):
        release, meta = self.album()
        plan, _ = self.build(release, "music", meta, original=False)
        (plan.content_path / "other.jpg").write_bytes(b"other")
        self.assertEqual(next(r.status for r in requirements.evaluate(plan) if r.id == "techinfo_coverage"), "FAIL")


class PlainTextReports(unittest.TestCase):
    def test_same_basename_keeps_relative_paths_and_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("CD1/01.flac", "CD2/01.flac"):
                path = root / name
                path.parent.mkdir()
                path.write_bytes(b"audio")
            report = "General\nComplete name : 01.flac\nTitle : Track\nPerformer : Artist\nAlbum : Album\nAudio\nFormat : FLAC\n"
            with mock.patch.object(media, "mediainfo_text", return_value=report):
                text, files = techinfo.generate(root)
            self.assertEqual(set(files), {"CD1/01.flac", "CD2/01.flac"})
            self.assertIn("Complete name : CD1/01.flac", text)
            self.assertIn("Complete name : CD2/01.flac", text)
            self.assertEqual(text.count("Performer : Artist"), 2)

    def test_missing_companion_report_does_not_silently_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cover.jpg").write_bytes(b"image")
            with mock.patch.object(media, "mediainfo_text", side_effect=media.MediaError("unreadable")):
                with self.assertRaises(media.MediaError):
                    techinfo.generate(root)
