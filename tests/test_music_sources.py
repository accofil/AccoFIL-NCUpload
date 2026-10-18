from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import keys, net
from ncore_up.sources import acoustid, coverart, discogs, lastfm, musicbrainz as mb, setlistfm, spotify, theaudiodb

FIX = Path(__file__).parent / "fixtures" / "music"


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class NoNetwork:
    """Fail loudly if a parser test reaches the network layer."""

    def __enter__(self):
        self.patch = mock.patch.object(net, "request", side_effect=AssertionError("network call"))
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class MusicBrainzTests(unittest.TestCase):
    def test_search(self):
        hits = mb.parse_search(fixture("mb_search_omega.json"))
        first = hits[0]
        self.assertEqual((first["id"], first["title"], first["artist"], first["year"], first["country"]),
                         ("89bd26a2-4d46-334f-91c2-5e8df803dab3", "10000 lépés", "Omega", "1969", "HU"))
        self.assertEqual((first["track_count"], first["formats"], first["primary_type"]), (10, ['12" Vinyl'], "Album"))
        self.assertEqual(first["labels"], [{"name": "Qualiton", "catno": "LPX 17400"}])

    def test_release(self):
        release = mb.parse_release(fixture("mb_release_omega.json"))
        self.assertEqual((release["title"], release["artist"], release["label"], release["catno"]), ("10000 lépés", "Omega", "Qualiton", "LPX 17400"))
        self.assertEqual(release["track_count"], 10)
        first = release["media"][0]["tracks"][0]
        self.assertEqual((first["number"], first["title"], first["length"]), ("1", "Petróleum lámpa", 201.0))
        self.assertEqual(release["discogs_url"], "https://www.discogs.com/release/6577918")
        self.assertEqual(release["artist_types"]["819e4775-b9a6-4a24-b4af-0acfa9669a00"], "Group")
        self.assertIn(("psychedelic rock", 1), release["release_group"]["genres"])
        self.assertTrue(release["artist_genres"])
        self.assertEqual(release["url"], "https://musicbrainz.org/release/89bd26a2-4d46-334f-91c2-5e8df803dab3")
        self.assertTrue(release["cover_front"])
        dsotm = mb.parse_release(fixture("mb_release_dsotm.json"))
        self.assertEqual((dsotm["barcode"], dsotm["country"], dsotm["track_count"]), ("724382975229", "GB", 9))
        self.assertIn("8jTZJTgjGGtKjwdt8UyAgK3DnbY-", dsotm["media"][0]["discids"])
        self.assertEqual(dsotm["release_group"]["genres"][0], ("progressive rock", 42))
        self.assertEqual(mb.parse_release({}), {})

    def test_discid_artist_group(self):
        releases = mb.parse_discid(fixture("mb_discid_dsotm.json"))
        self.assertTrue(releases)
        self.assertTrue(all(any("8jTZJTgjGGtKjwdt8UyAgK3DnbY-" in m["discids"] for m in r["media"]) for r in releases))
        artist = mb.parse_artist(fixture("mb_artist_pf.json"))
        self.assertEqual((artist["name"], artist["type"], artist["wikidata"], artist["official"]),
                         ("Pink Floyd", "Group", "Q2306", "https://www.pinkfloyd.com/"))
        group = mb.parse_release_group(fixture("mb_rg_dsotm.json"))
        self.assertEqual(group["id"], "f5093c06-23e3-404f-aeaa-40f72885ee3a")
        self.assertEqual(group["primary_type"], "Album")
        hits = mb.parse_artist_search(fixture("mb_artist_search.json"))
        self.assertEqual(hits[0]["name"], "Pink Floyd")

    def test_query(self):
        self.assertEqual(mb.words("10 000 lépés"), "10000 lépés")
        self.assertEqual(mb.words("Ágy, asztal, tv"), "Ágy asztal tv")
        self.assertEqual(mb.escape('AC/DC: "x"'), 'AC\\/DC\\: \\"x\\"')
        query = mb.build_query("Kispál és a Borz", "Ágy, asztal, tv", tracks=14, formats=("DVD-Video", "Blu-ray"))
        self.assertEqual(query, 'release:(Ágy asztal tv) AND artist:(Kispál és a Borz) AND tracks:14 AND (format:"DVD-Video" OR format:"Blu-ray")')
        self.assertEqual(mb.build_query(catno='523 249-4 "A"'), 'catno:"523 249-4 \\"A\\""')
        # upper-case AND/OR/NOT/TO are Lucene operators: 'I AM NOT A ROBOT' must stay a word group
        self.assertEqual(mb.words("I AM NOT A ROBOT"), "I AM not A ROBOT")
        self.assertEqual(mb.build_query("METALLICA", "...AND JUSTICE FOR ALL"), "release:(and JUSTICE FOR ALL) AND artist:(METALLICA)")

    def test_unknown_disc_is_not_a_source_failure(self):
        with mock.patch.object(net, "get_json", return_value=None) as call:
            self.assertEqual(mb.discid_lookup("8jTZJTgjGGtKjwdt8UyAgK3DnbY-", "1+9+193293+183"), [])
        self.assertTrue(call.call_args.kwargs["quiet"])
        self.assertEqual(call.call_args.kwargs["params"]["toc"], "1+9+193293+183")

    def test_no_network_on_bad_input_and_failure(self):
        with NoNetwork():
            self.assertEqual(mb.release("not-an-id"), {})
            self.assertEqual(mb.by_barcode("12"), [])
            self.assertEqual(mb.discid_lookup("bad id", ""), [])
            self.assertEqual(mb.search(""), [])
        with mock.patch.object(net, "get_json", return_value=None):
            self.assertEqual(mb.release("89bd26a2-4d46-334f-91c2-5e8df803dab3"), {})
            self.assertEqual(mb.search_releases("Omega", "10000 lépés"), [])


class DiscogsTests(unittest.TestCase):
    def test_search(self):
        hits = discogs.parse_search(fixture("discogs_search_dsotm.json"))
        first = hits[0]
        self.assertEqual((first["artist"], first["album"], first["type"]), ("Pink Floyd", "Dark Side Of The Moon", "release"))
        self.assertTrue(first["url"].startswith("https://www.discogs.com/release/"))
        self.assertIn("Unofficial Release", first["formats"])

    def test_release(self):
        release = discogs.parse_release(fixture("discogs_release_kispal.json"))
        self.assertEqual((release["id"], release["title"], release["year"], release["country"]), (3194229, "Ágy Asztal TV", "1994", "Hungary"))
        self.assertEqual((release["styles"], release["genres"]), (["Alternative Rock"], ["Rock"]))
        self.assertEqual(release["catno"], "523 249-4")
        first = release["tracks"][0]
        self.assertEqual((first["position"], first["number"], first["title"], first["duration"]), ("A1", "A1", "Neked Nem", 40.0))
        self.assertEqual(release["url"], "https://www.discogs.com/release/3194229")
        self.assertFalse(release["is_video"])

    def test_video_release_sections(self):
        release = discogs.parse_release(fixture("discogs_release_pulse_dvd.json"))
        self.assertTrue(release["is_video"])
        main = discogs.main_tracks(release["tracks"])
        titles = [t["title"] for t in main]
        self.assertEqual(titles[0], "Shine On You Crazy Diamond (Concert Version)")
        self.assertNotIn("What Do You Want From Me?", titles)  # 'Features' / 'Bootlegging The Bootleggers'
        self.assertNotIn("Pulse TV Ad", titles)
        self.assertIn("Comfortably Numb", titles)  # 'Pulse Two' is a main section again
        self.assertEqual(main[0]["disc"], 1)

    def test_helpers(self):
        self.assertEqual(discogs.split_position("1-01"), (1, "01"))
        self.assertEqual(discogs.split_position("CD2-3"), (2, "3"))
        self.assertEqual(discogs.split_position("2.5"), (2, "5"))
        self.assertEqual(discogs.split_position("A1"), (0, "A1"))
        self.assertEqual((discogs.duration_seconds("3:21"), discogs.duration_seconds("1:02:03"), discogs.duration_seconds("")), (201.0, 3723.0, 0.0))
        self.assertEqual(discogs.release_id_from_url("https://www.discogs.com/release/6577918-Omega-10000-L%C3%A9p%C3%A9s"), "6577918")
        master = discogs.parse_master(fixture("discogs_master_380314.json"))
        self.assertEqual(master["styles"], ["Alternative Rock", "Pop Rock"])

    def test_token_header(self):
        with mock.patch.object(keys, "get", side_effect=lambda name: "TOK" if name == "DISCOGS_TOKEN" else ""):
            self.assertEqual(discogs._headers()["Authorization"], "Discogs token=TOK")
        with mock.patch.object(keys, "get", return_value=""):
            self.assertNotIn("Authorization", discogs._headers())
        with NoNetwork():
            self.assertEqual(discogs.release("abc"), {})
            self.assertEqual(discogs.search(), [])


class SmallSourcesTests(unittest.TestCase):
    def test_coverart(self):
        info = coverart.parse_index(fixture("caa_index.json"))
        self.assertTrue(info["front"]["500"].endswith("-500.jpg"))
        self.assertEqual(coverart.front_url("f5093c06-23e3-404f-aeaa-40f72885ee3a", group=True),
                         "https://coverartarchive.org/release-group/f5093c06-23e3-404f-aeaa-40f72885ee3a/front-500")
        self.assertTrue(coverart.is_image(b"\xff\xd8\xff\xe0rest"))
        self.assertFalse(coverart.is_image(b"<html>"))
        with mock.patch.object(net, "get_bytes", return_value=b"<html>not found</html>"):
            self.assertEqual(coverart.front("f5093c06-23e3-404f-aeaa-40f72885ee3a"), b"")

    def test_acoustid(self):
        sample = {"status": "ok", "results": [
            {"id": "r1", "score": 0.96, "recordings": [{"id": "rec1", "title": "Time", "duration": 413, "artists": [{"name": "Pink Floyd"}],
                                                         "releases": [{"id": "A"}, {"id": "B"}],
                                                         "releasegroups": [{"id": "G", "title": "DSOTM", "type": "Album", "releases": [{"id": "C"}]}]}]},
            {"id": "r2", "score": 0.4, "recordings": [{"id": "rec2", "releases": [{"id": "Z"}]}]}]}
        results = acoustid.parse_lookup(sample)
        self.assertEqual(results[0]["recordings"][0]["release_ids"], ["A", "B", "C"])
        self.assertEqual(acoustid.release_ids(results), {"A", "B", "C"})
        other = acoustid.parse_lookup({"status": "ok", "results": [{"score": 0.9, "recordings": [{"releases": [{"id": "B"}, {"id": "D"}]}]}]})
        self.assertEqual(acoustid.common_releases([results, other])[0], ("B", 2))
        self.assertEqual(acoustid.parse_lookup({"status": "error", "error": {"code": 4}}), [])
        with NoNetwork(), mock.patch.object(keys, "get", return_value=""):
            self.assertEqual(acoustid.lookup("AQAA", 100), [])

    def test_lastfm(self):
        sample = {"album": {"name": "10000 lépés", "artist": "Omega", "mbid": "", "url": "https://www.last.fm/music/Omega/10000+l%C3%A9p%C3%A9s",
                            "tags": {"tag": [{"name": "Hungarian", "url": "x"}, {"name": "psychedelic rock", "url": "y"}]},
                            "tracks": {"track": [{"name": "Petróleum lámpa", "duration": 201, "@attr": {"rank": 1}}]},
                            "wiki": {"published": "01 Jan 2010", "summary": 'The second album of Omega. <a href="https://www.last.fm/music/Omega/10000+l%C3%A9p%C3%A9s">Read more on Last.fm</a>.',
                                     "content": "Long text. User-contributed text is available under the Creative Commons By-SA License"}}}
        album = lastfm.parse_album(sample)
        self.assertEqual(album["wiki_summary"], "The second album of Omega.")
        self.assertEqual(album["wiki_content"], "Long text.")
        self.assertEqual(album["tags"], [("psychedelic rock", 100)])
        self.assertEqual(album["tracks"][0], {"rank": 1, "name": "Petróleum lámpa", "duration": 201})
        top = lastfm.parse_tags({"toptags": {"tag": [{"name": "Progressive Rock", "count": 100}, {"name": "seen live", "count": 40}, {"name": "70s", "count": "33"}]}})
        self.assertEqual(top, [("progressive rock", 100), ("70s", 33)])
        with NoNetwork(), mock.patch.object(keys, "get", return_value=""):
            self.assertEqual(lastfm.album_info("Omega", "10000 lépés"), {})
            self.assertEqual(lastfm.artist_top_tags("Omega"), [])
        with mock.patch.object(keys, "get", return_value="k"), mock.patch.object(net, "get_json", return_value={"error": 6, "message": "x"}):
            self.assertEqual(lastfm.album_top_tags("Omega", "x"), [])

    def test_setlistfm(self):
        sample = {"type": "setlists", "setlist": [{"id": "63de4613", "eventDate": "23-08-1994", "url": "https://www.setlist.fm/setlist/pink-floyd/1994/x.html",
                                                   "artist": {"mbid": "83d91898-7763-47d7-b03b-b92132375c47", "name": "Pink Floyd"},
                                                   "venue": {"name": "Népstadion", "city": {"name": "Budapest", "country": {"code": "HU", "name": "Hungary"}}},
                                                   "tour": {"name": "The Division Bell Tour"},
                                                   "sets": {"set": [{"song": [{"name": "Intro", "tape": True}, {"name": "Astronomy Domine"}, {"name": "Learning to Fly"}]},
                                                                    {"encore": 1, "song": [{"name": "Hey You"}]}, {"encore": 2, "song": [{"name": "Run Like Hell"}]}]}}]}
        found = setlistfm.parse_setlists(sample)
        self.assertEqual((found[0]["date"], found[0]["venue"], found[0]["city"]), ("1994-08-23", "Népstadion", "Budapest"))
        songs = setlistfm.songs(found[0])
        self.assertEqual([s["title"] for s in songs], ["Astronomy Domine", "Learning to Fly", "Hey You", "Run Like Hell"])
        self.assertEqual([s["section"] for s in songs], ["", "", "Ráadás", "Ráadás 2"])
        with NoNetwork(), mock.patch.object(keys, "get", return_value=""):
            self.assertEqual(setlistfm.search(artist_name="Pink Floyd", date="1994-08-23"), [])
        with mock.patch.object(keys, "get", return_value="k"), mock.patch.object(net, "get_json", return_value=sample) as call:
            self.assertEqual(len(setlistfm.search(artist_mbid="83d91898-7763-47d7-b03b-b92132375c47", date="1994-08-23")), 1)
            self.assertEqual(call.call_args.kwargs["params"]["date"], "23-08-1994")
            self.assertEqual(call.call_args.kwargs["headers"]["x-api-key"], "k")

    def test_spotify(self):
        with NoNetwork(), mock.patch.object(keys, "get", return_value=""):
            self.assertFalse(spotify.enabled())
            self.assertEqual(spotify.search_albums("Omega", "10000 lépés"), [])
            self.assertEqual(spotify.album("x"), {})
        album = spotify.parse_album({"id": "a1", "name": "10000 lépés", "artists": [{"name": "Omega"}], "release_date": "1969-01-01", "total_tracks": 1,
                                     "images": [{"url": "big", "width": 640}, {"url": "mid", "width": 300}, {"url": "small", "width": 64}],
                                     "external_ids": {"upc": "5991811"}, "external_urls": {"spotify": "https://open.spotify.com/album/a1"},
                                     "tracks": {"items": [{"disc_number": 1, "track_number": 1, "name": "Petróleum lámpa", "duration_ms": 201000,
                                                           "artists": [{"name": "Omega"}]}]}})
        self.assertEqual((album["cover_url"], album["upc"], album["tracks"][0]["length"], album["tracks"][0]["artist"]), ("mid", "5991811", 201.0, ""))
        hits = spotify.parse_search({"albums": {"items": [{"id": "a1", "name": "X", "artists": [{"name": "Y"}], "total_tracks": 3, "images": []}]}})
        self.assertEqual((hits[0]["name"], hits[0]["artist"], hits[0]["cover_url"]), ("X", "Y", ""))

    def test_theaudiodb(self):
        artist = theaudiodb.parse_artist(fixture("tadb_artist.json"))
        self.assertTrue(artist["biography_hu"].startswith("A Pink Floyd 1965-ben"))
        self.assertEqual(artist["mbid"], "83d91898-7763-47d7-b03b-b92132375c47")
        self.assertEqual(theaudiodb.parse_artist({"artists": None}), {})
        album = theaudiodb.parse_album({"album": [{"idAlbum": "1", "strAlbum": "Sika, kasza, léc", "strDescriptionHU": None, "strDescriptionEN": "An album."}]})
        self.assertEqual((album["name"], album["description_hu"], album["description_en"]), ("Sika, kasza, léc", "", "An album."))
        with NoNetwork():
            self.assertEqual(theaudiodb.artist("nope"), {})


if __name__ == "__main__":
    unittest.main()
