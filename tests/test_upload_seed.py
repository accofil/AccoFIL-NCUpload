"""After the form: the .torrent (mkbrr), info-hash and content comparison, qBittorrent, original-release files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from requests.structures import CaseInsensitiveDict

from ncore_up import categories as cats
from ncore_up import qbit, torrent
from ncore_up import release as rel


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    keys = sorted(value, key=lambda k: k.encode("utf-8") if isinstance(k, str) else k)
    return b"d" + b"".join(bencode(k) + bencode(value[k]) for k in keys) + b"e"


def make_torrent(name: str = "Repo.Men.2010.1080p-GRP", pieces: bytes = b"a" * 20, source: str = "", announce: str = "https://ncore.pro/announce") -> bytes:
    info = {"name": name, "piece length": 16384, "pieces": pieces, "length": 5, "private": 1}
    if source:
        info["source"] = source
    return bencode({"announce": announce, "info": info})


def real_mkbrr() -> str | None:
    try:
        import pwd  # the tests run with HOME pointing elsewhere
    except ImportError:
        return shutil.which("mkbrr")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    path = home / "Upload-Assistant" / "bin" / "mkbrr" / "linux" / "amd64" / "mkbrr"
    return str(path) if path.exists() else shutil.which("mkbrr")


def answer(text: str = "Ok.", status: int = 200, url: str = "http://qb/api") -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = text.encode("utf-8")
    r.headers = CaseInsensitiveDict({"content-type": "text/plain"})
    r.encoding = "utf-8"
    return r


class TorrentTests(unittest.TestCase):
    def test_info_hash_rejects_non_torrents(self):
        for data in (b"<html>", b"", b"d4:info", b"d8:announce3:abc"):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    torrent.info_hash(data)

    def test_same_content_ignores_tracker_changes(self):
        local = make_torrent(source="nCore")
        self.assertTrue(torrent.same_content(make_torrent(source="[ncore.pro]", announce="https://x/y"), local))
        self.assertFalse(torrent.same_content(make_torrent(pieces=b"b" * 20), local))
        self.assertFalse(torrent.same_content(make_torrent(name="Masik"), local))
        self.assertFalse(torrent.same_content(b"<html>", local))
        self.assertNotEqual(torrent.info_hash(make_torrent(source="x")), torrent.info_hash(local))

    def fake_mkbrr(self, sizes: dict):
        """mkbrr stand-in: writes a torrent named after the content whose size depends on the piece exponent."""

        commands = []

        def run(command, **kw):
            commands.append(command)
            content, output = Path(command[2]), Path(command[command.index("-o") + 1])
            exponent = int(command[command.index("-l") + 1]) if "-l" in command else 20
            info = {"name": content.name, "piece length": 2 ** exponent, "pieces": b"p" * 20, "length": 1, "private": 1, "source": "nCore"}
            output.write_bytes(bencode({"announce": "https://ncore.pro/announce", "info": info, "pad": b"x" * sizes.get(exponent, 10)}))
            return subprocess.CompletedProcess(command, 0, "", "")

        return commands, run

    def test_create_grows_the_piece_size_until_1mb(self):
        commands, run = self.fake_mkbrr({20: 3 * 1024**2, 21: 2 * 1024**2})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(torrent.subprocess, "run", side_effect=run):
            content = Path(tmp) / "Rel.Name-GRP"
            content.mkdir()
            data = torrent.create("mkbrr", content, Path(tmp) / "out.torrent")
        self.assertLessEqual(len(data), torrent.MAX_TORRENT_BYTES)
        self.assertEqual(torrent.piece_length(data), 2 ** 22)
        self.assertNotIn("-l", commands[0])
        self.assertEqual([c[c.index("-l") + 1] for c in commands[1:]], ["21", "22"])
        for flag in ("--private", "--skip-prefix", "--no-date"):
            self.assertIn(flag, commands[0])
        self.assertEqual(commands[0][commands[0].index("-s") + 1], "nCore")

    def test_create_errors(self):
        commands, run = self.fake_mkbrr({})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                torrent.create("mkbrr", Path(tmp) / "nincs", Path(tmp) / "out.torrent")
            content = Path(tmp) / "Rel"
            content.mkdir()
            with mock.patch.object(torrent.subprocess, "run", side_effect=FileNotFoundError("mkbrr")), self.assertRaises(RuntimeError):
                torrent.create("mkbrr", content, Path(tmp) / "out.torrent")
            failed = subprocess.CompletedProcess([], 1, "", "error: no files")
            with mock.patch.object(torrent.subprocess, "run", return_value=failed), self.assertRaises(RuntimeError) as ctx:
                torrent.create("mkbrr", content, Path(tmp) / "out.torrent")
            self.assertIn("no files", str(ctx.exception))

            def renaming(command, **kw):
                Path(command[command.index("-o") + 1]).write_bytes(make_torrent(name="Other"))
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(torrent.subprocess, "run", side_effect=renaming), self.assertRaises(RuntimeError):
                torrent.create("mkbrr", content, Path(tmp) / "out.torrent")

    @unittest.skipUnless(real_mkbrr(), "mkbrr nincs")
    def test_real_mkbrr(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "Árvíz.Film.2010.1080p-GRP"
            (content / "Sample").mkdir(parents=True)
            (content / "film.mkv").write_bytes(os.urandom(300_000))
            (content / "Sample" / "sample.mkv").write_bytes(os.urandom(1000))
            (content / "grp.nfo").write_text("nfo\n")
            data = torrent.create(real_mkbrr(), content, Path(tmp) / "out.torrent")
            meta = torrent.decode(data)
        info = meta[b"info"]
        self.assertEqual(info[b"name"].decode(), content.name)
        self.assertEqual((info[b"private"], info[b"source"]), (1, b"nCore"))
        self.assertNotIn(b"creation date", meta)
        self.assertEqual(sorted(b"/".join(f[b"path"]) for f in info[b"files"]), [b"Sample/sample.mkv", b"film.mkv", b"grp.nfo"])


class FakeQbit:
    def __init__(self, *, login="Ok.", add="Ok.", add_status=200, items=None, fail=None):
        self.calls = []
        self.login, self.add, self.add_status = login, add, add_status
        self.items = items if items is not None else [{"hash": "ab" * 20, "state": "stalledUP"}]
        self.fail = fail

    def post(self, url, data=None, files=None, **kw):
        self.calls.append((url, data, files))
        if self.fail:
            raise self.fail
        if url.endswith("/auth/login"):
            return answer(self.login, 200 if self.login != "403" else 403)
        return answer(self.add, self.add_status)

    def get(self, url, params=None, **kw):
        self.calls.append((url, params, None))
        return answer(json.dumps(self.items))


class QbitTests(unittest.TestCase):
    def add(self, session, **kw):
        args = dict(url="http://127.0.0.1:8080/", user="u", password="p", torrent=b"d4:infode", save_path="/data/.ncore-up",
                    skip_checking=True, category="ncore", tags="ncore-up", info_hash="AB" * 20, wait=0, session=session)
        args.update(kw)
        with mock.patch.object(qbit.time, "sleep"):
            return qbit.add_torrent(**args)

    def test_add_with_layout_and_check(self):
        session = FakeQbit()
        self.assertEqual(self.add(session), "stalledUP")
        login, add, lookup = session.calls
        self.assertEqual(login[0], "http://127.0.0.1:8080/api/v2/auth/login")
        data = add[1]
        self.assertEqual((data["savepath"], data["contentLayout"], data["autoTMM"], data["skip_checking"]), ("/data/.ncore-up", "Original", "false", "true"))
        self.assertEqual((data["category"], data["tags"], data["paused"], data["stopped"]), ("ncore", "ncore-up", "false", "false"))
        self.assertEqual(add[2]["torrents"][2], "application/x-bittorrent")
        self.assertEqual(lookup[1], {"hashes": "ab" * 20})

    def test_login_problems(self):
        with self.assertRaises(qbit.QbitError):
            self.add(FakeQbit(login="Fails."))
        with self.assertRaises(qbit.QbitError) as ctx:
            self.add(FakeQbit(login="403"))
        self.assertIn("kitiltotta", str(ctx.exception))

    def test_no_user_skips_login(self):
        session = FakeQbit()
        self.add(session, user="")
        self.assertFalse(any(url.endswith("/auth/login") for url, *_ in session.calls))

    def test_unreachable(self):
        with self.assertRaises(qbit.QbitError) as ctx:
            self.add(FakeQbit(fail=requests.ConnectionError("refused")))
        self.assertIn("nem érhető el", str(ctx.exception))

    def test_refused_but_already_there(self):
        self.assertEqual(self.add(FakeQbit(add="Fails.")), "stalledUP")
        with self.assertRaises(qbit.QbitError):
            self.add(FakeQbit(add="Fails.", items=[]))

    def test_json_answers(self):
        self.assertEqual(self.add(FakeQbit(add='{"success_count": 1, "failure_count": 0, "pending_count": 0}')), "stalledUP")
        with self.assertRaises(qbit.QbitError):
            self.add(FakeQbit(add='{"success_count": 0, "failure_count": 1}', items=[]))

    def test_missing_files_is_an_error(self):
        with self.assertRaises(qbit.QbitError) as ctx:
            self.add(FakeQbit(items=[{"hash": "ab" * 20, "state": "missingFiles"}]))
        self.assertIn("mentési helyet", str(ctx.exception))

    def test_not_appearing_is_an_error(self):
        with self.assertRaises(qbit.QbitError):
            self.add(FakeQbit(items=[]))

    def test_without_hash_no_lookup(self):
        session = FakeQbit()
        self.assertEqual(self.add(session, info_hash=""), "")
        self.assertEqual(len(session.calls), 2)

    def test_login_required(self):
        with self.assertRaises(qbit.QbitError):
            self.add(FakeQbit(add="Forbidden", add_status=403), user="")


class OriginalFilesTests(unittest.TestCase):
    def inspect(self, files: dict[str, int]) -> rel.Release:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "Movie.2020.1080p.BluRay.x264-GRP"
        for name, size in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(b"x" * size)
        with mock.patch.object(rel, "hungarian_audio", return_value=None):
            return rel.inspect(root)

    def test_sfv_and_junk_are_fine(self):
        release = self.inspect({"movie.mkv": 10, "grp.nfo": 1, "grp.sfv": 1, "Thumbs.db": 1, "Sample/.DS_Store": 1, "Sample/sample.mkv": 2})
        self.assertEqual(rel.validate_original(release, cats.CATEGORIES["hd"]), [])

    def test_unfinished_download(self):
        release = self.inspect({"movie.mkv.!qB": 10, "grp.nfo": 1})
        self.assertEqual(rel.validate_original(release, cats.CATEGORIES["hd"]), ["befejezetlen letöltés: movie.mkv.!qB"])


if __name__ == "__main__":
    unittest.main()
