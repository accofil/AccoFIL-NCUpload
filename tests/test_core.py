from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import categories as cats
from ncore_up import client as ncore
from ncore_up import form as forms
from ncore_up import release as rel
from ncore_up import torrent

ERROR_PAGE = """<div class="fobox_fej">Hiba!</div><div class="fobox_tartalom"><table><tr><td>A felvitelnél néhány hiba történt:
<ul><li>Nem adtál meg techinfo fájlt!</li><li>Nem adtál meg torrent fájlt!</li><li>El kell fogadnod a feltöltés szabályait!</li></ul>
</td></tr></table></div>"""
IMDB_AJAX = """<input type="hidden" id="movie_angol_cim" value="Repo Men"><input type="hidden" id="movie_megjelenes_eve" value="2010">
<input type="hidden" id="movie_magyar_cim" value="V&eacute;grehajt&oacute;k"><input type="hidden" id="movie_orszag" value="United Kingdom, Canada, United States">
<input type="hidden" id="movie_hossz" value="111"><input type="hidden" id="movie_rendezo" value="Miguel Sapochnik">"""


def make_tree(root: Path, files: dict[str, int]) -> Path:
    for name, size in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return root


class DetectionTests(unittest.TestCase):
    def detect(self, name, exts, bdmv=False, video_ts=False, hun=None):
        return cats.detect(name, exts, bdmv, video_ts, hun).code

    def test_video_categories(self):
        self.assertEqual(self.detect("Repo.Men.2010.UNRATED.2160p.VUDU.WEB-DL.DDP5.1.H265.HuN.EnG-B9R", [".mkv", ".nfo"]), "hd_hun")
        self.assertEqual(self.detect("Tenzing.2026.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.H.265-FLUX", [".mkv", ".nfo"]), "hd")
        self.assertEqual(self.detect("Crystal.Lake.S01E01.2160p.PCOK.WEB-DL.DD5.1.HEVC-PlayWEB", [".mkv"]), "hdser")
        self.assertEqual(self.detect("Highlander.S01D01.COMPLETE.BLURAY-WRTB", [".m2ts", ".bdmv"], bdmv=True), "hdser")
        self.assertEqual(self.detect("Movie.2010.PAL.DVDR-GRP", [".vob", ".ifo"], video_ts=True), "dvd9")
        self.assertEqual(self.detect("Movie.2010.DVDRip.x264-GRP", [".mkv"], hun=True), "xvid_hun")

    def test_music_ebook_and_unknown(self):
        self.assertEqual(self.detect("Artist-Album-WEB-2026-GRP", [".mp3"] * 10 + [".nfo", ".sfv", ".m3u"]), "mp3")
        self.assertEqual(self.detect("Artist-Album-24BIT-WEB-FLAC-2026-GRP", [".flac"] * 10), "lossless")
        self.assertEqual(self.detect("Kiraly_Viktor-Csillag-HU-2026-GRP", [".mp3"] * 10), "mp3_hun")
        self.assertEqual(self.detect("Author.Book.2026.RETAIL.EPUB.eBook-GRP", [".epub", ".nfo"]), "ebook")
        self.assertIsNone(self.detect("Enshrouded-RUNE", [".iso", ".nfo"]))
        self.assertEqual(self.detect("Captive.State.2019.MULTi.COMPLETE.BLURAY-SAVEiT", [".rar", ".r00", ".nfo"]), "hd")
        self.assertEqual(self.detect("Some.Movie.2020.XXX.1080p.WEB.H264-GRP", [".mkv"]), "xxx_hd")


class ReleaseTests(unittest.TestCase):
    def test_original_release_files_and_imdb_from_nfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_tree(Path(tmp) / "Movie.2020.1080p.BluRay.x264-GRP", {
                "movie.2020.1080p.bluray.x264-grp.mkv": 10, "movie.2020.1080p.bluray.x264-grp.nfo": 0,
                "Sample/sample.mkv": 3, "Proof/proof.jpg": 1, "Subs/movie.srt": 1,
            })
            (root / "movie.2020.1080p.bluray.x264-grp.nfo").write_text("IMDb: https://www.imdb.com/title/tt1053424/\n")
            with mock.patch.object(rel, "hungarian_audio", return_value=None):
                release = rel.inspect(root)
            self.assertEqual(release.imdb_id, "tt1053424")
            self.assertEqual(rel.validate_original(release, cats.CATEGORIES["hd"]), [])

    def test_packed_extra_and_missing_nfo_are_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_tree(Path(tmp) / "Movie.2020.1080p.BluRay.x264-GRP", {"a.rar": 1, "a.r00": 1, "notes.url": 1, "movie.mkv": 5})
            with mock.patch.object(rel, "hungarian_audio", return_value=None):
                problems = rel.validate_original(rel.inspect(root), cats.CATEGORIES["hd"])
            text = "\n".join(problems)
            self.assertIn("nincs NFO", text)
            self.assertIn("a.rar", text)
            self.assertIn("notes.url", text)

    def test_bluray_structure_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_tree(Path(tmp) / "Movie.2020.COMPLETE.BLURAY-GRP", {"BDMV/STREAM/00000.m2ts": 5, "BDMV/index.bdmv": 1, "CERTIFICATE/id.bdmv": 1, "FAB!/mcmf.xml": 1, "grp.nfo": 0})
            with mock.patch.object(rel, "hungarian_audio", return_value=None):
                release = rel.inspect(root)
            self.assertTrue(release.has_bdmv)
            self.assertEqual(rel.validate_original(release, cats.CATEGORIES["hd"]), [])

    def test_rip_categories_may_stay_packed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_tree(Path(tmp) / "Game-GRP", {"grp.rar": 1, "grp.r00": 1, "grp.nfo": 0})
            with mock.patch.object(rel, "hungarian_audio", return_value=None):
                self.assertEqual(rel.validate_original(rel.inspect(root), cats.CATEGORIES["game_rip"]), [])


class ClientParsingTests(unittest.TestCase):
    def test_error_box(self):
        self.assertEqual(ncore.parse_errors(ERROR_PAGE), ["Nem adtál meg techinfo fájlt!", "Nem adtál meg torrent fájlt!", "El kell fogadnod a feltöltés szabályait!"])
        self.assertEqual(ncore.parse_errors("<html>ok</html>"), [])

    def test_imdb_ajax(self):
        data = ncore.parse_imdb_movie(IMDB_AJAX)
        self.assertEqual(data["movie_magyar_cim"], "Végrehajtók")
        self.assertEqual(data["movie_hossz"], "111")

    def test_search_without_results_returns_html(self):
        response = mock.Mock(url="https://ncore.pro/torrents.php", text="Nincs találat", headers={"content-type": "text/html"})
        response.raise_for_status = lambda: None
        session = mock.Mock(headers={})
        session.get.return_value = response
        client = ncore.NcoreClient(cookies=mock.Mock(), session=session)
        self.assertEqual(client.search("nothing"), [])

    def test_login_page_raises(self):
        response = mock.Mock(url="https://ncore.pro/login.php", text="<form>", headers={})
        session = mock.Mock(headers={})
        session.get.return_value = response
        with self.assertRaises(ncore.SessionExpired):
            ncore.NcoreClient(cookies=mock.Mock(), session=session).upload_token()


class FormAndTorrentTests(unittest.TestCase):
    def test_original_video_form(self):
        bar = forms.Infobar(imdb_id="tt1053424", year="2010", countries=["United Kingdom", "Canada", "United States"], title_en="Repo Men", title_hu="Végrehajtók")
        form = forms.build("tok", forms.UploadData(category="hd_hun", name="Repo.Men.2010-GRP", original=True, infobar=bar))
        self.assertEqual(form["eredeti"], "igen")
        self.assertEqual(form["orszag"], "UK, Canada, USA")
        self.assertNotIn("film_hangsav", form)
        self.assertEqual(forms.missing(form, {"nfo_fajl": 1, "torrent_fajl": 1}, has_sample=True), [])
        self.assertIn("3 kép kell (az eredeti release-ben nincs sample)", forms.missing(form, {"nfo_fajl": 1}, has_sample=False))
        self.assertEqual(forms.missing(form, {"nfo_fajl": 1, "kep1": 1, "kep2": 1, "kep3": 1}, has_sample=False), [])
        self.assertIn("eredeti release-hez NFO kell", forms.missing(form, {}))
        music = forms.build("t", forms.UploadData(category="mp3", name="A-B-WEB-2026-GRP", original=True, styles=["Hip Hop", "rock"]))
        self.assertNotIn("imdb_id", music)
        self.assertNotIn("eloado", music)
        self.assertEqual(music["music_style"], "hip.hop, rock")

    def test_own_upload_forms(self):
        series = forms.build("t", forms.UploadData(category="hdser_hun", name="Titkok tengere S01E56 1080p", original=False, season=1, episodes="56", infobar=forms.Infobar(imdb_id="tt1", title_hu="Titkok tengere")))
        self.assertEqual((series["eredeti"], series["szezon"], series["epizod_szamok"]), ("nem", "1", "56"))
        self.assertEqual(forms.missing(series, {"techinfo_fajl": 1}), ["3 kép kell"])
        film = forms.build("t", forms.UploadData(category="dvd_hun", name="Nyerő páros", original=False, menu=True, audio="magyar 5.1", infobar=forms.Infobar(imdb_id="tt1", title_hu="Nyerő páros")))
        self.assertEqual((film["menu"], film["film_hangsav"]), ("van", "magyar 5.1"))
        album = forms.build("t", forms.UploadData(category="mp3_hun", name="X - Y", original=False, artist="X", album="Y", description="Előadó: X\n"))
        self.assertIn("zene: a tracklistát a leírásba kötelező beírni", forms.missing(album, {"techinfo_fajl": 1}))
        self.assertNotIn("3 kép kell", forms.missing(album, {"techinfo_fajl": 1}))

    def test_info_hash(self):
        info = b"d6:lengthi5e4:name3:abc12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"e"
        data = b"d8:announce10:http://x/a4:info" + info + b"e"
        self.assertEqual(torrent.info_hash(data), hashlib.sha1(info).hexdigest())


if __name__ == "__main__":
    unittest.main()
