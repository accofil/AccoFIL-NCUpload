from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import cli, compose, credentials, diagnostics, keys, net
from ncore_up.sources import discogs, spotify
import test_music_resolve as music_tests
from test_music_resolve import Offline, omega_table, OMEGA
from ncore_up.resolve import music


ALBUM_ID = "A" * 22


def track(number):
    return {"disc_number": 1, "track_number": number, "name": f"Track {number}", "duration_ms": 180000, "artists": [{"name": "Artist"}]}


class SpotifyContinuationTests(unittest.TestCase):
    def setUp(self):
        self.keys = mock.patch.dict(keys._VALUES, {"SPOTIFY_CLIENT_ID": "fake-client", "SPOTIFY_CLIENT_SECRET": "fake-secret"}, clear=True)
        self.keys.start()
        self.addCleanup(self.keys.stop)
        old = net.FAILURES[:]
        net.FAILURES.clear()
        self.addCleanup(lambda: net.FAILURES.__setitem__(slice(None), old))

    def test_album_pagination_is_complete(self):
        raw = {"id": ALBUM_ID, "name": "Album", "total_tracks": 4, "tracks": {"items": [track(1), track(2)], "next": "https://untrusted.example/secret"}}
        page = {"offset": 2, "items": [track(3), track(4)]}
        with mock.patch.object(spotify, "_get", side_effect=[raw, page]) as get:
            album = spotify.album(ALBUM_ID)
        self.assertTrue(album["tracks_complete"])
        self.assertEqual(len(album["tracks"]), 4)
        self.assertEqual(get.call_args.args, (f"albums/{ALBUM_ID}/tracks", {"market": "HU", "limit": 50, "offset": 2}))

    def test_missing_page_is_not_complete(self):
        raw = {"id": ALBUM_ID, "total_tracks": 4, "tracks": {"items": [track(1), track(2)]}}
        with mock.patch.object(spotify, "_get", side_effect=[raw, {}]):
            album = spotify.album(ALBUM_ID)
        self.assertFalse(album["tracks_complete"])
        self.assertTrue(any("hiányos" in item for item in net.FAILURES))

    def test_repeated_page_is_not_counted_twice(self):
        raw = {"id": ALBUM_ID, "total_tracks": 4, "tracks": {"items": [track(1), track(2)]}}
        with mock.patch.object(spotify, "_get", side_effect=[raw, {"items": [track(1), track(2)]}]):
            album = spotify.album(ALBUM_ID)
        self.assertEqual(len(album["tracks"]), 2)
        self.assertFalse(album["tracks_complete"])

    def test_album_urls_and_ids(self):
        for value in (ALBUM_ID, "spotify:album:" + ALBUM_ID, "https://open.spotify.com/album/" + ALBUM_ID + "?si=x", "https://open.spotify.com/intl-hu/album/" + ALBUM_ID):
            self.assertEqual(spotify.normalize_album_id(value), ALBUM_ID)
        for value in ("https://evil.example/album/" + ALBUM_ID, "../secret", "short"):
            self.assertEqual(spotify.normalize_album_id(value), "")

    def test_unauthorized_refreshes_once(self):
        good = mock.Mock()
        good.json.return_value = {"ok": True}
        def get(*args, **kwargs):
            if not net.FAILURES:
                net.FAILURES.append("api.spotify.com: HTTP 401")
                return None
            return good
        with mock.patch.object(spotify, "token", side_effect=["old-token", "new-token"]) as token, mock.patch.object(net, "get", side_effect=get):
            self.assertEqual(spotify._get("search"), {"ok": True})
        self.assertEqual(token.call_args_list, [mock.call(), mock.call(force_refresh=True)])

    def test_other_network_failure_does_not_refresh(self):
        net.FAILURES.append("api.spotify.com: HTTP 401")
        def get(*args, **kwargs):
            net.FAILURES.append("api.spotify.com: HTTP 429")
            return None
        with mock.patch.object(spotify, "token", return_value="token") as token, mock.patch.object(net, "get", side_effect=get):
            self.assertEqual(spotify._get("search"), {})
        token.assert_called_once_with()

    def test_token_saved_privately_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(net, "cache_dir", return_value=Path(tmp)), \
                mock.patch.object(net, "post_json", return_value={"access_token": "fake-access-token", "expires_in": 3600}) as post:
            self.assertEqual(spotify.token(), "fake-access-token")
            self.assertEqual(spotify.token(), "fake-access-token")
            post.assert_called_once()
            if os.name != "nt":
                self.assertEqual((Path(tmp) / "spotify-token.json").stat().st_mode & 0o777, 0o600)

    def test_doctor_does_not_print_credentials(self):
        report = json.dumps(diagnostics.report())
        self.assertNotIn("fake-client", report)
        self.assertNotIn("fake-secret", report)


class SpotifyResolverTests(Offline):
    def test_explicit_album_uses_local_tracklist_and_credits_source(self):
        release, names = music_tests.MusicResolveTests.omega_release(self)
        data = {"id": ALBUM_ID, "name": "10000 lépés", "artist": "Omega", "release_date": "1969-01-01", "total_tracks": 10,
                "tracks_complete": True, "tracks": [{"disc": 1, "number": i, "title": title, "length": seconds, "artist": ""}
                 for i, (title, seconds) in enumerate(OMEGA, 1)], "url": "https://open.spotify.com/album/" + ALBUM_ID}
        with self.tags_for(omega_table(names)), mock.patch.object(spotify, "album", return_value=data):
            meta = music.resolve(release, "mp3_hun", original=False, hints={"spotify": ALBUM_ID})
        self.assertEqual(meta.identifiers["spotify"], ALBUM_ID)
        self.assertEqual(len(meta.tracks), 10)
        self.assertEqual(compose.music(meta).count(data["url"]), 1)
        self.assertIn("Igazoló link: " + data["url"], compose.music(meta))


class KeyAndDoctorTests(unittest.TestCase):
    def test_discogs_accepts_consumer_pair_for_public_database(self):
        values = {"DISCOGS_CONSUMER_KEY": "fake-key", "DISCOGS_CONSUMER_SECRET": "fake-secret"}
        with mock.patch.dict(keys._VALUES, values, clear=True):
            self.assertEqual(discogs._headers()["Authorization"], "Discogs key=fake-key, secret=fake-secret")
            row = next(s for s in diagnostics.report()["sources"] if s["id"] == "discogs")
            self.assertEqual(row["status"], "NOT_CHECKED")
            self.assertEqual(row["missing_keys"], [])
            self.assertNotIn("DISCOGS_TOKEN", keys.missing())

    def test_discogs_token_takes_precedence(self):
        values = {"DISCOGS_TOKEN": "fake-token", "DISCOGS_CONSUMER_KEY": "fake-key", "DISCOGS_CONSUMER_SECRET": "fake-secret"}
        with mock.patch.dict(keys._VALUES, values, clear=True):
            self.assertEqual(discogs._headers()["Authorization"], "Discogs token=fake-token")

    def test_discogs_pair_import_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / "incoming.env", Path(tmp) / "config.env"
            dest.write_text("TMDB_API=untouched\n")
            source.write_text("DISCOGS_CONSUMER_KEY=fake-key\n")
            with self.assertRaisesRegex(ValueError, "együtt szükséges"):
                credentials.import_keys(source, dest)
            self.assertEqual(dest.read_text(), "TMDB_API=untouched\n")
            source.write_text("DISCOGS_CONSUMER_KEY=fake-key\nDISCOGS_CONSUMER_SECRET=fake-secret\n")
            self.assertEqual(credentials.import_keys(source, dest), ["DISCOGS_CONSUMER_KEY", "DISCOGS_CONSUMER_SECRET"])

    def test_discogs_live_probe_sanitizes_result(self):
        row = {"id": "discogs", "status": "NOT_CHECKED"}
        response = mock.Mock(status_code=200)
        response.json.return_value = {"results": [{"id": 1}]}
        with mock.patch.dict(keys._VALUES, {"DISCOGS_TOKEN": "fake-token"}, clear=True), mock.patch.object(net, "get", return_value=response) as get:
            result = diagnostics._probe(row)
        self.assertEqual(result["status"], "OK")
        self.assertNotIn("fake-token", json.dumps(result))
        self.assertEqual(get.call_args.kwargs["expire"], 0)

    def test_books_live_probe_checks_search_instead_of_key_presence(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"items": [{"id": "test-volume", "volumeInfo": {"title": "The Google Story"}}]}
        with mock.patch.dict(keys._VALUES, {"GOOGLE_BOOKS_KEY": "fake-books-key"}, clear=True), mock.patch.object(net, "get", return_value=response) as get:
            result = diagnostics._probe({"id": "googlebooks", "status": "NOT_CHECKED"})
        self.assertEqual(result["status"], "OK")
        self.assertEqual(get.call_args.args[0], "https://books.googleapis.com/books/v1/volumes")
        self.assertEqual(get.call_args.kwargs["params"]["printType"], "BOOKS")
        self.assertEqual(get.call_args.kwargs["expire"], 0)
        self.assertEqual(get.call_args.kwargs["retries"], 1)
        self.assertEqual(get.call_args.kwargs["timeout"], 10)
        self.assertNotIn("fake-books-key", json.dumps(result))

    def test_books_live_probe_does_not_report_empty_results_as_success(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"totalItems": 0}
        with mock.patch.object(net, "get", return_value=response):
            result = diagnostics._probe({"id": "googlebooks", "status": "NOT_CHECKED"})
        self.assertEqual(result["status"], "INVALID_RESPONSE")

    def test_import_preserves_other_config_and_filters_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest, source = root / "config.env", root / "incoming.env"
            dest.write_text("# private settings\nQB_URL=http://localhost:8080\nTMDB_API=old-tmdb\nNCORE_SPOTIFY_CLIENT_ID=old-id\n")
            source.write_text("SPOTIFY_CLIENT_ID=new-id\nSPOTIFY_CLIENT_SECRET=new-secret\n")
            self.assertEqual(credentials.import_keys(source, dest), ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"])
            text = dest.read_text()
            self.assertIn("QB_URL=http://localhost:8080", text)
            self.assertIn("TMDB_API=old-tmdb", text)
            self.assertNotIn("old-id", text)
            if os.name != "nt":
                self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

    def test_partial_pair_rejected_without_replacing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest, source = root / "config.env", root / "incoming.env"
            dest.write_text("TMDB_API=untouched\n")
            source.write_text("SPOTIFY_CLIENT_ID=id-only\n")
            with self.assertRaisesRegex(ValueError, "együtt szükséges"):
                credentials.import_keys(source, dest)
            self.assertEqual(dest.read_text(), "TMDB_API=untouched\n")

    def test_unknown_fields_do_not_replace_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "incoming.env"
            source.write_text("QB_PASS=DO_NOT_ACCEPT\n")
            with self.assertRaises(ValueError) as ctx:
                credentials.import_keys(source, Path(tmp) / "config.env")
            self.assertNotIn("DO_NOT_ACCEPT", str(ctx.exception))

    def test_doctor_does_not_require_ncore_cookies_or_network(self):
        with mock.patch.object(cli.Config, "load"), mock.patch.object(cli, "load_cookies") as cookies, \
                mock.patch.object(net, "request", side_effect=AssertionError("network")), mock.patch.object(cli, "say") as say:
            self.assertEqual(cli.main(["--doctor", "--json"]), 0)
        cookies.assert_not_called()
        self.assertIn("sources", json.loads(say.call_args.args[0]))

    def test_import_and_doctor_emit_single_json_document(self):
        with mock.patch.object(cli.Config, "load"), mock.patch.object(credentials, "import_keys", return_value=["DISCOGS_TOKEN"]), mock.patch.object(cli, "say") as say:
            self.assertEqual(cli.main(["--import-keys", "private.env", "--doctor", "--json"]), 0)
        say.assert_called_once()
        self.assertEqual(json.loads(say.call_args.args[0])["imported_fields"], ["DISCOGS_TOKEN"])

    def test_missing_keys_are_not_probed(self):
        with mock.patch.dict(keys._VALUES, {}, clear=True), mock.patch.object(diagnostics, "_probe", side_effect=lambda value: value) as probe:
            report = diagnostics.report(live=True)
        self.assertNotIn("spotify", [call.args[0]["id"] for call in probe.call_args_list])
        self.assertEqual(next(s["status"] for s in report["sources"] if s["id"] == "spotify"), "MISSING_CREDENTIALS")
