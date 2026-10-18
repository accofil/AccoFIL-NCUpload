"""media.py: ffmpeg/BDInfo/MediaInfo invocations (mocked) and, where the tools exist, real runs on generated videos."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from ncore_up import media

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

LISTING = """Please wait while we scan the disc...
Playlists:
  1: 00000.MPLS             01:30:13 /data/Rel/BDMV/PLAYLIST/00000.mpls
  2: 00203.MPLS             00:10:02 /data/Rel/BDMV/PLAYLIST/00203.mpls
  3: 00800.MPLS             02:01:00 /data/Rel/BDMV/PLAYLIST/00800.mpls
"""

REPORT = """Disc Title:     Film
Disc Label:     Rel
BDInfo:         0.7.6.4 CLI

<--- BEGIN FORUMS PASTE --->
[code]
Title    Codec
00800.MPLS  HEVC
[/code]

[code]

DISC INFO:

Disc Label:     Rel
Disc Size:      63,027,863,089 bytes

PLAYLIST REPORT:

Name:                   00800.MPLS
Path:                   /data/Rel/BDMV/PLAYLIST/00800.mpls

[/code]
<---- END FORUMS PASTE ---->

QUICK SUMMARY:

Disc Label: Rel
"""

MEDIAINFO = """General
Unique ID                                : 1 (0x1)
Complete name                            : /home/u/torrents/Rel.Name-GRP/rel.name-grp.mkv
Format                                   : Matroska
"""


def done(stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, stderr)


class CommandTests(unittest.TestCase):
    def test_run_closes_stdin(self):
        with mock.patch.object(media.safeio, "run", return_value=done()) as run:
            media._run(["bdinfo", "x"])
        run.assert_called_once_with(["bdinfo", "x"], timeout=600)

    def test_screenshot_args(self):
        m2ts = media.screenshot_args(Path("/d/BDMV/STREAM/00000.m2ts"), 812.0, Path("/o/screen1.png"), hdr=True)
        self.assertLess(m2ts.index("-skip_frame"), m2ts.index("-i"))  # decoder option: before the input
        self.assertEqual(m2ts[m2ts.index("-skip_frame") + 1], "nokey")
        self.assertLess(m2ts.index("-ss"), m2ts.index("-i"))
        self.assertEqual(m2ts[m2ts.index("-map") + 1], "0:V:0")
        self.assertEqual(m2ts[m2ts.index("-vf") + 1], media.TONEMAP)
        self.assertEqual(m2ts[m2ts.index("-pix_fmt") + 1], "rgb24")
        mkv = media.screenshot_args(Path("/d/movie.mkv"), 10.0, Path("/o/screen1.jpg"), hdr=False, image_format="jpg")
        self.assertNotIn("-skip_frame", mkv)
        self.assertNotIn("-vf", mkv)
        self.assertEqual(mkv[-3:], ["-q:v", "2", str(Path("/o/screen1.jpg"))])
        self.assertIn("tonemap=hable", media.TONEMAP)
        self.assertTrue(media.TONEMAP.startswith("zscale=t=linear"))

    def test_big_png_becomes_jpeg(self):
        sizes = {"screen1.png": 100, "screen2.png": 10 * 1024**2, "screen3.png": 100}

        def fake_run(args, timeout=600):
            target = Path(args[-1])
            target.write_bytes(b"x" * (sizes.get(target.name) or 50))
            return done()

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(media, "probe", return_value=media.VideoInfo(path=Path("v.mkv"), duration=100.0, hdr=True)), \
                mock.patch.object(media, "has_tonemap", return_value=True), mock.patch.object(media, "_run", side_effect=fake_run) as run:
            shots = media.screenshots(Path("v.mkv"), Path(tmp))
            self.assertEqual([p.name for p in shots], ["screen1.png", "screen2.jpg", "screen3.png"])
            self.assertFalse((Path(tmp) / "screen2.png").exists())
            first = run.call_args_list[0].args[0]
            self.assertEqual(first[first.index("-vf") + 1], media.TONEMAP)
            times = [float(c.args[0][c.args[0].index("-ss") + 1]) for c in run.call_args_list if "-ss" in c.args[0]]
            self.assertEqual(times, [15.0, 45.0, 75.0])

    def test_no_tonemap_without_filters(self):
        with mock.patch.object(media, "_filters", return_value=frozenset({"scale", "zscale"})):
            self.assertFalse(media.has_tonemap())
        with mock.patch.object(media, "_filters", return_value=frozenset({"zscale", "tonemap"})):
            self.assertTrue(media.has_tonemap())

    def test_sample_is_recut_until_big_enough(self):
        calls = []

        def fake_run(args, timeout=600):
            seconds = int(args[args.index("-t") + 1])
            calls.append(seconds)
            Path(args[-1]).write_bytes(b"x" * (seconds * 1000))  # the middle has half the average bitrate
            return done()

        info = media.VideoInfo(path=Path("v.mkv"), duration=5000.0, bitrate=16000)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(media, "probe", return_value=info), mock.patch.object(media, "_run", side_effect=fake_run):
            sample = media.make_sample(Path("v.mkv"), Path(tmp) / "stage" / "sample.mkv", min_bytes=200_000)
            self.assertGreaterEqual(sample.stat().st_size, 200_000)
        self.assertEqual(calls[0], 110)  # 200 kB at 16 kbit/s = 100 s, +10
        self.assertGreater(calls[1], calls[0])

    def test_sample_args_copy_video_audio_subtitles(self):
        args = media.sample_args(Path("in.mkv"), Path("out.mkv"), 100.0, 65)
        self.assertEqual(args[args.index("-c") + 1], "copy")
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-map"], ["0:V", "0:a?", "0:s?"])
        self.assertLess(args.index("-ss"), args.index("-i"))

    def test_cover_takes_one_frame_within_300(self):
        args = media.cover_args(Path("c.src"), Path("c.jpg"))
        self.assertEqual(args[args.index("-frames:v") + 1], "1")
        self.assertIn("min(300,iw)", args[args.index("-vf") + 1])
        self.assertIn("force_original_aspect_ratio=decrease", args[args.index("-vf") + 1])


class ReportTests(unittest.TestCase):
    def test_mediainfo_keeps_only_the_file_name(self):
        text = media.strip_paths(MEDIAINFO, PurePosixPath("/home/u/torrents/Rel.Name-GRP/rel.name-grp.mkv"))
        self.assertIn("Complete name                            : rel.name-grp.mkv\n", text)
        self.assertNotIn("/home/u", text)

    def test_main_playlist_is_the_longest(self):
        self.assertEqual(media._main_playlist(LISTING), "00800.MPLS")
        self.assertIsNone(media._main_playlist("Please wait while we scan the disc...\n"))

    def test_bdinfo_report_is_the_full_playlist_report(self):
        text = media.bdinfo_report(REPORT, PurePosixPath("/data/Rel"))
        self.assertTrue(text.startswith("DISC INFO:"))
        self.assertNotIn("[code]", text)
        self.assertNotIn("[/code]", text)
        self.assertNotIn("QUICK SUMMARY", text)
        self.assertNotIn("/data/", text)
        self.assertIn("Path:                   Rel/BDMV/PLAYLIST/00800.mpls", text)
        self.assertIn("Disc Size:      63,027,863,089 bytes\n", text)

    def test_bdinfo_invocation(self):
        calls = []

        def fake_run(args, timeout=600):
            calls.append(args)
            if "-l" in args:
                return done(LISTING.encode())
            (Path(args[-1]) / "BDINFO.Rel.txt").write_text(REPORT, encoding="utf-8")
            return done()

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media, "_run", side_effect=fake_run):
            bdinfo = Path(tmp) / "bdinfo"
            bdinfo.write_bytes(b"")
            text = media.bdinfo_text(str(bdinfo), Path("/data/Rel"), Path(tmp) / "work")
        self.assertEqual(calls[0][:3], [str(bdinfo), "-l", str(Path("/data/Rel"))])
        self.assertEqual(calls[1][:4], [str(bdinfo), "-m", "00800.MPLS", str(Path("/data/Rel"))])
        self.assertTrue(text.startswith("DISC INFO:"))

    def test_bdinfo_without_playlist_stops(self):
        # without -m the BDInfo CLI asks for a playlist on the console and hangs (or crashes without a console)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media, "_run", return_value=done(b"Please wait...\n")) as run:
            bdinfo = Path(tmp) / "bdinfo"
            bdinfo.write_bytes(b"")
            with self.assertRaises(media.MediaError):
                media.bdinfo_text(str(bdinfo), Path("/data/Rel"), Path(tmp) / "work")
        self.assertEqual(run.call_count, 1)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe nincs")
class RealFfmpegTests(unittest.TestCase):
    """Small generated videos; checks that the commands really work with the installed ffmpeg."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.mkv, cls.ts = root / "Árvíz film.mkv", root / "00001.m2ts"
        base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440", "-t", "80", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac",
                "-metadata:s:a:0", "language=hun"]
        subprocess.run(base + [str(cls.mkv)], check=True, timeout=120)
        subprocess.run(base + ["-f", "mpegts", str(cls.ts)], check=True, timeout=120)
        cover = root / "cover.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=600x900", "-frames:v", "1", str(cover)],
                       check=True, timeout=60)
        cls.cover = cover.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def size(self, path: Path) -> tuple[int, int]:
        info = media.probe(path)
        return info.width, info.height

    def test_screenshots_at_own_resolution(self):
        for video in (self.mkv, self.ts):
            with self.subTest(video=video.name):
                out = Path(self.tmp.name) / f"shots-{video.suffix[1:]}"
                shots = media.screenshots(video, out)
                self.assertEqual(len(shots), 3)
                self.assertTrue(all(p.suffix == ".png" for p in shots))
                self.assertEqual(self.size(shots[0]), (320, 240))

    def test_oversized_png_is_sent_as_jpeg(self):
        shots = media.screenshots(self.mkv, Path(self.tmp.name) / "shots-small", max_bytes=1000)
        self.assertEqual([p.suffix for p in shots], [".jpg"] * 3)
        self.assertEqual(self.size(shots[1]), (320, 240))

    def test_sample_is_at_least_a_minute(self):
        sample = media.make_sample(self.mkv, Path(self.tmp.name) / "stage" / "sample.mkv")
        info = media.probe(sample)
        self.assertGreaterEqual(info.duration, 60)
        self.assertEqual([a["lang"] for a in info.audio], ["hun"])

    def test_cover_fits_300(self):
        out = media.infobar_cover(self.cover, Path(self.tmp.name) / "cover" / "borito.jpg")
        width, height = self.size(out)
        self.assertLessEqual(max(width, height), 300)
        self.assertEqual((width, height), (200, 300))

    @unittest.skipUnless(shutil.which("mediainfo"), "mediainfo nincs")
    def test_mediainfo_without_paths(self):
        text = media.mediainfo_text(self.mkv)
        self.assertIn(": Árvíz film.mkv", text)
        self.assertNotIn(str(self.mkv.parent), text)


if __name__ == "__main__":
    unittest.main()
