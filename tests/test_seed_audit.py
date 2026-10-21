"""--seed-audit finds the seed by the nCore torrent's hash, also when the upload run stopped during the seed wait."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ncore_up import cli, qbit, torrent

SITE = b"d8:announce3:url4:infod6:lengthi1e4:name1:x12:piece lengthi16384e6:pieces20:" + b"0" * 20 + b"7:privatei1eee"


class SeedAuditTests(unittest.TestCase):
    def run_audit(self, state: dict, torrents: list[dict]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "ncore-42.torrent").write_bytes(SITE)
            (run_dir / "state.json").write_text(json.dumps({"torrent_id": 42, "name": "A Biblia S01 BD50", **state}), encoding="utf-8")
            with sqlite3.connect(root / "uploads.sqlite3") as db:
                db.execute("CREATE TABLE jobs (key TEXT PRIMARY KEY, status TEXT, run_dir TEXT, updated REAL)")
                db.execute("INSERT INTO jobs VALUES ('k', 'uploaded', ?, 0)", (str(run_dir),))
            for item in torrents:
                item.setdefault("content_path", str(run_dir))
            config = SimpleNamespace(work_dir=root, qb_url="http://qb", qb_user="", qb_pass="")
            out = io.StringIO()
            with mock.patch.object(qbit, "torrents", return_value=torrents), redirect_stdout(out):
                code = cli.seed_audit(config)
        return code, out.getvalue()

    def test_interrupted_seed_wait_is_found_by_the_saved_site_torrent(self):
        seeded = {"hash": torrent.info_hash(SITE), "state": "uploading", "ratio": 0.1}
        code, text = self.run_audit({"status": "uploaded", "local_hash": "329397e0"}, [seeded])
        self.assertEqual(code, 0, text)
        self.assertIn("OK  #42", text)

    def test_missing_seed_is_reported(self):
        code, text = self.run_audit({"status": "uploaded"}, [])
        self.assertEqual(code, 11)
        self.assertIn("nincs a qBittorrentben", text)


if __name__ == "__main__":
    unittest.main()
