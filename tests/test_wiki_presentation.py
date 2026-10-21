"""Regressions from the authenticated Wiki review and the user's actual episode screenshot."""
from __future__ import annotations

import hashlib
import os
import unittest
from unittest import mock

from ncore_up import client, compose, describe, form, requirements
from ncore_up.meta import Meta, Track, valid_proof
from ncore_up.sources import mafab
from test_plan_build import PlanCase, video_info

SOURCE = "Slo.Pitch.2026.S01E07.The.Real.Housewives.of.Mississauga.1080p.AMZN.WEB-DL.DDP5.1.H.264-Kitsune"


class DescriptionTests(unittest.TestCase):
    def test_rank_minimum_sizes(self):
        for rank, expected in (("Felhasználó",50),("Tag",10),("Elite",10),("Legend",0),("VIP",0),("HelpDesk",0)):
            page = f'<div id="infosav_adatok">User ({rank})</div>'
            self.assertEqual(client.rank_minimum_size(page), expected*1024**2)

    def test_episode_header_is_rendered_once_even_with_supplied_description(self):
        data = form.UploadData(category="hdser", name="Slo Pitch S01E07 1080p", original=False,
            infobar=form.Infobar(imdb_id="tt37195835", title_en="Slo Pitch"), season=1, episodes="7", episode_title="Episode",
            description="Évad: 1\nEpizód: 7\nEpizód címe: Episode\n\nFelirat(ok): angol, angol SDH, francia SDH\nHangsáv: angol 5.1\nExtra: nincs\n\nTartalom.\n\nForrás: Videó és hang: " + SOURCE + ". A szerveren meglévő fájl kép- és hangsávjai változatlanul, újrakódolás nélkül.")
        payload = form.build("fixture", data)
        rendered = f"Évad: {payload['szezon']}\nEpizód: {payload['epizod_szamok']}\nEpizód címe: {payload['epizod_cim']}\n\n" + payload["szoveg"]
        for label in ("Évad:", "Epizód:", "Epizód címe:", "Hangsáv:", "Felirat(ok):"):
            self.assertEqual(rendered.count(label), 1, rendered)
        self.assertIn("Felirat(ok): angol, francia", rendered)
        self.assertNotIn("SDH", rendered)
        self.assertNotIn("Extra:", rendered)
        self.assertIn("Forrás: " + SOURCE, rendered)
        self.assertNotIn("A szerveren", rendered)
        self.assertNotIn("film_extra", payload)
        self.assertEqual(payload["imdb_id"], "tt37195835")

    def test_normalization_is_idempotent_and_preserves_plot_credit(self):
        text = "Felirat(ok): angol, angol SDH\nExtra: nincs\n\nIsmertető.\n(Forrás: Prime Video)\n\nForrás: " + SOURCE
        once = describe.clean_video_description(text, series=True, structured_details=False)
        self.assertEqual(once, describe.clean_video_description(once, series=True, structured_details=False))
        self.assertIn("(Forrás: Prime Video)", once)
        self.assertIn(SOURCE, once)

    def test_real_extra_remains_and_forced_language_is_not_merged(self):
        text = describe.video_description(menu=None, audio=[], subtitles=[
            {"lang":"hun","forced":True}, {"lang":"hun","title":"SDH"}], external_subs=[],
            extras="Kimaradt jelenetek", plot="Tartalom.", source=SOURCE)
        self.assertIn("magyar kiegészítő, magyar", text)
        self.assertIn("Extra: Kimaradt jelenetek", text)

    def test_music_has_no_technical_format_in_description(self):
        meta = Meta(title="Album", creators=["Artist"], year="2026", extra={"format":"FLAC 24/96"},
                    tracks=[Track("01", "Song", "3:00")], proof_links=["https://www.discogs.com/release/1"])
        text = compose.music(meta)
        self.assertNotIn("24/96", text)
        self.assertNotIn("Formátum:", text)
        self.assertIn("Tracklista:", text)

    def test_explicitly_forbidden_proofs_and_duplicate_spotify_credit(self):
        for url in ("https://rateyourmusic.com/release/album/x", "https://www.music-bazaar.com/x",
                    "https://example.com/forum/x", "https://tinyurl.com/x", "https://example.com/cover.jpg"):
            self.assertFalse(valid_proof(url), url)
        self.assertTrue(valid_proof("https://www.amazon.com/dp/ALBUM"))
        link = "https://open.spotify.com/album/example"
        meta = Meta(title="Album", creators=["Artist"], proof_links=[link], extra={"spotify_url":link})
        self.assertEqual(compose.music(meta).count(link), 1)


class PayloadTests(PlanCase):
    def album_meta(self):
        return Meta(kind="album", title="Album", creators=["Artist"], year="2026", confidence=1,
                    tags=["jazz"], tracks=[Track("01", "One", "3:00"), Track("02", "Two", "4:00")],
                    proof_links=["https://www.discogs.com/release/1"])

    def test_nfo_is_read_but_excluded_and_techinfo_covers_whole_album(self):
        release = self.release("Artist-Album-FLAC-2026-GRP", {"01.flac":b"A", "02.flac":b"B", "info.nfo":b"Artist: Artist\nAlbum: Album\n"})
        before = {f.relpath:hashlib.sha256((release.path/f.relpath).read_bytes()).hexdigest() for f in release.files}
        plan, _ = self.build(release, "music", self.album_meta(), original=False)
        self.assertEqual(release.nfo_data["fields"]["artist"], "Artist")
        self.assertNotEqual(plan.content_path, release.path)
        self.assertFalse((plan.content_path/"info.nfo").exists())
        self.assertTrue(release.nfo.exists())
        self.assertEqual(self.calls["mediainfo"], ["01.flac", "02.flac"])
        for filename in ("01.flac", "02.flac"):
            self.assertIn(filename, plan.texts["techinfo_fajl"])
        self.assertEqual(before, {f.relpath:hashlib.sha256((release.path/f.relpath).read_bytes()).hexdigest() for f in release.files})
        self.assertFalse(plan.errors)

    def test_original_music_keeps_its_nfo_and_folder(self):
        release = self.release("Artist-Album-FLAC-2026-GRP", {"01.flac":b"A", "info.nfo":b"Artist: Artist\n"})
        plan, _ = self.build(release, "music", self.album_meta(), original=True)
        # an own hardlinked NCUP copy with the original folder name and every original file (NFO included)
        self.assertEqual(plan.content_path.name, release.path.name)
        self.assertTrue(os.path.samefile(plan.content_path / "info.nfo", release.path / "info.nfo"))
        self.assertIn("nfo_fajl", plan.attachments)
        self.assertFalse(plan.excluded_files)

    def test_staged_sfv_omits_metadata_reference_without_writing_through_hardlink(self):
        sfv = b'01.flac 12345678\ninfo.nfo 87654321\n'
        release = self.release("Artist-Album-FLAC-2026-GRP", {"01.flac":b"A", "info.nfo":b"Artist: Artist", "checks.sfv":sfv})
        plan, _ = self.build(release,"music",self.album_meta(),original=False)
        self.assertEqual((release.path/"checks.sfv").read_bytes(),sfv)
        self.assertEqual((plan.content_path/"checks.sfv").read_bytes(),b'01.flac 12345678\n')

    def test_music_arbitrary_text_is_omitted_but_rip_log_txt_is_valid(self):
        release = self.release("Artist-Album-FLAC-2026-GRP", {"01.flac":b"A", "readme.txt":b"Not a ripping log"})
        plan, _ = self.build(release, "music", self.album_meta(), original=False)
        self.assertFalse((plan.content_path/"readme.txt").exists())
        self.assertIn("readme.txt", plan.excluded_files)
        self.assertFalse(plan.errors)
        (release.path/"readme.txt").write_text("Exact Audio Copy extraction logfile\nAccurateRip verified")
        plan, _ = self.build(release, "music", self.album_meta(), original=False)
        self.assertFalse(plan.errors)

    def test_supplied_full_source_equal_to_release_name_is_valid(self):
        release = self.release(SOURCE, {SOURCE+".mkv":b"A"})
        self.info = video_info(audio=[{"lang":"eng","format":"eac3","channels":6,"default":True}])
        meta = Meta(kind="tv", title="Slo Pitch", year="2026", identifiers={"imdb":"tt37195835"}, confidence=1,
                    description_hu="Egy valódi epizód érdemi ismertetője.")
        plan, _ = self.build(release, "video", meta, category="hdser", original=False, source=SOURCE)
        self.assertEqual(next(r.status for r in requirements.evaluate(plan) if r.id == "source"), "PASS")
        plan.data.description = "Egy epizód ismertetője.\n(Forrás: Prime Video)\n"
        self.assertEqual(next(r.status for r in requirements.evaluate(plan) if r.id == "source"), "UNKNOWN")

    def test_hardware_requirements_are_mandatory_for_own_programs(self):
        release = self.release("Tool v1.0", {"setup.exe":b"A"})
        meta = Meta(kind="software",title="Tool",confidence=1,install_hu="Futtasd a telepítőt és indítsd el a programot.",
                    proof_links=["https://example.com/tool"])
        plan, _ = self.build(release,"software",meta,category="misc",original=False,images=self.images())
        self.assertEqual(next(r.status for r in plan.requirements if r.id == "system_requirements"),"UNKNOWN")
        self.assertFalse(plan.auto_ok)

    def test_series_pack_techinfo_contains_every_episode(self):
        release = self.release("Series.2026.S01.1080p-GRP", {"Series.S01E01.mkv":b"A", "Series.S01E02.mkv":b"B"})
        meta = Meta(kind="tv", title="Series", year="2026", identifiers={"imdb":"tt1234567"}, confidence=1,
                    description_hu="Sorozat ismertetője.")
        plan, _ = self.build(release, "video", meta, category="hdser_hun", original=False, source="Verified source")
        self.assertEqual(self.calls["mediainfo"], ["Series.S01E01.mkv", "Series.S01E02.mkv"])


class MafabTests(unittest.TestCase):
    def test_search_ignores_off_site_links(self):
        page = '<div class="search-item-movie"><h4><a href="/movies/slo-pitch-761110.html">Slo Pitch</a></h4>2026</div>'
        page += '<div class="search-item-movie"><h4><a href="https://other.example/movies/no.html">Other</a></h4></div>'
        self.assertEqual(mafab.parse_search(page), [{"url":"https://www.mafab.hu/movies/slo-pitch-761110.html","year":"2026"}])

    def test_exact_imdb_match_is_required(self):
        search = '<div class="search-item-movie"><h4><a href="/movies/slo-pitch-761110.html">Slo Pitch</a></h4>2026</div>'
        detail = '<title>Slo Pitch (2026)</title><h1>Slo Pitch</h1><a href="https://www.imdb.com/title/tt37195835/">IMDb</a>'
        with mock.patch.object(mafab.net, "get_text", side_effect=[search,detail]):
            found = mafab.find_by_imdb("tt37195835", ["Slo Pitch"], "2026")
        self.assertEqual(found["title"], "Slo Pitch")
        with mock.patch.object(mafab.net, "get_text", side_effect=[search,detail]):
            self.assertEqual(mafab.find_by_imdb("tt9999999", ["Slo Pitch"], "2026"), {})

    def test_wrong_year_and_missing_imdb_are_never_accepted(self):
        search = '<div class="search-item-movie"><h4><a href="/movies/old-1.html">Slo Pitch</a></h4>2020</div>'
        with mock.patch.object(mafab.net, "get_text", return_value=search) as get:
            self.assertEqual(mafab.find_by_imdb("tt37195835", ["Slo Pitch"], "2026"), {})
            self.assertEqual(get.call_count, 1)
        self.assertEqual(mafab.parse_detail('<h1>Slo Pitch</h1>', 'https://www.mafab.hu/movies/x-1.html'), {})
