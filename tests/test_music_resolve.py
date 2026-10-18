from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import keys, net
from ncore_up import release as rel
from ncore_up.local import audiotags, chapters as chaps
from ncore_up.meta import Track
from ncore_up.resolve import clip, music
from ncore_up.sources import coverart, discogs, musicbrainz as mb, setlistfm, theaudiodb, wikidata, wikipedia

FIX = Path(__file__).parent / "fixtures" / "music"
OMEGA_ID = "89bd26a2-4d46-334f-91c2-5e8df803dab3"
OMEGA = [("Petróleum lámpa", 201), ("Gyöngyhajú lány", 349), ("Tűzvihar", 192), ("Udvari bolond kenyere", 211), ("Kérgeskezű favágók", 492),
         ("Tékozló fiúk", 279), ("10000 lépés", 376), ("Az 1958-as boogie-woogie klubban", 143), ("Spanyolgitár legenda", 208), ("Félbeszakadt koncert", 240)]
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
WIKI_HU = "1969-ben jelent meg a 10000 lépés, az Omega második magyar nagylemeze."


def fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def make_release(tmp: str, name: str, files: list[str]) -> rel.Release:
    root = Path(tmp) / name
    entries = []
    for item in files:
        path = root / item
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"x")
        entries.append(rel.ReleaseFile(item, path.stat().st_size))
    entries.sort(key=lambda f: f.relpath)
    return rel.Release(path=root, name=name, files=entries, nfo=None, has_bdmv=False, has_video_ts=False)


class Offline(unittest.TestCase):
    """Every outside call is mocked; the network layer itself raises."""

    def setUp(self):
        self.patches = [
            mock.patch.dict(keys._VALUES, clear=True),  # no keys, restored afterwards for the other test modules
            mock.patch.object(net, "request", side_effect=AssertionError("network call")),
            mock.patch.object(coverart, "front", return_value=JPEG),
            mock.patch.object(wikidata, "lookup", return_value={"qid": "Q1", "huwiki": "10000 lépés (album)", "enwiki": ""}),
            mock.patch.object(wikidata, "entity", return_value={}),
            mock.patch.object(wikipedia, "summary", return_value={"extract": WIKI_HU}),
            mock.patch.object(theaudiodb, "artist", return_value={}),
            mock.patch.object(discogs, "release", return_value={}),
            mock.patch.object(discogs, "search", return_value=[]),
            mock.patch.object(mb, "discid_lookup", return_value=[]),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.wiki_summary = self.mocks[5]
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tags_for(self, table: dict[str, audiotags.AudioTags]):
        return mock.patch.object(audiotags, "read", side_effect=lambda path, cover=False: table[Path(path).name])


def omega_table(names: list[str], *, tagged: bool = True, accents: bool = True, mbid: str = "", codec: str = "mp3", bitrate: int = 320000):
    table = {}
    for index, (name, (title, seconds)) in enumerate(zip(names, OMEGA), 1):
        if not accents:
            title = music.fold(title)
        item = audiotags.AudioTags(path=Path(name), duration=float(seconds), codec=codec, bitrate=bitrate, bitrate_mode="CBR", sample_rate=44100)
        if tagged:
            item.artist = item.albumartist = "Omega"
            item.album, item.title, item.date, item.track, item.track_total = "10 000 lépés", title, "1969", index, 10
            item.genres, item.tagged, item.mb_album_id = ["Rock"], True, mbid
        table[name] = item
    return table


class HelperTests(unittest.TestCase):
    def test_hungarian_grammar(self):
        cases = {1969: "ben", 1993: "ban", 1996: "ban", 1998: "ban", 2009: "ben", 2010: "ben", 1980: "ban", 1970: "ben", 2000: "ben",
                 2020: "ban", 1900: "ban", 1973: "ban", 2021: "ben", 1990: "ben", 2030: "ban"}
        for year, suffix in cases.items():
            self.assertEqual(music.year_suffix(year), suffix, year)
        self.assertEqual([music.article(n) for n in ("Omega", "Pink Floyd", "Illés", "Ős-Bikini", "U2", "5th Dimension", "30Y", "Edda Művek")],
                         ["az", "a", "az", "az", "a", "az", "a", "az"])
        self.assertEqual([music.article(n) for n in ("LGT", "R-GO", "KFT", "HBB", "NOX", "Republic", "Network")], ["az", "az", "a", "a", "a", "a", "a"])

    def test_fold_and_similarity(self):
        self.assertEqual(music.fold("10 000 lépés"), music.fold("10000 Lépés"))
        self.assertEqual(music.fold("The Dark Side of the Moon"), "dark side of the moon")
        self.assertGreater(music.similarity("Ágy, asztal, tv", "Ágy asztal TV"), 0.95)
        self.assertLess(music.similarity("Tűzvihar", "Money"), 0.5)

    def test_styles(self):
        self.assertEqual(music.map_style("Death Metal"), [("death.metal", 1.0), ("metal", 0.5)])
        self.assertEqual(music.map_style("Hungarian"), [])
        self.assertEqual(music.map_style("Synth-pop"), [("synth.pop", 1.0)])
        self.assertEqual(music.map_style("Melodic Black Metal"), [("metal", 1.0)])
        self.assertEqual(music.map_style("Népzene"), [("folk", 1.0), ("world.music", 0.5)])
        terms = music.style_terms({"release_group": {"genres": [("alternative rock", 2), ("rock", 2), ("pop", 1), ("pop rock", 1)]}},
                                  {"styles": ["Alternative Rock", "Pop Rock"], "genres": ["Rock", "Pop"]})
        self.assertEqual(set(music.style_tags(terms)), {"alternative", "rock", "pop.rock", "pop"})
        electronic = music.style_terms(None, {"styles": ["Hardcore"], "genres": ["Electronic"]})
        self.assertEqual(music.style_tags(electronic), ["hardcore.dance"])  # Discogs 'Hardcore' under Electronic is gabber
        live = music.style_terms({"release_group": {"genres": [("rock", 5)], "secondary_types": ["Live"]}})
        self.assertIn("live", music.style_tags(live))
        self.assertEqual(music.style_tags([("seen live", 10)]), [])
        self.assertEqual(len(music.style_tags([(g, 3) for g in ("rock", "pop", "jazz", "blues", "funk", "soul", "disco")])), 5)

    def test_template_sentence(self):
        meta = music.Meta(title="10000 lépés", creators=["Omega"], year="1969")
        self.assertEqual(music.template_sentence(meta, artist_type="Group", release_type="Album"), "10000 lépés: az Omega 1969-ben megjelent albuma.")
        self.assertEqual(music.template_sentence(meta, artist_type="Person"), "10000 lépés: Omega 1969-ben megjelent albuma.")
        self.assertEqual(music.template_sentence(meta, artist_type="Group", release_type="Album + Live"),
                         "10000 lépés: az Omega 1969-ben megjelent koncertalbuma.")
        self.assertEqual(music.template_sentence(meta, artist_type="Group", video=True), "10000 lépés: az Omega 1969-ben megjelent koncertfelvétele.")
        book = music.Meta(title="Egri csillagok", creators=["Gárdonyi Géza"])
        self.assertEqual(music.template_sentence(book, audiobook=True), "Gárdonyi Géza „Egri csillagok” című művének hangoskönyve.")
        self.assertEqual(music.template_sentence(music.Meta(title="Best of", creators=["Various Artists"])), "Best of – válogatásalbum.")
        self.assertEqual(music.template_sentence(music.Meta(title="X", creators=["Y"])), "X: Y albuma.")

    def test_names(self):
        self.assertEqual(music.parse_name("Pink Floyd - The Dark Side of the Moon (1973) [FLAC]"), ("Pink Floyd", "The Dark Side of the Moon", "1973"))
        self.assertEqual(music.parse_name("Omega-10000_Lepes-REMASTERED-CD-FLAC-1998-GRP"), ("Omega", "10000 Lepes", "1998"))
        self.assertEqual(music.parse_name("Kispal_es_a_Borz-Agy_Asztal_TV-(5232494)-CD-FLAC-1994-GRP")[:2], ("Kispal es a Borz", "Agy Asztal TV"))
        self.assertEqual(music.media_hint("Artist-Album-WEB-2026-GRP"), "Digital Media")
        self.assertEqual(music.media_hint("Artist-Album-VINYL-1969-GRP"), "Vinyl")
        self.assertEqual(music.media_hint("Artist - Album (2001)"), "")
        self.assertEqual(music.strip_artist_prefix("omega-petróleum lámpa", "Omega"), "petróleum lámpa")
        self.assertEqual(music.strip_artist_prefix("kispal es a borz-neked nem", "Kispál és a Borz"), "neked nem")
        self.assertEqual(music.strip_artist_prefix("Money", "Pink Floyd"), "Money")

    def test_duration_agreement(self):
        self.assertEqual(music.duration_agreement([200, 300], [201, 299]), 1.0)
        self.assertEqual(music.duration_agreement([200, 300], [260, 240]), 0.5)
        self.assertEqual(music.duration_agreement([200, 300], [0, 0]), 0.5)
        self.assertEqual(music.duration_agreement([200, 300], [500]), 0.5)
        self.assertEqual(music.cut_text("Egy. Kettő. " * 200, 100)[-1], ".")


class MusicResolveTests(Offline):
    def omega_release(self, name="Omega - 10 000 lépés (1969)"):
        names = [f"{i:02d} - {title}.mp3" for i, (title, _s) in enumerate(OMEGA, 1)]
        return make_release(self.tmp, name, names), names

    def test_loose_search_match(self):
        release, names = self.omega_release()
        with self.tags_for(omega_table(names)), \
                mock.patch.object(mb, "search_releases", return_value=mb.parse_search(fixture("mb_search_omega.json"))) as search, \
                mock.patch.object(mb, "release", side_effect=lambda rid: mb.parse_release(fixture("mb_release_omega.json")) if rid == OMEGA_ID else {}):
            meta = music.resolve(release, "mp3_hun", original=False, hints={})
        self.assertEqual(search.call_args.kwargs["tracks"], 10)
        self.assertEqual((meta.kind, meta.title, meta.creators, meta.year), ("album", "10000 lépés", ["Omega"], "1969"))
        self.assertGreaterEqual(meta.confidence, music.CONFIDENT)
        self.assertEqual((meta.publisher, meta.extra["catno"]), ("Qualiton", "LPX 17400"))
        self.assertEqual((meta.extra["format"], meta.extra["bitrate_kbps"], meta.extra["duration"]), ("MP3 320 kbps CBR", "320", "44:51"))
        self.assertEqual(meta.identifiers["mbid"], OMEGA_ID)
        self.assertEqual(meta.tracks[0], Track("01", "Petróleum lámpa", "3:21"))
        self.assertEqual(meta.tracks[9].position, "10")
        self.assertIn("rock", meta.tags)
        self.assertIn("psychedelic", meta.tags)
        self.assertEqual(meta.proof_links[0], f"https://musicbrainz.org/release/{OMEGA_ID}")
        self.assertEqual(meta.cover, JPEG)
        self.assertIn("Cover Art Archive", meta.sources)
        self.assertEqual((meta.description_hu, meta.description_source), (WIKI_HU, wikipedia.CREDIT))
        self.assertFalse(meta.machine_translated)
        self.assertFalse([w for w in meta.warnings if "azonosít" in w], meta.warnings)

    def test_spelling_fixed_only_when_confident(self):
        release, names = self.omega_release()
        with self.tags_for(omega_table(names, accents=False)), \
                mock.patch.object(mb, "search_releases", return_value=mb.parse_search(fixture("mb_search_omega.json"))), \
                mock.patch.object(mb, "release", side_effect=lambda rid: mb.parse_release(fixture("mb_release_omega.json")) if rid == OMEGA_ID else {}):
            meta = music.resolve(release, "mp3_hun", original=False, hints={})
        self.assertEqual([t.title for t in meta.tracks][:3], ["Petróleum lámpa", "Gyöngyhajú lány", "Tűzvihar"])

    def test_no_match_keeps_local_data_and_template(self):
        release, names = self.omega_release()
        with self.tags_for(omega_table(names)), mock.patch.object(mb, "search_releases", return_value=[]), \
                mock.patch.object(mb, "release", return_value={}):
            meta = music.resolve(release, "mp3_hun", original=False, hints={})
        self.assertLess(meta.confidence, 0.5)
        self.assertTrue(any("nem sikerült azonosítani" in w for w in meta.warnings))
        self.assertTrue(any("igazoló link" in w for w in meta.warnings))
        self.assertEqual((meta.title, meta.creators, meta.year), ("10 000 lépés", ["Omega"], "1969"))
        self.assertEqual(meta.tracks[1], Track("02", "Gyöngyhajú lány", "5:49"))
        self.assertEqual(meta.description_hu, "10 000 lépés: Omega 1969-ben megjelent albuma.")
        self.assertEqual(meta.tags, ["rock"])  # from the file tags
        self.assertEqual(meta.proof_links, [])
        self.wiki_summary.assert_not_called()  # no Wikipedia without an id

    def test_embedded_id_skips_search_and_original_has_description(self):
        release, names = self.omega_release("Omega-10000_Lepes-VINYL-1969-GRP")
        with self.tags_for(omega_table(names, mbid=OMEGA_ID)), mock.patch.object(mb, "search_releases", side_effect=AssertionError("search")), \
                mock.patch.object(mb, "release", side_effect=lambda rid: mb.parse_release(fixture("mb_release_omega.json")) if rid == OMEGA_ID else {}):
            meta = music.resolve(release, "mp3_hun", original=True, hints={})
        self.assertGreaterEqual(meta.confidence, 0.95)
        self.assertTrue(meta.description_hu or meta.description_en)
        self.assertEqual(meta.extra["media"], '12" Vinyl')

    def test_scene_file_names_without_tags(self):
        names = [f"{i:02d}-omega-{music.fold(title).replace(' ', '_')}.mp3" for i, (title, _s) in enumerate(OMEGA, 1)]
        release = make_release(self.tmp, "Omega-10000_Lepes-VINYL-1969-GRP", names)
        with self.tags_for(omega_table(names, tagged=False)), \
                mock.patch.object(mb, "search_releases", return_value=mb.parse_search(fixture("mb_search_omega.json"))) as search, \
                mock.patch.object(mb, "release", side_effect=lambda rid: mb.parse_release(fixture("mb_release_omega.json")) if rid == OMEGA_ID else {}):
            meta = music.resolve(release, "mp3_hun", original=False, hints={})
        self.assertEqual(search.call_args.args[:2], ("Omega", "10000 Lepes"))
        self.assertGreaterEqual(meta.confidence, music.CONFIDENT)
        self.assertEqual(meta.tracks[7].title, "Az 1958-as boogie-woogie klubban")
        self.assertIn("fájlnevek", meta.sources)

    def test_disc_id_from_eac_log(self):
        offsets, leadout = [183, 18018, 34143, 66003, 87533, 116218, 151488, 166913, 184200], 193293
        names = [f"Track{i:02d}.flac" for i in range(1, 10)]
        release = make_release(self.tmp, "PF_DSOTM_CD", names + ["rip.log"])
        shutil.copy(FIX / "eac_dsotm.log", release.path / "rip.log")
        table = {}
        for index, name in enumerate(names):
            end = offsets[index + 1] if index + 1 < len(offsets) else leadout
            table[name] = audiotags.AudioTags(path=Path(name), duration=(end - offsets[index]) / 75, codec="flac", sample_rate=44100, bits_per_sample=16,
                                              total_samples=(end - offsets[index]) * 588)
        discid_answer = mb.parse_discid(fixture("mb_discid_dsotm.json"))
        with self.tags_for(table), mock.patch.object(mb, "discid_lookup", return_value=discid_answer) as lookup, \
                mock.patch.object(mb, "release", return_value=mb.parse_release(fixture("mb_release_dsotm.json"))), \
                mock.patch.object(mb, "search_releases", side_effect=AssertionError("search")):
            meta = music.resolve(release, "lossless", original=False, hints={})
        self.assertEqual(lookup.call_args.args[0], "8jTZJTgjGGtKjwdt8UyAgK3DnbY-")
        self.assertEqual(meta.identifiers["discid"], "8jTZJTgjGGtKjwdt8UyAgK3DnbY-")
        self.assertGreaterEqual(meta.confidence, 0.9)
        self.assertEqual(meta.extra["format"], "FLAC 16/44.1")
        self.assertEqual(meta.tracks[2].title, "Time")  # 'Track03' is a placeholder: the database title is used
        self.assertIn("progressive.rock", meta.tags)

    def test_multi_disc_positions_and_mediums_query(self):
        names = [f"CD{d}/{i:02d} - Song {d}-{i}.flac" for d in (1, 2) for i in (1, 2)]
        release = make_release(self.tmp, "Artist - Double (1979)", names)
        table = {}
        for name in names:
            disc, number = int(name[2]), int(name[4:6])
            table[Path(name).name] = audiotags.AudioTags(path=Path(name), artist="Artist", albumartist="Artist", album="Double", date="1979",
                                                         title=f"Song {disc}-{number}", track=number, disc=disc, duration=100.0, codec="flac",
                                                         bits_per_sample=16, sample_rate=44100, tagged=True)
        with self.tags_for(table), mock.patch.object(mb, "search_releases", return_value=[]) as search, mock.patch.object(mb, "release", return_value={}):
            meta = music.resolve(release, "lossless", original=True, hints={})
        self.assertEqual(search.call_args_list[0].kwargs, {"limit": 40, "mediums": 2})
        self.assertEqual([t.position for t in meta.tracks], ["1-01", "1-02", "2-01", "2-02"])
        self.assertEqual(meta.extra["discs"], "2")

    def test_cue_image_disc_id(self):
        offsets, leadout = [183, 18018, 34143, 66003, 87533, 116218, 151488, 166913, 184200], 193293
        release = make_release(self.tmp, "Pink Floyd - Dark Side of the Moon (CD image)", ["image.flac", "image.cue"])
        lines = ['PERFORMER "Pink Floyd"', 'TITLE "Dark Side of the Moon"', 'FILE "image.wav" WAVE']
        for number, offset in enumerate(offsets, 1):
            frames = offset - 150
            lines += [f"  TRACK {number:02d} AUDIO", f'    TITLE "Song {number}"', f"    INDEX 01 {frames // 4500:02d}:{frames // 75 % 60:02d}:{frames % 75:02d}"]
        (release.path / "image.cue").write_text("\n".join(lines), encoding="utf-8")
        table = {"image.flac": audiotags.AudioTags(path=Path("image.flac"), duration=(leadout - 150) / 75, total_samples=(leadout - 150) * 588,
                                                   codec="flac", sample_rate=44100, bits_per_sample=16)}
        with self.tags_for(table), mock.patch.object(mb, "discid_lookup", return_value=mb.parse_discid(fixture("mb_discid_dsotm.json"))) as lookup, \
                mock.patch.object(mb, "release", return_value=mb.parse_release(fixture("mb_release_dsotm.json"))):
            meta = music.resolve(release, "lossless", original=True, hints={})
        self.assertEqual(lookup.call_args.args[0], "8jTZJTgjGGtKjwdt8UyAgK3DnbY-")
        self.assertEqual(len(meta.tracks), 9)
        self.assertEqual(meta.tracks[0].title, "Song 1")  # a different (not just misspelt) local title stays
        self.assertTrue(any("eltér az adatbázistól" in w for w in meta.warnings))
        self.assertEqual(meta.tracks[1].length, "3:35")  # INDEX 01 of track 2 -> track 3

    def test_various_artists_compilation(self):
        names = [f"{i:02d} - x.mp3" for i in range(1, 6)]
        release = make_release(self.tmp, "VA - Best of Rock (2001)", names)
        table = {n: audiotags.AudioTags(path=Path(n), artist=f"Artist {i}", album="Best of Rock", title=f"Hit {i}", track=i, date="2001",
                                        duration=200.0, codec="mp3", bitrate=256000, tagged=True) for i, n in enumerate(names, 1)}
        with self.tags_for(table), mock.patch.object(mb, "search_releases", return_value=[]) as search, mock.patch.object(mb, "release", return_value={}):
            meta = music.resolve(release, "mp3", original=False, hints={})
        self.assertEqual(search.call_args_list[0].args[:2], ("", "Best of Rock"))
        self.assertEqual(meta.creators, ["Various Artists"])
        self.assertEqual((meta.tracks[0].artist, meta.tracks[0].title), ("Artist 1", "Hit 1"))
        self.assertEqual(meta.description_hu, "Best of Rock – válogatásalbum.")

    def test_nfo_ids_and_hints(self):
        release = make_release(self.tmp, "Artist-Album-WEB-2026-GRP", ["01-artist-song.mp3", "artist-album.nfo"])
        (release.path / "artist-album.nfo").write_text("Label....: X\nCat.No...: ABC-123\nUPC/EAN : 0602527111605\n"
                                                       f"https://musicbrainz.org/release/{OMEGA_ID}\n", encoding="utf-8")
        release.nfo = release.path / "artist-album.nfo"
        self.assertEqual(music._nfo_ids(release), {"mbid": OMEGA_ID, "barcode": "0602527111605", "catno": "ABC-123"})
        table = {"01-artist-song.mp3": audiotags.AudioTags(path=Path("01-artist-song.mp3"), duration=10.0, codec="mp3", bitrate=320000)}
        with self.tags_for(table), mock.patch.object(mb, "release", return_value={}) as lookup, mock.patch.object(mb, "by_barcode", return_value=[]) as barcode, \
                mock.patch.object(mb, "by_catno", return_value=[]), mock.patch.object(mb, "search_releases", return_value=[]):
            meta = music.resolve(release, "mp3", original=False, hints={"album": "Lemez", "year": "2025"})
        self.assertEqual(lookup.call_args.args[0], OMEGA_ID)
        self.assertEqual(barcode.call_args.args[0], "0602527111605")
        self.assertEqual((meta.creators, meta.title, meta.year), (["Artist"], "Lemez", "2025"))
        self.assertEqual(meta.tracks[0].title, "song")  # 'artist-' prefix of the scene file name dropped

    def test_category_warnings(self):
        release, names = self.omega_release()
        with self.tags_for(omega_table(names, bitrate=96000)), mock.patch.object(mb, "search_releases", return_value=[]), \
                mock.patch.object(mb, "release", return_value={}):
            meta = music.resolve(release, "mp3", original=True, hints={})
            self.assertTrue(any("128 kbps" in w for w in meta.warnings))
            meta = music.resolve(release, "lossless", original=True, hints={"tags": "rock, beat"})
            self.assertTrue(any("veszteséges" in w for w in meta.warnings))
            self.assertEqual(meta.tags, ["rock", "beat"])

    def test_audiobook_m4b(self):
        release = make_release(self.tmp, "Gárdonyi Géza - Egri csillagok (hangoskönyv)", ["Egri csillagok.m4b"])
        table = {"Egri csillagok.m4b": audiotags.AudioTags(path=Path("x.m4b"), artist="Gárdonyi Géza", albumartist="Gárdonyi Géza", album="Egri csillagok",
                                                           title="Egri csillagok", narrator="Kaszás Attila", date="2015", duration=300.0, codec="aac",
                                                           bitrate=64000, genres=["Audiobook"], tagged=True)}
        named = chaps.parse_chapters(fixture("ffprobe_chapters_m4b.json"))
        with self.tags_for(table), mock.patch.object(chaps, "chapters", return_value=named), \
                mock.patch.object(mb, "search_releases", return_value=[]), mock.patch.object(mb, "release", return_value={}):
            meta = music.resolve(release, "mp3_hun", original=False, hints={})
        self.assertEqual((meta.kind, meta.title, meta.creators, meta.year), ("audiobook", "Egri csillagok", ["Gárdonyi Géza"], "2015"))
        self.assertEqual(meta.tags, ["hangoskönyv"])
        self.assertEqual(meta.extra["narrator"], "Kaszás Attila")
        self.assertEqual(len(meta.tracks), 5)
        self.assertEqual(meta.tracks[0], Track("01", "1. rész: Hol terem a magyar vitéz?", "1:00"))
        self.assertEqual(meta.description_hu, "Gárdonyi Géza „Egri csillagok” című művének hangoskönyve.")
        self.assertTrue(any("igazoló link" in w for w in meta.warnings))

    def test_audiobook_without_narrator_warns(self):
        release = make_release(self.tmp, "Szerző - Könyv hangoskönyv", ["01.mp3", "02.mp3"])
        table = {n: audiotags.AudioTags(path=Path(n), album="Könyv", artist="Szerző", title=f"{i}. fejezet", track=i, duration=600.0, codec="mp3",
                                        bitrate=128000, tagged=True) for i, n in enumerate(["01.mp3", "02.mp3"], 1)}
        with self.tags_for(table), mock.patch.object(mb, "search_releases", return_value=[]), mock.patch.object(mb, "release", return_value={}):
            meta = music.resolve(release, "mp3_hun", original=False, hints={"narrator": ""})
        self.assertEqual(meta.kind, "audiobook")
        self.assertTrue(any("felolvasó" in w for w in meta.warnings))
        self.assertEqual([t.title for t in meta.tracks], ["1. fejezet", "2. fejezet"])

    def test_empty_release(self):
        release = make_release(self.tmp, "Empty", ["readme.txt"])
        meta = music.resolve(release, "mp3", original=False, hints={})
        self.assertTrue(meta.warnings)
        self.assertEqual(meta.confidence, 0.0)

    def test_missing_code_and_url_hint(self):
        release, names = self.omega_release()
        with self.tags_for(omega_table(names)), mock.patch.object(mb, "search_releases", side_effect=AssertionError("search")), \
                mock.patch.object(mb, "release", side_effect=lambda rid: mb.parse_release(fixture("mb_release_omega.json")) if rid == OMEGA_ID else {}) as lookup:
            meta = music.resolve(release, None, original=True, hints={"mbid": f"https://musicbrainz.org/release/{OMEGA_ID}"})
        self.assertEqual(lookup.call_args.args[0], OMEGA_ID)
        self.assertEqual(meta.identifiers["mbid"], OMEGA_ID)

    def test_featuring_guests_are_not_various_artists(self):
        names = [f"{i:02d} - song.mp3" for i in range(1, 6)]
        release = make_release(self.tmp, "Gorillaz - Humanz (2017)", names)
        table = {n: audiotags.AudioTags(path=Path(n), artist=f"Gorillaz feat. Guest {i}", album="Humanz", title=f"Song {i}", track=i, date="2017",
                                        duration=200.0, codec="mp3", bitrate=320000, tagged=True) for i, n in enumerate(names, 1)}
        with self.tags_for(table), mock.patch.object(mb, "search_releases", return_value=[]) as search, mock.patch.object(mb, "release", return_value={}):
            meta = music.resolve(release, "mp3", original=True, hints={})
        self.assertEqual(search.call_args_list[0].args[:2], ("Gorillaz", "Humanz"))
        self.assertEqual(meta.creators, ["Gorillaz"])
        self.assertEqual(meta.tracks[0].artist, "Gorillaz feat. Guest 1")

    def test_flac_image_with_only_a_cuesheet_block(self):
        offsets, leadout = [183, 18018, 34143, 66003, 87533, 116218, 151488, 166913, 184200], 193293
        release = make_release(self.tmp, "PF_DSOTM_IMAGE", ["image.flac"])
        starts = [(o - 150) * 588 for o in offsets] + [(leadout - 150) * 588]
        table = {"image.flac": audiotags.AudioTags(path=Path("image.flac"), duration=(leadout - 150) / 75, total_samples=(leadout - 150) * 588,
                                                   codec="flac", sample_rate=44100, bits_per_sample=16, flac_toc=starts)}
        with self.tags_for(table), mock.patch.object(mb, "discid_lookup", return_value=mb.parse_discid(fixture("mb_discid_dsotm.json"))) as lookup, \
                mock.patch.object(mb, "release", return_value=mb.parse_release(fixture("mb_release_dsotm.json"))):
            meta = music.resolve(release, "lossless", original=True, hints={})
        self.assertEqual(lookup.call_args.args[0], "8jTZJTgjGGtKjwdt8UyAgK3DnbY-")
        self.assertEqual([t.position for t in meta.tracks], [f"{i:02d}" for i in range(1, 10)])
        self.assertEqual((meta.tracks[2].title, meta.tracks[1].length), ("Time", "3:35"))
        self.assertGreaterEqual(meta.confidence, 0.9)

    def test_broken_cover_file_and_unreadable_audio(self):
        release, names = self.omega_release()
        release = make_release(self.tmp, release.name, names + ["cover.jpg"])  # cover.jpg holds b"x", not an image
        table = omega_table(names)
        table[names[3]] = audiotags.AudioTags(path=Path(names[3]))  # zero-byte / corrupt file: nothing readable
        with self.tags_for(table), mock.patch.object(mb, "search_releases", return_value=[]), mock.patch.object(mb, "release", return_value={}):
            meta = music.resolve(release, "mp3_hun", original=True, hints={})
        self.assertEqual(meta.cover, b"")
        self.assertTrue(any("nincs borító" in w for w in meta.warnings))
        self.assertTrue(any("nem olvasható hangfájl (1 db" in w and names[3] in w for w in meta.warnings), meta.warnings)

    def test_nfo_without_catalogue_number(self):
        release = make_release(self.tmp, "Artist-Album-WEB-2026-GRP", ["01-artist-song.mp3", "artist-album.nfo"])
        (release.path / "artist-album.nfo").write_text("Label....: X\nCat.No...: N/A\n", encoding="utf-8")
        release.nfo = release.path / "artist-album.nfo"
        self.assertEqual(music._nfo_ids(release), {})

    def test_english_text_alone_keeps_its_credit(self):
        meta = music.Meta()
        with mock.patch.object(wikidata, "lookup", return_value={"qid": "Q1", "huwiki": "", "enwiki": "Album"}), \
                mock.patch.object(wikipedia, "summary", return_value={"extract": "An English article."}):
            music.describe_hu(meta, group={"id": "f5093c06-23e3-404f-aeaa-40f72885ee3a"}, artist_id="", artist_type="")
        self.assertEqual((meta.description_hu, meta.description_en), ("", "An English article."))
        self.assertEqual(meta.description_source, wikipedia.CREDIT)


class ClipResolveTests(Offline):
    def setUp(self):
        super().setUp()
        self.patches += [mock.patch.object(audiotags, "ffprobe_json", return_value={}),
                         mock.patch.object(mb, "search_artists", side_effect=lambda name: mb.parse_artist_search(fixture("mb_artist_search.json"))),
                         mock.patch.object(mb, "search_releases", return_value=[])]
        for patch in self.patches[-3:]:
            patch.start()
        self.pulse_hit = {"id": 999, "type": "release", "title": "Pink Floyd - Pulse", "artist": "Pink Floyd", "album": "Pulse", "year": "2006",
                          "country": "UK", "formats": ["DVD", "DVD-Video", "PAL"], "format_names": ["DVD"], "labels": [], "catno": "", "genres": [], "styles": [],
                          "barcodes": [], "master_id": 0, "cover_image": "", "url": "https://www.discogs.com/release/999"}

    def test_helpers(self):
        self.assertEqual(clip.concert_date("Artist.Live.2019.07.06.1080p"), "2019-07-06")
        self.assertEqual(clip.concert_date("Live 06.07.2019"), "2019-07-06")
        self.assertEqual(clip.concert_date("Pulse 1995"), "")
        artist, title, hit = clip.split_artist("Pink Floyd Pulse", lookup=lambda n: [{"name": "Pink Floyd", "aliases": []}] if n == "Pink Floyd" else [])
        self.assertEqual((artist, title, hit["name"]), ("Pink Floyd", "Pulse", "Pink Floyd"))
        self.assertEqual(clip.split_artist("Unknown Words", lookup=lambda n: [])[:2], ("", "Unknown Words"))

    def test_named_chapters_and_discogs_proof(self):
        release = make_release(self.tmp, "Pink.Floyd.Pulse.1995.1080p.BluRay.x264-GRP", ["pink.floyd.pulse.1995.1080p.bluray.x264-grp.mkv"])
        songs = ["Shine On You Crazy Diamond", "Learning to Fly", "High Hopes"]
        items = [{"index": i, "start": (i - 1) * 30.0, "end": i * 30.0, "title": s, "placeholder": False} for i, s in enumerate(songs, 1)]
        with mock.patch.object(chaps, "chapters", return_value=items), mock.patch.object(discogs, "search", return_value=[self.pulse_hit]), \
                mock.patch.object(discogs, "release", return_value=discogs.parse_release(fixture("discogs_release_pulse_dvd.json"))):
            meta = clip.resolve(release, "clip", original=False, hints={})
        self.assertEqual((meta.kind, meta.creators, meta.title, meta.year), ("concert", ["Pink Floyd"], "Pulse", "1995"))
        self.assertEqual(meta.tags[0], "live")
        self.assertIn("progressive.rock", meta.tags)
        self.assertEqual([t.title for t in meta.tracks], songs)
        self.assertEqual(meta.tracks[0].length, "0:30")
        self.assertIn("https://www.discogs.com/release/", meta.proof_links[0])
        self.assertEqual(meta.extra["format"], "MKV 1080p")
        self.assertEqual(meta.identifiers["mb_artist"], "83d91898-7763-47d7-b03b-b92132375c47")
        self.assertTrue(meta.description_hu.startswith("Pulse: a Pink Floyd 1995-ben megjelent koncertfelvétele."))

    def test_tracklist_from_discogs_without_named_chapters(self):
        release = make_release(self.tmp, "Pink Floyd - Pulse (1995)", ["pulse.mkv"])
        placeholders = [{"index": i, "start": 0.0, "end": 1.0, "title": f"Chapter {i:02d}", "placeholder": True} for i in range(1, 5)]
        with mock.patch.object(chaps, "chapters", return_value=placeholders), mock.patch.object(discogs, "search", return_value=[self.pulse_hit]), \
                mock.patch.object(discogs, "release", return_value=discogs.parse_release(fixture("discogs_release_pulse_dvd.json"))):
            meta = clip.resolve(release, "clip", original=False, hints={})
        titles = [t.title for t in meta.tracks]
        self.assertEqual(titles[0], "Shine On You Crazy Diamond (Concert Version)")
        self.assertNotIn("Pulse TV Ad", titles)
        self.assertEqual(meta.extra["chapters"], "4")

    def test_setlist_fallback(self):
        release = make_release(self.tmp, "Pink Floyd - Live in Budapest 1994.08.23", ["concert.mkv"])
        found = setlistfm.parse_setlists({"setlist": [{"id": "1", "eventDate": "23-08-1994", "url": "https://www.setlist.fm/setlist/x.html",
                                                       "artist": {"name": "Pink Floyd"}, "venue": {"name": "Népstadion", "city": {"name": "Budapest"}},
                                                       "sets": {"set": [{"song": [{"name": "Astronomy Domine"}]}, {"encore": 1, "song": [{"name": "Run Like Hell"}]}]}}]})
        with mock.patch.object(chaps, "chapters", return_value=[]), mock.patch.object(setlistfm, "search", return_value=found) as search:
            meta = clip.resolve(release, "clip", original=True, hints={})
        self.assertEqual(search.call_args.kwargs["date"], "1994-08-23")
        self.assertEqual(search.call_args.kwargs["city"], "Budapest")
        self.assertEqual([t.title for t in meta.tracks], ["Astronomy Domine", "Run Like Hell (Ráadás)"])
        self.assertEqual(meta.proof_links, ["https://www.setlist.fm/setlist/x.html"])
        self.assertEqual(meta.tags, ["live"])
        self.assertEqual(meta.description_hu, "")

    def test_video_release_without_artist_ids(self):
        release = make_release(self.tmp, "Somebody - Something (2001)", ["x.mkv"])
        hit = {"id": "11111111-2222-3333-4444-555555555555", "title": "Something", "artist": "Somebody", "track_count": 0, "score": 100, "year": "2001",
               "formats": ["DVD-Video"], "status": "Official"}
        data = {"id": hit["id"], "title": "Something", "artist": "Somebody", "artist_ids": [], "artist_types": {}, "year": "2001", "date": "2001",
                "formats": ["DVD-Video"], "country": "", "status": "Official", "media": [], "release_group": {}, "label": "", "discogs_url": "",
                "url": f"https://musicbrainz.org/release/{hit['id']}"}
        with mock.patch.object(chaps, "chapters", return_value=[]), mock.patch.object(mb, "search_releases", return_value=[hit]), \
                mock.patch.object(mb, "release", return_value=data), mock.patch.object(setlistfm, "search", return_value=[]) as search:
            meta = clip.resolve(release, "clip", original=False, hints={})
        self.assertEqual(search.call_args.kwargs["artist_mbid"], "")
        self.assertEqual(meta.identifiers["mbid"], hit["id"])
        self.assertIn(data["url"], meta.proof_links)

    def test_nothing_found(self):
        release = make_release(self.tmp, "Somebody - Something", ["x.mkv"])
        with mock.patch.object(chaps, "chapters", return_value=[]), mock.patch.object(setlistfm, "search", return_value=[]):
            meta = clip.resolve(release, "clip", original=False, hints={})
        self.assertEqual((meta.creators, meta.title), (["Somebody"], "Something"))
        self.assertTrue(any("tracklista" in w for w in meta.warnings))
        self.assertTrue(any("igazoló link" in w for w in meta.warnings))
        self.assertEqual(meta.tags, ["live"])


if __name__ == "__main__":
    unittest.main()
