from __future__ import annotations

import unittest
from pathlib import Path

from ncore_up import describe, media, rules


def info(**kw) -> media.VideoInfo:
    base = dict(path=Path("x.mkv"), duration=6000, width=1920, height=1080, codec="h264",
                audio=[{"lang": "hun", "format": "ac3", "profile": "", "channels": 6, "title": "", "default": True},
                       {"lang": "eng", "format": "eac3", "profile": "", "channels": 6, "title": "", "default": False}],
                subtitles=[{"lang": "hun", "format": "subrip", "title": "", "forced": False}])
    base.update(kw)
    return media.VideoInfo(**base)


class VideoRuleTests(unittest.TestCase):
    def test_clean_hun_encode(self):
        self.assertEqual(rules.video(info(), "hd_hun", remux=False, original=False, foreign_original=False), ([], []))

    def test_track_rules(self):
        audio = [{"lang": "eng", "format": "truehd", "profile": "", "channels": 8, "title": "", "default": True},
                 {"lang": "hun", "format": "ac3", "profile": "", "channels": 6, "title": "", "default": False}]
        errors, warnings = rules.video(info(audio=audio), "hd_hun", remux=False, original=False, foreign_original=False)
        text = "\n".join(errors + warnings)
        self.assertIn("magyar hangsávnak kell az első helyen", text)
        self.assertIn("nem engedélyezett hangformátum ebben a kategóriában: truehd", text)
        self.assertIn("alapértelmezett", text)
        errors, _ = rules.video(info(audio=audio[:1]), "hd", remux=True, original=False, foreign_original=False)
        self.assertIn("kompatibilitási sáv kötelező", "\n".join(errors))

    def test_codec_resolution_and_sd(self):
        errors, _ = rules.video(info(codec="hevc"), "hd", remux=False, original=False, foreign_original=False)
        self.assertIn("1080p-ben x265/H.265 csak HDR/Dolby Vision", "\n".join(errors))
        self.assertEqual(rules.video(info(codec="hevc", hdr=True), "hd", remux=False, original=False, foreign_original=False)[0], [])
        errors, _ = rules.video(info(width=1280, height=720), "xvid", remux=False, original=False, foreign_original=False)
        self.assertIn("500–720 pixel", "\n".join(errors))
        subs = [{"lang": "und", "format": "subrip", "title": "", "forced": False}] * 20
        errors, _ = rules.video(info(subtitles=subs), "hd_hun", remux=False, original=False, foreign_original=False)
        self.assertTrue(any("16 sáv" in e for e in errors) and any("nyelvi címke" in e for e in errors))

    def test_foreign_original_is_exempt(self):
        errors, _ = rules.video(info(codec="hevc"), "hd", remux=False, original=True, foreign_original=True)
        self.assertTrue(any("1080p-ben" in e for e in errors))

    def test_sample_rule(self):
        self.assertTrue(rules.sample_needed("hd_hun", 3 * 1024**3, False, False))
        self.assertFalse(rules.sample_needed("hdser", 3 * 1024**3, False, False))
        self.assertFalse(rules.sample_needed("hd", 3 * 1024**3, True, False))


class NamingTests(unittest.TestCase):
    def test_wiki_examples(self):
        self.assertEqual(describe.video_name("Tomb Raider", "", "Tomb.Raider.2018.1080p.BluRay.x265-X", hungarian=True, hdr=True, dovi=True), "Tomb Raider 1080p DV HDR")
        self.assertEqual(describe.video_name("Tomb Raider", "", "Tomb.Raider.2018.2160p.UHD.BluRay.REMUX.HEVC-X", hungarian=True, hdr=True), "Tomb Raider 2160p UHD BluRay REMUX HDR")
        self.assertEqual(describe.video_name("Tomb Raider", "", "Tomb.Raider.2018.2160p.WEB-DL.H265-X", hungarian=True, dovi=True), "Tomb Raider 2160p DV")
        self.assertEqual(describe.video_name("Tomb Raider", "", "x", hungarian=True, disc="BD50"), "Tomb Raider BD50")
        self.assertEqual(describe.video_name("A függetlenség napja", "", "x.DVD9", hungarian=True, disc="DVD5"), "A függetlenség napja DVD5")
        self.assertEqual(describe.video_name("A függetlenség napja", "", "x.DVD9", hungarian=True, disc="DVD9"), "A függetlenség napja")
        self.assertEqual(describe.video_name("A függetlenség napja", "", "x.720p.BluRay.x264", hungarian=True, sd=True), "A függetlenség napja")
        self.assertEqual(describe.video_name("A szökés", "", "Prison.Break.S01.1080p.BluRay.x264-X", hungarian=True, season=1), "A szökés S01 1080p")


if __name__ == "__main__":
    unittest.main()
