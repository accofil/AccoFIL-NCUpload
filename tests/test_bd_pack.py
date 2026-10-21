"""Multi-disc Blu-ray season packs as own uploads: category, disc roots and label, BD-J menu, BDInfo limits."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import categories as cats, media, payload, plan as plans, release as rel, safeio

DISC = "Show.Disc{n}.2013.Bluray.1080p-GRP/"


def pack(discs=2, menu=True, gb=40):
    files = []
    for n in range(1, discs + 1):
        root = DISC.format(n=n)
        files += [rel.ReleaseFile(root + "BDMV/STREAM/00001.m2ts", gb << 30), rel.ReleaseFile(root + "BDMV/index.bdmv", 100),
                  rel.ReleaseFile(root + "AACS/CPSUnit00001.cci", 1 << 20), rel.ReleaseFile(root + "CERTIFICATE/id.bdmv", 100),
                  rel.ReleaseFile(root + "disc.inf", 50)]
        if menu:
            files += [rel.ReleaseFile(root + "BDMV/BDJO/00000.bdjo", 300), rel.ReleaseFile(root + "BDMV/JAR/00000/menu.jar", 9000)]
    return rel.Release(path=Path("/x/Show.S01.Bluray"), name="Show.S01.Bluray", files=files, nfo=None, has_bdmv=True, has_video_ts=False)


class BluRayPackTests(unittest.TestCase):
    def test_aacs_cci_is_not_a_3ds_game(self):
        exts = [".m2ts", ".bdmv", ".cci", ".inf", ".mpls", ".clpi"]
        code = cats.detect("Show.S01.2013.Bluray.1080p.AVC.DTSHD.MA5.1-GRP", exts, True, False, None).code
        self.assertIn(code, {"hdser", "hdser_hun"})
        self.assertEqual(cats.CATEGORIES[cats.detect("Game.3DS-GRP", [".cci"], False, False, None).code].group, "console")

    def test_each_disc_is_a_root_and_the_label_is_per_disc(self):
        release = pack(discs=4)
        roots = plans._disc_roots(release)
        self.assertEqual(len(roots), 4)
        one = next(iter(roots.values()))
        self.assertEqual(one, (40 << 30) + 100 + (1 << 20) + 100 + 50 + 300 + 9000)
        self.assertEqual(plans._disc_label(release, None), "BD50")

    def test_bdj_menu_on_every_disc_is_detected(self):
        self.assertIs(pack().bd_menu, True)
        self.assertIsNone(pack(menu=False).bd_menu)
        mixed = pack()
        mixed.files = [f for f in mixed.files if not (f.relpath.startswith(DISC.format(n=2)) and "BDJO" in f.relpath)]
        self.assertIsNone(mixed.bd_menu)

    def test_disc_structure_files_are_kept(self):
        release = pack(discs=1)
        for item in release.files:
            self.assertEqual(payload.decision(release, item, cats.CATEGORIES["hdser"], False)[0], "KEEP", item.relpath)

    def test_bdinfo_runs_without_rlimits(self):
        calls = []

        def fake(args, timeout=600, memory_limit=safeio.MEMORY_LIMIT, file_limit=safeio.PACKAGE_LIMIT):
            calls.append((memory_limit, file_limit))
            out = b"  1: 00800.MPLS  00:49:41 /d/BDMV/PLAYLIST/00800.mpls\n" if args[1] == "-l" else b""
            if args[1] == "-m":
                Path(args[-1], "report.txt").write_text("DISC INFO:\nx\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, out, b"")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media, "_run", fake):
            tool = Path(tmp) / "bdinfo"
            tool.write_text("")
            media.bdinfo_text(str(tool), Path(tmp) / "Disc1", Path(tmp) / "work")
        self.assertEqual(calls, [(None, None), (None, None)])
        self.assertNotIn("--fsize", " ".join(safeio.command(["x"], file_limit=None, memory_limit=None)))


if __name__ == "__main__":
    unittest.main()
