"""plan.build for every content group, offline: synthetic releases in temp dirs, stub resolvers and media tools.

Every resolver (``ncore_up.resolve.<group>.resolve``) is replaced by a stub returning a crafted Meta; ffprobe,
MediaInfo, BDInfo, ffmpeg (screenshots, sample, cover), the page renderer and network downloads are faked so the
results are the same on the PC and on the seedbox.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import form as forms
from ncore_up import media, net
from ncore_up import plan as plans
from ncore_up import release as rel
from ncore_up.local import ebookmeta
from ncore_up.meta import Meta, Track
from test_books_samples import png

GIB = 1024**3


def audio(lang: str, fmt: str = "ac3", channels: int = 6, title: str = "", default: bool = False) -> dict:
    return {"lang": lang, "format": fmt, "profile": "", "channels": channels, "title": title, "default": default}


def subtitle(lang: str, forced: bool = False, title: str = "") -> dict:
    return {"lang": lang, "format": "subrip", "title": title, "forced": forced}


def video_info(**changes) -> media.VideoInfo:
    info = media.VideoInfo(path=Path("x.mkv"), duration=6000.0, width=1920, height=1080, codec="h264", bitrate=8_000_000,
                           audio=[audio("hun", default=True), audio("eng")], subtitles=[subtitle("hun")])
    return dataclasses.replace(info, **changes)


def write_tree(root: Path, files: dict[str, bytes | int]) -> Path:
    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else b"x" * content)
    return root


class StubClient:
    def __init__(self, ncore_imdb: dict | None = None, fail: bool = False):
        self.ncore_imdb = ncore_imdb or {}
        self.fail = fail
        self.asked: list[str] = []

    def imdb_movie(self, imdb_id: str) -> dict:
        self.asked.append(imdb_id)
        if self.fail:
            raise RuntimeError("nCore down")
        return self.ncore_imdb


class PlanCase(unittest.TestCase):
    """Fakes for every outside tool; ``self.build`` runs plan.build with a stub resolver."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.work = self.tmp / "work"
        self.info: media.VideoInfo | None = video_info()
        self.infos: dict[str, media.VideoInfo] = {}
        self.screens_error = ""
        self.cover_error = ""
        self.pages = 3
        self.calls: dict[str, list] = {"screens": [], "covers": [], "samples": [], "pages": [], "urls": [], "mediainfo": []}
        saved = list(net.FAILURES)
        net.FAILURES.clear()
        self.addCleanup(lambda: net.FAILURES.__setitem__(slice(None), saved))
        for target, name, fake in (
            (rel, "hungarian_audio", lambda root, files: None),
            (media, "probe", self._probe),
            (media, "mediainfo_text", self._mediainfo),
            (media, "bdinfo_text", lambda bdinfo, disc_root, work_dir: "DISC INFO:\nBDINFO REPORT\n"),
            (media, "screenshots", self._screenshots),
            (media, "infobar_cover", self._cover),
            (media, "make_sample", self._make_sample),
            (net, "get_bytes", self._get_bytes),
            (ebookmeta, "proof_pages", self._proof_pages),
        ):
            patcher = mock.patch.object(target, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- fakes ---

    def _probe(self, path):
        info = self.infos.get(Path(path).name, self.info)
        if info is None:
            raise media.MediaError("az ffprobe nincs telepítve")
        return dataclasses.replace(info, path=Path(path))

    def _mediainfo(self, path):
        self.calls["mediainfo"].append(Path(path).name)
        return f"MEDIAINFO {Path(path).name}\n"

    def _screenshots(self, video, out_dir, count=3, image_format="png"):
        self.calls["screens"].append(Path(video).name)
        if self.screens_error:
            raise media.MediaError(self.screens_error)
        out_dir.mkdir(parents=True, exist_ok=True)
        shots = []
        for index in range(1, count + 1):
            shot = out_dir / f"screen{index}.{image_format}"
            shot.write_bytes(png(self.info.width, self.info.height) if self.info else png(1920, 1080))
            shots.append(shot)
        return shots

    def _cover(self, image, out, limit=300):
        self.calls["covers"].append(image)
        if self.cover_error:
            raise media.MediaError(self.cover_error)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(image)
        return out

    def _make_sample(self, video, target, min_bytes=0, min_seconds=60):
        self.calls["samples"].append((Path(video).name, Path(target), min_bytes))
        target.write_bytes(b"SAMPLE")
        return target

    def _get_bytes(self, url, **kwargs):
        self.calls["urls"].append(url)
        return b"URLCOVER"

    def _proof_pages(self, path, out_dir, count=3, warnings=None, layer=""):
        self.calls["pages"].append(Path(path).name)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        for index in range(1, self.pages + 1):
            page = out_dir / f"oldal{index}.png"
            page.write_bytes(png(400, 600, shade=201-index))
            pages.append(page)
        return pages

    # --- helpers ---

    def release(self, name: str, files: dict[str, bytes | int], sizes: dict[str, int] | None = None) -> rel.Release:
        release = rel.inspect(write_tree(self.tmp / name, files))
        for item in release.files:
            item.size = (sizes or {}).get(Path(item.relpath).as_posix(), item.size)
        return release

    def build(self, release: rel.Release, group: str, meta: Meta, client=None, **options):
        calls: list[dict] = []

        def fake(release_, code, *, original, hints):
            calls.append({"code": code, "original": original, "hints": dict(hints)})
            return copy.deepcopy(meta)

        with mock.patch(f"ncore_up.resolve.{group}.resolve", side_effect=fake):
            plan = plans.build(release, plans.Options(**options), client, self.work)
        return plan, calls

    @staticmethod
    def form(plan: plans.Plan) -> dict[str, str]:
        return forms.build("0123456789abcdef", plan.data)

    @staticmethod
    def files(plan: plans.Plan) -> dict[str, object]:
        return {**plan.attachments, **{k: f"{k}.txt" for k in plan.texts}, "torrent_fajl": "(készül)"}

    def missing(self, plan: plans.Plan) -> list[str]:
        return forms.missing(self.form(plan), self.files(plan), plan.has_sample)

    def images(self, count: int = 3) -> list[str]:
        paths = []
        for index in range(1, count + 1):
            path = self.tmp / "sajat" / f"kep{index}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png(400, 600, shade=201-index))
            paths.append(str(path))
        return paths


# --- films and series ------------------------------------------------------------------------------------------

FILM = "The.Long.Night.2020.1080p.BluRay.x264.HUN-GRP"


def film_meta(**changes) -> Meta:
    meta = Meta(kind="movie", title="The Long Night", title_hu="A hosszú éjszaka: Visszatérés", title_original="The Long Night", year="2020",
                creators=["Dir One"], description_hu="Magyar tartalom.", description_source="PORT.hu", cover=b"IMG",
                identifiers={"imdb": "tt1234567"}, confidence=1.0, sources=["TMDB", "PORT.hu"],
                extra={"countries": "USA, UK", "runtime": "101", "cast": "Actor A, Actor B", "other_link": "https://port.hu/adatlap/film/tv/x/movie-1"},
                warnings=["ellenőrizd a címet", "ellenőrizd a címet"])
    for key, value in changes.items():
        setattr(meta, key, value)
    return meta


class VideoFilmTests(PlanCase):
    def film(self, **files):
        return self.release(FILM, files or {f"{FILM}.mkv": 10, "The.Long.Night.hun.srt": 1})

    def test_own_film_name_form_attachments(self):
        client = StubClient({"movie_magyar_cim": "A hosszú éjszaka"})
        plan, calls = self.build(self.film(), "video", film_meta(), client)
        data = plan.data
        self.assertEqual((data.category, data.original), ("hd_hun", False))
        self.assertEqual(data.name, "A hosszú éjszaka - Visszatérés 1080p")
        self.assertEqual(data.description, "Magyar tartalom.\n(Forrás: PORT.hu)\n\nForrás: ismeretlen\n")
        form = self.form(plan)
        expected = {"tipus": "hd_hun", "torrent_nev": data.name, "eredeti": "nem", "imdb_id": "tt1234567",
                    "film_adatbazis": "https://port.hu/adatlap/film/tv/x/movie-1", "megjelent": "2020", "orszag": "USA, UK", "hossz": "101",
                    "film_magyar_cim": "A hosszú éjszaka: Visszatérés", "film_angol_cim": "The Long Night", "film_idegen_cim": "The Long Night",
                    "rendezo": "Dir One", "szereplok": "Actor A, Actor B", "film_felirat": "magyar, magyar (srt)",
                    "film_hangsav": "magyar 5.1, angol 5.1", "film_extra": "nincs", "menu": "nincs", "keresre": "nem",
                    "mindent_tud1": "szabalyzat", "mindent_tud3": "seedeles"}
        self.assertEqual({k: form.get(k) for k in expected}, expected)
        self.assertNotIn("szezon", form)
        self.assertEqual(sorted(plan.attachments), ["infobar_kep", "kep1", "kep2", "kep3"])
        self.assertEqual(plan.attachments["infobar_kep"].read_bytes(), b"IMG")
        self.assertIn(f"MEDIAINFO {FILM}.mkv\n", plan.texts["techinfo_fajl"])
        self.assertIn("MEDIAINFO The.Long.Night.hun.srt\n", plan.texts["techinfo_fajl"])
        self.assertNotIn("nfo_fajl", plan.attachments)
        self.assertEqual(self.missing(plan), [])
        self.assertEqual(plan.errors, [])
        self.assertEqual(plan.warnings, ["ellenőrizd a címet"])
        self.assertEqual(plan.sources, ["TMDB", "PORT.hu"])
        self.assertTrue(plan.notes[0].startswith("kategória: hd_hun (Film (HUN HD)) — "))
        self.assertIn("mód: nem release (saját feltöltés)", plan.notes)
        self.assertIn("azonosítás biztonsága: 1.00", plan.notes)
        self.assertFalse(plan.auto_ok)  # source origin requires evidence even with high identity confidence
        self.assertEqual(plan.content_path, plan.release.path)
        # the resolver is asked again with nCore's own IMDb data
        self.assertEqual(client.asked, ["tt1234567"])
        self.assertEqual(calls[0]["hints"], {"title": "The Long Night", "year": "2020", "series": False})
        self.assertEqual(calls[1]["hints"], {"title": "The Long Night", "year": "2020", "series": False, "imdb": "tt1234567",
                                             "ncore_imdb": {"movie_magyar_cim": "A hosszú éjszaka"}})
        self.assertEqual((calls[0]["code"], calls[0]["original"]), ("hd_hun", False))

    def test_ncore_cross_check_is_optional(self):
        _plan, calls = self.build(self.film(), "video", film_meta(), StubClient(fail=True))
        self.assertEqual(len(calls), 1)
        _plan, calls = self.build(self.film(), "video", film_meta(), None)
        self.assertEqual(len(calls), 1)
        _plan, calls = self.build(self.film(), "video", film_meta(identifiers={}, extra={"other_link": "https://port.hu/x"}), StubClient({"a": "b"}))
        self.assertEqual(len(calls), 1)

    def test_title_other_when_the_name_has_the_english_title(self):
        plan, _ = self.build(self.film(), "video", film_meta(title_hu=""), None)
        self.assertEqual(plan.data.name, "The Long Night 1080p")
        self.assertEqual(plan.data.infobar.title_other, "")
        plan, _ = self.build(self.film(), "video", film_meta(), None, name="The Long Night 1080p")
        self.assertEqual(plan.data.infobar.title_other, "A hosszú éjszaka: Visszatérés")

    def test_hd_dvd_details_render_once_from_form_fields(self):
        for code in sorted(forms.DISC_FILM_CODES):
            with self.subTest(code=code):
                plan, _ = self.build(self.film(), "video", film_meta(), None, category=code, original=False)
                form = self.form(plan)
                rendered = (f"Menü: {form.get('menu', '')}\nFelirat(ok): {form['film_felirat']}\n"
                            f"Hangsáv: {form['film_hangsav']}\nExtra: {form['film_extra']}\n\n" + form['szoveg'])
                for label in ("Menü:", "Felirat(ok):", "Hangsáv:", "Extra:"):
                    self.assertEqual(rendered.count(label), 1)
                self.assertIn("Magyar tartalom.", form['szoveg'])
                self.assertIn("Forrás: ismeretlen", form['szoveg'])

    def test_structured_details_do_not_disguise_missing_plot(self):
        plan, _ = self.build(self.film(), "video", film_meta(description_hu="", description_en=""), None)
        check = next(r for r in plan.requirements if r.id == 'description')
        self.assertEqual(check.status, 'FAIL')

    def test_no_imdb_is_an_error_unless_another_link(self):
        plan, _ = self.build(self.film(), "video", film_meta(identifiers={}, extra={}, title="", title_hu="", title_original="", confidence=0.0), None)
        self.assertIn("nem találtam IMDb-azonosítót: add meg --imdb tt... (vagy --other-link)", plan.errors)
        self.assertEqual(plan.data.name, "The Long Night 1080p")  # the parsed release title, never an empty name
        self.assertIn("infobar: IMDb azonosító vagy más filmadatbázis link kell", self.missing(plan))
        self.assertFalse(plan.auto_ok)
        plan, _ = self.build(self.film(), "video", film_meta(identifiers={}, extra={}), None, other_link="https://www.mafab.hu/movies/x")
        self.assertFalse(any("IMDb" in e for e in plan.errors))
        self.assertEqual(self.form(plan)["film_adatbazis"], "https://www.mafab.hu/movies/x")

    def test_hints_from_options(self):
        _plan, calls = self.build(self.film(), "video", film_meta(), None, imdb="tt0000001",
                                  hints={"title": "Override Title", "year": "1999", "empty": "", "none": None})
        self.assertEqual(calls[0]["hints"], {"title": "Override Title", "year": "1999", "series": False, "imdb": "tt0000001"})

    def test_own_upload_rules(self):
        release = self.release(FILM, {f"{FILM}.mkv": 10, "film.ts": 5, "notes.url": 1, "readme.nfo": 1})
        self.info = video_info(audio=[audio("eng"), audio("hun", default=True)])
        plan, _ = self.build(release, "video", film_meta(), None, original=False)  # NFO + scene name would mean an original
        self.assertTrue(any("film.ts" in e for e in plan.errors))
        self.assertFalse((plan.content_path / "notes.url").exists())
        self.assertIn("a magyar hangsávnak kell az első helyen állnia", plan.errors)
        self.assertFalse(any("readme.nfo" in e for e in plan.errors))
        self.assertFalse(plan.auto_ok)

    def test_sd_rules_and_message_dedup(self):
        name = "Old.Film.2001.DVDRip.x264-GRP"
        release = self.release(name, {"old.film.mp4": 10})
        self.info = video_info(width=720, height=400, audio=[audio("eng", "aac", 6), audio("hun", "aac", 6)], subtitles=[])
        plan, _ = self.build(release, "video", film_meta(title_hu="Régi film", title="Old Film"), None)
        self.assertEqual(plan.data.category, "xvid")
        self.assertEqual(plan.data.name, "Régi film")
        self.assertIn("SD kategóriában .mp4 csak scene release-nél lehet", plan.errors)
        self.assertEqual(plan.errors.count("SD kategóriában legfeljebb 2.0 hang lehet"), 1)
        self.assertEqual(len(plan.errors), len(set(plan.errors)))
        self.assertNotIn("film_hangsav", self.form(plan))  # SD films have no audio/subtitle fields

    def test_bluray_disc(self):
        release = self.release("A Film 2019 BD", {"BDMV/STREAM/00000.m2ts": 10, "BDMV/index.bdmv": 1})
        plan, _ = self.build(release, "video", film_meta(), None, category="hd_hun")
        self.assertEqual(plan.data.name, "A hosszú éjszaka - Visszatérés BD25")
        self.assertNotIn("menu", self.form(plan))
        self.assertFalse(plan.auto_ok)  # disc menu/extras require explicit evidence
        self.assertEqual(self.form(plan)["film_extra"], "ismeretlen")
        self.assertNotIn("Extra:", plan.data.description)
        self.assertTrue(plan.texts["techinfo_fajl"].startswith("DISC INFO:"))
        self.assertEqual(self.calls["screens"], ["00000.m2ts"])
        self.assertEqual(plan.errors, [])
        plan, _ = self.build(release, "video", film_meta(), None, category="hd_hun", menu=False)
        self.assertIn("BluRay lemez csak menüvel tölthető fel", plan.errors)
        self.assertEqual(self.form(plan)["menu"], "nincs")

    def test_dvd_folder_is_refused(self):
        release = self.release("A Film 2001 DVD", {"VIDEO_TS/VTS_01_1.VOB": 10, "VIDEO_TS/VIDEO_TS.IFO": 1})
        plan, _ = self.build(release, "video", film_meta(), None, category="dvd_hun")
        self.assertEqual(plan.data.name, "A hosszú éjszaka - Visszatérés DVD5")
        self.assertEqual(len([e for e in plan.errors if e.startswith("DVD csak lemezképként")]), 1)

    def test_screenshot_failure_for_own_upload_is_an_error(self):
        self.screens_error = "ffmpeg: hiba"
        plan, _ = self.build(self.film(), "video", film_meta(), None)
        self.assertIn("képek: ffmpeg: hiba", plan.errors)
        self.assertIn("3 kép kell", self.missing(plan))

    def test_unreadable_video_still_plans(self):
        self.info = None  # no ffprobe
        plan, _ = self.build(self.film(), "video", film_meta(), None)
        self.assertEqual(plan.data.audio, "")
        self.assertNotIn("Hangsáv:", plan.data.description)
        self.assertEqual(self.form(plan)["film_hangsav"], "")
        self.assertEqual(plan.data.name, "A hosszú éjszaka - Visszatérés 1080p")

    def test_cover_from_url_and_cover_failure(self):
        plan, _ = self.build(self.film(), "video", film_meta(cover=b"", cover_url="https://image.tmdb.org/p.jpg"), None)
        self.assertEqual(self.calls["urls"], ["https://image.tmdb.org/p.jpg"])
        self.assertEqual(plan.attachments["infobar_kep"].read_bytes(), b"URLCOVER")
        self.cover_error = "rossz kép"
        plan, _ = self.build(self.film(), "video", film_meta(), None)
        self.assertNotIn("infobar_kep", plan.attachments)
        self.assertIn("borító: rossz kép", plan.warnings)
        self.assertEqual(plan.errors, [])


class VideoSeriesTests(PlanCase):
    PACK = "Dark.Matter.S02.1080p.WEB-DL.DDP5.1.H.264-GRP"

    def meta(self, **changes) -> Meta:
        meta = Meta(kind="tv", title="Dark Matter", title_hu="Sötét anyag", year="2024", identifiers={"imdb": "tt2222222"}, confidence=0.9,
                    description_hu="Sorozat tartalma.", extra={"other_link": "https://www.tvmaze.com/shows/1/dark-matter", "countries": "USA"})
        for key, value in changes.items():
            setattr(meta, key, value)
        return meta

    def test_season_pack(self):
        files = {f"Dark.Matter.S02E0{n}.1080p.WEB-DL.DDP5.1.H.264-GRP.mkv": 10 for n in (1, 2, 3)}
        plan, calls = self.build(self.release(self.PACK, files), "video", self.meta(), None)
        data = plan.data
        self.assertEqual((data.category, data.name, data.season, data.episodes), ("hdser", "Sötét anyag S02 1080p", 2, "1-3"))
        self.assertEqual(calls[0]["hints"], {"title": "Dark Matter", "season": 2, "episodes": "1-3", "series": True})
        self.assertTrue(data.description.startswith("Felirat(ok): magyar\nHangsáv: magyar 5.1, angol 5.1\n\n"
                                                    "Sorozat tartalma.\n\nForrás: ismeretlen"))
        self.assertNotIn("Menü", data.description)
        form = self.form(plan)
        self.assertEqual((form["szezon"], form["epizod_szamok"], form["epizod_cim"]), ("2", "1-3", ""))
        self.assertNotIn("film_hangsav", form)
        self.assertNotIn("menu", form)
        self.assertEqual(set(self.calls["mediainfo"]), set(files))
        for filename in files:
            self.assertIn(filename, plan.texts["techinfo_fajl"])
        self.assertEqual(self.missing(plan), [])

    def test_single_episode_file(self):
        path = write_tree(self.tmp, {"Dark.Matter.S02E08.1080p.WEB-DL.H.264-GRP.mkv": 10}) / "Dark.Matter.S02E08.1080p.WEB-DL.H.264-GRP.mkv"
        release = rel.inspect(path)
        plan, calls = self.build(release, "video", self.meta(extra={"episode_title": "A kezdet", "other_link": "https://www.tvmaze.com/x"}), None)
        data = plan.data
        self.assertEqual((data.name, data.episodes, data.episode_title), ("Sötét anyag S02E08 1080p", "8", "A kezdet"))
        self.assertEqual(calls[0]["hints"], {"title": "Dark Matter", "season": 2, "episode": 8, "episodes": "8", "series": True})
        self.assertNotIn("Évad:", data.description)
        self.assertNotIn("Epizód:", data.description)
        self.assertNotIn("Epizód címe:", data.description)
        self.assertEqual(self.form(plan)["epizod_cim"], "A kezdet")
        self.assertEqual(plan.attachments["kep1"].name, "screen1.png")


class VideoOriginalTests(PlanCase):
    NAME = "Some.Film.2021.1080p.BluRay.x264-GRP"

    def original(self, sample: bool = True, extra: dict | None = None, name: str = NAME) -> rel.Release:
        files = {f"{name.lower()}.mkv": 10, f"{name.lower()}.nfo": b"IMDb: https://www.imdb.com/title/tt7654321/\n"}
        if sample:
            files["Sample/some.film.sample.mkv"] = 3
        files.update(extra or {})
        return self.release(name, files)

    def meta(self, **changes) -> Meta:
        meta = Meta(kind="movie", title="Some Film", year="2021", description_en="English plot.", identifiers={"imdb": "tt7654321"}, confidence=1.0)
        for key, value in changes.items():
            setattr(meta, key, value)
        return meta

    def test_original_release(self):
        release = self.original()
        plan, calls = self.build(release, "video", self.meta(), None)
        data = plan.data
        self.assertEqual((data.category, data.original, data.name), ("hd", True, self.NAME))
        self.assertEqual(plan.attachments["nfo_fajl"], release.nfo)
        self.assertEqual(calls[0]["hints"], {"imdb": "tt7654321", "title": "Some Film", "year": "2021", "series": False})
        self.assertTrue(calls[0]["original"])
        self.assertEqual(data.description, "")  # no Hungarian plot: the page itself says "Eredeti release!"
        self.assertNotIn("techinfo_fajl", plan.texts)
        self.assertEqual(self.calls["samples"], [])
        self.assertTrue(plan.has_sample)
        form = self.form(plan)
        self.assertEqual((form["eredeti"], form["imdb_id"], form["film_angol_cim"]), ("igen", "tt7654321", "Some Film"))
        for own_only in ("film_hangsav", "film_felirat", "menu", "szezon"):
            self.assertNotIn(own_only, form)
        self.assertEqual(plan.errors, [])
        self.assertEqual(self.missing(plan), [])
        # with a sample in the release no picture is required
        self.assertEqual(forms.missing(form, {"nfo_fajl": 1, "torrent_fajl": 1}, plan.has_sample), [])
        self.assertIn("mód: EREDETI release (NFO, változatlan név és fájlok)", plan.notes)

    def test_original_without_sample_needs_three_images(self):
        plan, _ = self.build(self.original(sample=False), "video", self.meta(), None, screenshots=False)
        self.assertFalse(plan.has_sample)
        self.assertEqual(self.calls["screens"], [])
        self.assertIn("3 kép kell (az eredeti release-ben nincs sample)", self.missing(plan))
        self.screens_error = "ffmpeg: hiba"
        plan, _ = self.build(self.original(sample=False), "video", self.meta(), None)
        self.assertIn("képek: ffmpeg: hiba", plan.errors)
        plan, _ = self.build(self.original(sample=True), "video", self.meta(), None)
        self.assertIn("képek: ffmpeg: hiba", plan.warnings)
        self.assertEqual(plan.errors, [])

    def test_hungarian_original_plot_and_rules(self):
        name = "Some.Film.2021.1080p.BluRay.x264.HUN-GRP"
        self.info = video_info(audio=[audio("eng"), audio("hun", default=True)])
        plan, _ = self.build(self.original(name=name), "video", self.meta(description_hu="Magyar tartalom."), None)
        self.assertEqual(plan.data.category, "hd_hun")
        self.assertEqual(plan.data.description, "Magyar tartalom.\n")
        self.assertIn("a magyar hangsávnak kell az első helyen állnia", plan.errors)
        # foreign originals are exempt from the stream rules
        plan, _ = self.build(self.original(), "video", self.meta(), None)
        self.assertEqual(plan.errors, [])

    def test_original_problems(self):
        with self.assertRaisesRegex(ValueError, "archívum"):
            self.build(self.original(extra={"notes.url": 1, "a.rar": 1}), "video", self.meta(), None)
        (self.tmp / self.original().name / "a.rar").unlink()
        plan, _ = self.build(self.original(extra={"notes.url": 1}), "video", self.meta(), None)
        self.assertIn("nem engedélyezett fájl az eredeti release-ben: notes.url", plan.errors)
        self.assertFalse(plan.auto_ok)
        plan, _ = self.build(self.original(), "video", self.meta(), None, name="Más név 1080p")
        self.assertIn("eredeti release-nél a torrent neve a release neve kell legyen", plan.warnings)

    def test_forced_modes(self):
        release = self.release("Some Film 2021 1080p", {"some.film.mkv": 10})
        plan, _ = self.build(release, "video", self.meta(), None, original=True, category="hd")
        self.assertIn("nincs NFO a release gyökerében (eredeti release-hez kötelező)", plan.errors)
        self.assertIn("eredeti release-hez NFO kell", self.missing(plan))
        plan, calls = self.build(self.original(), "video", self.meta(title_hu="Valami film"), None, original=False)
        self.assertFalse(calls[0]["original"])
        self.assertFalse(plan.data.original)
        self.assertNotIn("nfo_fajl", plan.attachments)  # own uploads never carry an NFO
        self.assertEqual(plan.data.name, "Valami film 1080p")
        self.assertIn("techinfo_fajl", plan.texts)


class SampleStagingTests(PlanCase):
    NAME = "Big.Film.2022.1080p.BluRay.x264.HUN-GRP"

    def big(self, extra: dict | None = None) -> rel.Release:
        files = {f"{self.NAME}.mkv": 10, "cover.jpg": 1, **(extra or {})}
        return self.release(self.NAME, files, sizes={f"{self.NAME}.mkv": 3 * GIB})

    def test_no_make_sample_is_an_error_and_touches_nothing(self):
        plan, _ = self.build(self.big(), "video", film_meta(), None, make_sample=False)
        self.assertIn("2 GB fölötti filmhez legalább 1 perces sample kell (UHD: ≥200 MB) — nincs (--no-make-sample miatt nem vágtam)", plan.errors)
        self.assertFalse((self.work / "NCUP").exists())
        self.assertIsNone(plan.content)
        self.assertEqual(self.calls["samples"], [])
        self.assertFalse(plan.auto_ok)

    def test_make_sample_into_hardlinked_copy(self):
        release = self.big()
        plan, _ = self.build(release, "video", film_meta(), None, make_sample=True)
        stage = plan.content
        self.assertEqual(stage.name, self.NAME)
        self.assertEqual(stage.parent.parent, self.work / "NCUP")
        self.assertEqual(plan.content_path, stage)
        for item in release.files:
            self.assertTrue(os.path.samefile(release.path / item.relpath, stage / item.relpath), item.relpath)
        self.assertEqual((stage / "sample.mkv").read_bytes(), b"SAMPLE")
        self.assertEqual(self.calls["samples"], [(f"{self.NAME}.mkv", stage / "sample.mkv", 0)])
        self.assertFalse((release.path / "sample.mkv").exists())  # the original folder stays untouched
        self.assertTrue(plan.has_sample)
        self.assertTrue(any(n.startswith("sample elkészítve (hardlinkes másolatba)") for n in plan.notes))
        self.assertEqual(plan.texts["techinfo_fajl"], f"MEDIAINFO {self.NAME}.mkv\n\n\nMEDIAINFO cover.jpg\n\n\nMEDIAINFO sample.mkv\n")
        self.assertEqual(plan.errors, [])
        # a second run gets a fresh staging copy
        plan2, _ = self.build(rel.inspect(release.path), "video", film_meta(), None)
        self.assertNotEqual(plan2.content, stage)
        self.assertEqual(plan2.data.name, plan.data.name)

    def test_uhd_sample_size(self):
        self.info = video_info(width=3840, height=2160, codec="hevc", hdr=True)
        plan, _ = self.build(self.big(), "video", film_meta(), None)
        self.assertEqual(self.calls["samples"][0][2], 200 * 1024**2)
        self.assertEqual(plan.data.name, "A hosszú éjszaka - Visszatérés 1080p HDR")  # the release name says 1080p

    def test_existing_sample_is_checked(self):
        self.infos["big.sample.mkv"] = video_info(duration=30.0)
        plan, _ = self.build(self.big({"Sample/big.sample.mkv": 3}), "video", film_meta(), None)
        self.assertEqual(self.calls["samples"], [])
        self.assertIsNone(plan.content)
        self.assertIn("a sample rövidebb 1 percnél (30 mp)", plan.errors)
        self.assertIn("MEDIAINFO big.sample.mkv\n", plan.texts["techinfo_fajl"])

    def test_unexpected_hardlink_failure_is_an_error(self):
        with mock.patch("ncore_up.preparation.os.link", side_effect=OSError("unexpected link failure")):
            plan, _ = self.build(self.big(), "video", film_meta(), None)
        self.assertTrue(any(e.startswith("nem készíthető NCUP fájl") for e in plan.errors), plan.errors)

    def test_single_file_release(self):
        path = write_tree(self.tmp, {f"{self.NAME}.mkv": 10}) / f"{self.NAME}.mkv"
        release = rel.inspect(path)
        release.files[0].size = 3 * GIB
        plan, _ = self.build(release, "video", film_meta(), None)
        stage = plan.content_path
        self.assertEqual(stage.name, self.NAME)
        self.assertTrue(os.path.samefile(path, stage / path.name))
        self.assertTrue((stage / "sample.mkv").exists())

    def test_small_films_and_discs_need_no_sample(self):
        plan, _ = self.build(self.release(FILM, {f"{FILM}.mkv": 10}), "video", film_meta(), None, make_sample=False)
        self.assertEqual(plan.errors, [])
        release = self.release("Big Disc", {"BDMV/STREAM/00000.m2ts": 10, "BDMV/index.bdmv": 1}, sizes={"BDMV/STREAM/00000.m2ts": 30 * GIB})
        plan, _ = self.build(release, "video", film_meta(), None, category="hd_hun", make_sample=False)
        self.assertEqual(plan.errors, [])
        self.assertEqual(plan.data.name, "A hosszú éjszaka - Visszatérés BD50")


# --- clip ------------------------------------------------------------------------------------------------------

class ClipTests(PlanCase):
    NAME = "Artist.Live.at.Venue.2019.1080p.BluRay.x264-GRP"

    def meta(self, **changes) -> Meta:
        meta = Meta(kind="concert", title="Live at Venue", creators=["Artist"], year="2019", confidence=0.6, cover=b"IMG",
                    tracks=[Track("1", "Intro"), Track("2", "Song", "4:01")])
        for key, value in changes.items():
            setattr(meta, key, value)
        return meta

    def test_own_clip(self):
        self.info = video_info(codec="mpeg2video", chapters=True, audio=[audio("eng", "ac3", 6, default=True)], subtitles=[])
        plan, calls = self.build(self.release(self.NAME, {"concert.mkv": 10}), "clip", self.meta(), None, category="clip")
        data = plan.data
        self.assertEqual(data.name, "Artist - Live at Venue 1080p")
        self.assertEqual(calls[0]["hints"], {"year": "2019"})
        self.assertEqual(data.description, "Tracklista:\n1. Intro\n2. Song (4:01)\n\n"
                                           "Menü: nincs\nHangsáv: angol 5.1\nFelirat: nincs\nExtra: nincs\nChapter: van\nSample: nincs\n\nForrás: ismeretlen\n")
        form = self.form(plan)
        self.assertEqual({k: form[k] for k in ("tipus", "music_style", "eloado", "album", "album_megjelenes")},
                         {"tipus": "clip", "music_style": "live", "eloado": "Artist", "album": "Live at Venue", "album_megjelenes": "2019"})
        self.assertNotIn("imdb_id", form)
        # clip rules are only shown
        self.assertIn("klip: csak H.264 vagy H.265 videó tölthető fel (ez: mpeg2video)", plan.warnings)
        self.assertEqual(plan.errors, [])
        self.assertEqual(self.missing(plan), [])
        self.assertFalse(plan.auto_ok)  # clip source/proof still missing

    def test_va_and_missing_tracklist(self):
        plan, _ = self.build(self.release(self.NAME, {"concert.mkv": 10}), "clip", self.meta(creators=["A", "B", "C", "D"], tracks=[], tags=["rock"]),
                             None, category="clip")
        self.assertEqual(plan.data.name, "VA - Live at Venue 1080p")
        self.assertEqual(plan.data.styles, ["rock"])
        self.assertIn("klip: nem találtam tracklistát (fejezetek / MusicBrainz / Discogs / setlist.fm) — írd be (--edit)", plan.warnings)

    def test_original_clip(self):
        release = self.release(self.NAME, {"concert.mkv": 10, "grp.nfo": b"nfo"})
        plan, _ = self.build(release, "clip", self.meta(), None, category="clip")
        self.assertTrue(plan.data.original)
        self.assertEqual((plan.data.name, plan.data.description), (self.NAME, ""))
        self.assertNotIn("techinfo_fajl", plan.texts)
        form = self.form(plan)
        self.assertNotIn("eloado", form)
        self.assertEqual(form["music_style"], "live")
        self.assertEqual(self.missing(plan), [])


# --- music, audiobooks -----------------------------------------------------------------------------------------

class MusicTests(PlanCase):
    NAME = "Artist-Album-WEB-2026-GRP"

    def album(self, nfo: bool = False) -> rel.Release:
        files = {"01-artist-one.mp3": 5, "02-artist-two.mp3": 5, "folder.jpg": 1}
        if nfo:
            files.update({"00-artist-album-web-2026.nfo": b"nfo", "00-artist-album-web-2026.sfv": b"sfv"})
        return self.release(self.NAME, files)

    def meta(self, **changes) -> Meta:
        meta = Meta(kind="album", title="Album", creators=["Artist"], year="2026", tags=["rock"], confidence=0.9, cover=b"IMG",
                    tracks=[Track("1", "One", "3:21"), Track("2", "Two", "4:00")], proof_links=["https://musicbrainz.org/release/x"],
                    extra={"format": "MP3 320 kbps", "bitrate_kbps": "320"})
        for key, value in changes.items():
            setattr(meta, key, value)
        return meta

    def test_own_album(self):
        plan, calls = self.build(self.album(), "music", self.meta(), None, tags=["rock", "pop.rock"])
        data = plan.data
        self.assertEqual((data.category, data.name, data.artist, data.album, data.album_year), ("mp3", "Artist - Album", "Artist", "Album", "2026"))
        self.assertEqual(calls[0]["hints"], {"tags": ["rock", "pop.rock"]})
        self.assertTrue(data.description.startswith("Tracklista:\n1. One (3:21)"))
        self.assertEqual(self.calls["mediainfo"], ["01-artist-one.mp3", "02-artist-two.mp3", "folder.jpg"])
        self.assertIn("MEDIAINFO 01-artist-one.mp3", plan.texts["techinfo_fajl"])
        self.assertIn("MEDIAINFO 02-artist-two.mp3", plan.texts["techinfo_fajl"])
        form = self.form(plan)
        self.assertEqual({k: form[k] for k in ("tipus", "music_style", "eloado", "album", "album_megjelenes")},
                         {"tipus": "mp3", "music_style": "rock, pop.rock", "eloado": "Artist", "album": "Album", "album_megjelenes": "2026"})
        self.assertNotIn("imdb_id", form)
        self.assertEqual(sorted(plan.attachments), ["infobar_kep"])  # music: no screenshots
        self.assertEqual(self.calls["screens"], [])
        self.assertEqual(self.missing(plan), [])
        self.assertEqual(plan.warnings, [])
        self.assertTrue(plan.auto_ok)

    def test_various_artists_and_name_from_resolver(self):
        plan, _ = self.build(self.album(), "music", self.meta(creators=["Various Artists"]), None)
        self.assertEqual(plan.data.name, "VA - Album")
        plan, _ = self.build(self.album(), "music", self.meta(creators=["A", "B", "C", "D"]), None)
        self.assertEqual(plan.data.name, "VA - Album")
        plan, _ = self.build(self.album(), "music", self.meta(extra={"torrent_name": "Artist - Album (Deluxe)"}), None)
        self.assertEqual(plan.data.name, "Artist - Album (Deluxe)")

    def test_low_bitrate_is_an_error_without_duplicate_warning(self):
        meta = self.meta(extra={"bitrate_kbps": "96"}, warnings=["96 kbps szám van: az nCore MP3-nál legalább 128 kbps-t kér"])
        plan, _ = self.build(self.album(), "music", meta, None)
        self.assertIn("MP3 kategóriában legalább 128 kbps kell (a leggyengébb szám: 96 kbps)", plan.errors)
        self.assertFalse(any("128 kbps" in w for w in plan.warnings))
        self.assertFalse(plan.auto_ok)

    def test_missing_tags_proof_and_low_confidence(self):
        plan, _ = self.build(self.album(), "music", self.meta(tags=[], proof_links=[], confidence=0.7), None)
        self.assertIn("nincs stílus-címke (add meg: --tags rock,pop)", plan.warnings)
        self.assertIn("nincs igazoló link (MusicBrainz/Discogs): a szabályzat kötelezővé teszi — írd be (--edit)", plan.warnings)
        self.assertIn("azonosítás biztonsága: 0.70 — automatikus módban ez átnézésre kerül", plan.notes)
        self.assertEqual(plan.errors, [])
        self.assertFalse(plan.auto_ok)  # music needs 0.75
        # the resolver's own words are not repeated
        plan, _ = self.build(self.album(), "music", self.meta(proof_links=[], warnings=["nincs igazoló link a MusicBrainzen"]), None)
        self.assertEqual([w for w in plan.warnings if "igazoló link" in w], ["nincs igazoló link a MusicBrainzen"])
        # --tags given: the resolver may ignore it, the form still gets the tags and no stale warning
        plan, _ = self.build(self.album(), "music", self.meta(tags=[]), None, tags=["jazz"])
        self.assertEqual(plan.data.styles, ["jazz"])
        self.assertFalse(any("stílus-címke" in w for w in plan.warnings))

    def test_missing_tracklist_is_a_nonblocking_warning(self):
        plan, _ = self.build(self.album(), "music", self.meta(tracks=[]), None)
        self.assertEqual(self.missing(plan), [])
        self.assertIn("nem sikerült számlistát kinyerni a helyi adatokból vagy a zenei adatbázisokból", plan.warnings)

    def test_original_album(self):
        release = self.album(nfo=True)
        plan, _ = self.build(release, "music", self.meta(), None)
        self.assertTrue(plan.data.original)
        self.assertEqual(plan.data.name, self.NAME)
        self.assertIn("Előadó: Artist", plan.data.description)
        self.assertIn("Album címe: Album", plan.data.description)
        self.assertIn("Tracklista:", plan.data.description)
        self.assertEqual(plan.attachments["nfo_fajl"], release.nfo)
        self.assertNotIn("techinfo_fajl", plan.texts)
        form = self.form(plan)
        self.assertEqual(form["music_style"], "rock")
        self.assertNotIn("eloado", form)
        self.assertEqual(self.missing(plan), [])


class AudiobookTests(PlanCase):
    NAME = "Szerzo Elek - A konyv (hangoskonyv)"

    def meta(self, **changes) -> Meta:
        meta = Meta(kind="audiobook", title="A könyv", creators=["Szerző Elek"], year="2020", tags=["hangoskönyv"], confidence=0.8,
                    tracks=[Track("01", "Első fejezet", "30:00"), Track("02", "Második fejezet", "31:00")],
                    extra={"narrator": "Felolvasó Ferenc", "format": "MP3 64 kbps", "bitrate_kbps": "64"},
                    proof_links=["https://moly.hu/konyvek/a-konyv"])
        for key, value in changes.items():
            setattr(meta, key, value)
        return meta

    def test_audiobook(self):
        release = self.release(self.NAME, {"01.mp3": 5, "02.mp3": 5})
        plan, _ = self.build(release, "music", self.meta(), None, category="mp3_hun")
        data = plan.data
        self.assertEqual((data.name, data.artist, data.album, data.styles), ("Szerző Elek - A könyv", "Felolvasó Ferenc", "A könyv", ["hangoskönyv"]))
        self.assertTrue(data.description.startswith("Szerző: Szerző Elek\n"))
        self.assertNotIn("Cím:", data.description)
        self.assertNotIn("Előadó:", data.description)
        self.assertIn("Fejezetek:\n01. Első fejezet (30:00)", data.description)
        # Wiki 30 does not exempt spoken-word uploads from the category's bitrate minimum.
        self.assertIn("MP3 kategóriában legalább 128 kbps kell (a leggyengébb szám: 64 kbps)", plan.errors)
        self.assertEqual(self.form(plan)["music_style"], "hangoskönyv")
        self.assertEqual(self.missing(plan), [])
        self.assertFalse(plan.auto_ok)

    def test_without_narrator_the_author_fills_the_artist(self):
        release = self.release(self.NAME, {"01.mp3": 5})
        meta = self.meta(extra={"format": "MP3"}, tags=[], warnings=["hangoskönyv: nem találtam a felolvasó nevét — írd be a leírásba (Előadó: …)"])
        plan, _ = self.build(release, "music", meta, None, category="mp3_hun")
        self.assertEqual(plan.data.artist, "Szerző Elek")
        self.assertEqual(plan.data.styles, ["hangoskönyv"])
        self.assertNotIn("zene: előadó és album kell", self.missing(plan))


# --- ebooks ----------------------------------------------------------------------------------------------------

class EbookTests(PlanCase):
    NAME = "Szerzo Elek - A konyv (2020)"

    def book(self, nfo: bool = False) -> tuple[rel.Release, Path]:
        files = {"a_konyv.epub": 10}
        if nfo:
            files["grp.nfo"] = b"nfo"
        release = self.release(self.NAME, files)
        return release, release.path / "a_konyv.epub"

    def meta(self, main: Path, **changes) -> Meta:
        meta = Meta(kind="book", title="A könyv", creators=["Szerző Elek"], year="2020", date="2020-05-01", publisher="Európa", languages=["magyar"],
                    tags=["szépirodalom"], proof_links=["https://moly.hu/konyvek/a-konyv"], cover=b"IMG", confidence=0.85,
                    extra={"format": "EPUB", "torrent_name": "Szerző Elek - A könyv", "main_file": str(main), "text_layer": "text"})
        for key, value in changes.items():
            setattr(meta, key, value)
        return meta

    def test_own_ebook(self):
        release, main = self.book()
        plan, calls = self.build(release, "books", self.meta(main), None, category="ebook_hun", hints={"isbn": "9789630000000"})
        data = plan.data
        self.assertEqual((data.name, data.book_title, data.book_year, data.book_format), ("Szerző Elek - A könyv", "A könyv", "2020", "EPUB"))
        self.assertEqual(calls[0]["hints"], {"isbn": "9789630000000"})
        self.assertTrue(data.description.startswith("Könyv címe: A könyv\nKiadás dátuma: 2020-05-01\nKiadó: Európa\nFormátum: EPUB\n\nNyelv: magyar"))
        form = self.form(plan)
        self.assertEqual({k: form[k] for k in ("tipus", "ebook_style", "konyv_cime", "konyvv_kiadva", "konyv_formatum")},
                         {"tipus": "ebook_hun", "ebook_style": "szépirodalom", "konyv_cime": "A könyv", "konyvv_kiadva": "2020", "konyv_formatum": "EPUB"})
        self.assertEqual(self.calls["pages"], ["a_konyv.epub"])
        self.assertEqual([plan.attachments[f"kep{i}"].name for i in (1, 2, 3)], ["oldal1.png", "oldal2.png", "oldal3.png"])
        self.assertNotIn("techinfo_fajl", plan.texts)
        self.assertEqual(self.missing(plan), [])
        self.assertTrue(plan.auto_ok)

    def test_too_few_pages_and_missing_facts(self):
        release, main = self.book()
        self.pages = 0
        plan, _ = self.build(release, "books", self.meta(main, tags=[], proof_links=[], extra={"main_file": str(main)}), None, category="ebook_hun")
        self.assertEqual(plan.data.name, self.NAME)
        self.assertIn("e-könyv: nincs stílus-címke (--tags), pl. szépirodalom, sci-fi, szakkönyv", plan.warnings)
        self.assertIn("e-könyv: nincs igazoló link — a szabályzat kötelezővé teszi (--edit)", plan.warnings)
        self.assertIn("3 kép kell", self.missing(plan))

    def test_language_against_category_is_a_doubt(self):
        release, main = self.book()
        resolver_says = "a könyv magyar nyelvűnek tűnik, de angol (ENG) kategóriát választottál"
        plan, _ = self.build(release, "books", self.meta(main, warnings=[resolver_says]), None, category="ebook")
        self.assertEqual(plan.doubts, ["e-könyv: a nyelv (magyar) nem illik a kategóriához (ebook)"])
        self.assertEqual([w for w in plan.warnings if "kategóri" in w], [resolver_says])
        self.assertEqual(plan.errors, [])
        self.assertFalse(plan.auto_ok)
        plan, _ = self.build(release, "books", self.meta(main, languages=["angol"]), None, category="ebook_hun")
        self.assertIn("e-könyv: a nyelv (angol) nem illik a kategóriához (ebook_hun)", plan.warnings)
        self.assertFalse(plan.auto_ok)
        for code, languages in (("ebook", ["angol"]), ("ebook_hun", ["magyar", "angol"]), ("ebook", [])):
            plan, _ = self.build(release, "books", self.meta(main, languages=languages), None, category=code)
            self.assertEqual(plan.doubts, [], (code, languages))

    def test_original_ebook(self):
        release, main = self.book(nfo=True)
        plan, _ = self.build(release, "books", self.meta(main), None, category="ebook", original=True)
        self.assertEqual((plan.data.name, plan.data.description), (self.NAME, ""))
        self.assertEqual(self.calls["pages"], [])
        self.assertIn("nfo_fajl", plan.attachments)
        form = self.form(plan)
        self.assertNotIn("konyv_cime", form)
        self.assertEqual(form["ebook_style"], "szépirodalom")
        self.assertEqual(self.missing(plan), [])


# --- games, console, mobile, programs ----------------------------------------------------------------------------

class SoftTests(PlanCase):
    def meta(self, **changes) -> Meta:
        meta = Meta(title="Thing", version="1.2", publisher="Pub", creators=["Dev"], confidence=0.9, cover=b"IMG", description_hu="Leírás.",
                    install_hu="1. Futtasd a telepítőt.\n2. Válaszd ki a mappát, majd indítsd el a programot.", proof_links=["https://example.com/thing"])
        for key, value in changes.items():
            setattr(meta, key, value)
        return meta

    def test_game_rip(self):
        release = self.release("Game Title v1.2 GoG", {"setup_game_title_1.2.exe": 10, "setup_game_title_1.2-1.bin": 10})
        meta = self.meta(kind="game", title="Game Title", requirements="OS: Windows 10",
                         extra={"torrent_name": "Game Title v1.2 GoG", "category_hint": "game_iso"})
        plan, calls = self.build(release, "games", meta, None, category="game_rip", hints={"gog": "1234"})
        self.assertEqual(calls[0], {"code": "game_rip", "original": False, "hints": {"gog": "1234"}})
        self.assertEqual(plan.data.name, "Game Title v1.2 GoG")
        self.assertTrue(plan.data.description.startswith("Game Title v1.2 GoG\n\nFejlesztő: Dev\nKiadó: Pub\nVerzió: 1.2\n\nLeírás.\n\n"
                                                         "Hivatalos oldal / igazoló link: https://example.com/thing\n\nGépigény:\nOS: Windows 10"))
        self.assertIn("a tartalom alapján inkább ebbe a kategóriába való: game_iso (most: game_rip)", plan.warnings)
        self.assertEqual(plan.doubts, ["a tartalom alapján inkább ebbe a kategóriába való: game_iso (most: game_rip)"])
        self.assertFalse(plan.auto_ok)  # a contradicted category is never uploaded unattended
        form = self.form(plan)
        self.assertEqual((form["tipus"], form["eredeti"]), ("game_rip", "nem"))
        for other in ("imdb_id", "music_style", "ebook_style", "eloado"):
            self.assertNotIn(other, form)
        self.assertEqual(sorted(plan.attachments), ["infobar_kep"])
        self.assertEqual(self.missing(plan), ["3 kép kell"])  # games need the user's own pictures
        plan, _ = self.build(release, "games", meta, None, category="game_rip", images=self.images())
        self.assertEqual(self.missing(plan), [])

    def test_category_hint_agreeing_or_unknown_is_no_doubt(self):
        release = self.release("Game Title v1.2 GoG", {"setup.exe": 10})
        for hint in ("game_rip", "nonsense", ""):
            plan, _ = self.build(release, "games", self.meta(kind="game", requirements="Windows 10", extra={"category_hint": hint}), None, category="game_rip", images=self.images())
            self.assertEqual(plan.doubts, [], hint)
            self.assertTrue(plan.auto_ok, hint)
        # the resolver's own words about the category are not repeated, the doubt stays
        meta = self.meta(kind="game", extra={"category_hint": "game_iso"}, warnings=["kategória: a fájlok alapján inkább game_iso (lemezkép)"])
        plan, _ = self.build(release, "games", meta, None, category="game_rip")
        self.assertEqual([w for w in plan.warnings if "game_iso" in w], ["kategória: a fájlok alapján inkább game_iso (lemezkép)"])
        self.assertEqual(len(plan.doubts), 1)

    def test_missing_install_guide_and_proof_once(self):
        release = self.release("Game Title v1.2 GoG", {"setup.exe": 10})
        meta = self.meta(kind="game", install_hu="", proof_links=[], warnings=["nincs telepítési útmutató az NFO-ban"])
        plan, _ = self.build(release, "games", meta, None, category="game_rip")
        self.assertEqual([w for w in plan.warnings if "telepítési útmutató" in w], ["nincs telepítési útmutató az NFO-ban"])
        self.assertIn("nincs igazoló link / hivatalos oldal: a szabályzat kötelezővé teszi (--edit)", plan.warnings)
        self.assertEqual(plan.data.name, "Game Title v1.2 GoG")  # no resolver name: the release name

    def test_console(self):
        release = self.release("Game PS4", {"game.pkg": 10})
        meta = self.meta(kind="console_game", platform="PS4", region="PAL (EUR)", identifiers={"titleid": "CUSA00001"},
                         extra={"torrent_name": "[PS4] Thing v1.05 HUN", "required_system": "PS4 9.00+"})
        plan, _ = self.build(release, "console", meta, None, category="console", hints={"platform": "ps4"})
        self.assertEqual(plan.data.name, "[PS4] Thing v1.05 HUN")
        self.assertTrue(plan.data.description.startswith("[PS4] Thing v1.05 HUN\n\nPlatform: PS4\nKiadó: Pub\nFejlesztő: Dev\nRégió: PAL (EUR)\n"))
        self.assertIn("Szükséges gép: PS4 9.00+", plan.data.description)
        self.assertEqual(self.form(plan)["tipus"], "console")

    def test_mobile(self):
        release = self.release("App 1.2.3 Android", {"app.apk": 10})
        meta = self.meta(kind="app", platform="Android", identifiers={"package": "com.example.app"}, confidence=0.6,
                         extra={"torrent_name": "[Android] App v1.2.3", "min_os": "Android 8.0"})
        plan, calls = self.build(release, "mobile", meta, None, category="mobil", hints={"package": "com.example.app"})
        self.assertEqual(calls[0]["hints"], {"package": "com.example.app"})
        self.assertEqual(plan.data.name, "[Android] App v1.2.3")
        self.assertIn("Csomagnév: com.example.app", plan.data.description)
        self.assertFalse(plan.auto_ok)  # 0.6 < 0.7
        self.assertIn("azonosítás biztonsága: 0.60 — automatikus módban ez átnézésre kerül", plan.notes)

    def test_software_own_and_original(self):
        release = self.release("Tool 1.2 x64", {"tool_setup.exe": 10})
        meta = self.meta(kind="software", title="Tool", extra={"torrent_name": "Tool v1.2 x64", "arch": "x64"}, requirements="Windows 10")
        plan, _ = self.build(release, "software", meta, None, category="misc")
        self.assertEqual(plan.data.name, "Tool v1.2 x64")
        self.assertIn("Architektúra: x64", plan.data.description)
        self.assertIn("Rendszerigény:\nWindows 10", plan.data.description)
        name = "Tool.v1.2.x64-GRP"
        original = self.release(name, {"tool_setup.exe": 10, "grp.nfo": b"nfo", "file_id.diz": b"diz"})
        meta = self.meta(kind="software", title="Tool", extra={"torrent_name": name, "torrent_name_own": "Tool v1.2 x64"})
        plan, calls = self.build(original, "software", meta, None, category="misc")
        self.assertTrue(calls[0]["original"])
        self.assertEqual((plan.data.name, plan.data.description), (name, ""))
        self.assertIn("nfo_fajl", plan.attachments)
        self.assertEqual(self.missing(plan), [])  # programs: no pictures for originals
        self.assertEqual(plan.errors, [])


# --- shared overrides and plan-level behaviour ---------------------------------------------------------------------

class CommonTests(PlanCase):
    def setUp(self):
        super().setUp()
        self.game = self.release("Game Title v1.2 GoG", {"setup.exe": 10})
        self.game_meta = Meta(kind="game", title="Game Title", confidence=0.9, cover=b"META", install_hu="1. Telepítsd.",
                              proof_links=["https://www.gog.com/game/x"], extra={"torrent_name": "Game Title v1.2 GoG"})

    def test_images_override(self):
        images = self.images()
        plan, _ = self.build(self.release(FILM, {f"{FILM}.mkv": 10}), "video", film_meta(), None, images=images)
        self.assertEqual([str(plan.attachments[f"kep{i}"]) for i in (1, 2, 3)], images)
        self.assertEqual(self.calls["screens"], [])
        self.assertEqual(plan.errors, [])
        plan, _ = self.build(self.game, "games", self.game_meta, None, category="game_rip", images=images[:2])
        self.assertIn("--images: pontosan 3 képet adj meg", plan.errors)
        self.assertEqual(sorted(k for k in plan.attachments if k.startswith("kep")), ["kep1", "kep2"])
        plan, _ = self.build(self.game, "games", self.game_meta, None, category="game_rip", images=images[:2] + [str(self.tmp / "nincs.png")])
        self.assertIn(f"--images: nincs ilyen fájl: {self.tmp / 'nincs.png'}", plan.errors)

    def test_cover_override(self):
        cover = self.tmp / "sajat_borito.png"
        cover.write_bytes(b"MYCOVER")
        plan, _ = self.build(self.game, "games", self.game_meta, None, category="game_rip", cover=str(cover))
        self.assertEqual(self.calls["covers"], [b"MYCOVER"])  # the resolver's cover is not used
        self.assertEqual(plan.attachments["infobar_kep"].read_bytes(), b"MYCOVER")
        plan, _ = self.build(self.game, "games", self.game_meta, None, category="game_rip", cover=str(self.tmp / "nincs.png"))
        self.assertTrue(any(e.startswith("borító: ") for e in plan.errors))
        self.assertNotIn("infobar_kep", plan.attachments)

    def test_description_tags_request(self):
        text = self.tmp / "leiras.txt"
        text.write_text("[b]Saját leírás[/b]\n", encoding="utf-8")
        plan, _ = self.build(self.game, "games", self.game_meta, None, category="game_rip", description_file=str(text), request_id="4242")
        self.assertEqual(plan.data.description, "[b]Saját leírás[/b]\n")
        form = self.form(plan)
        self.assertEqual((form["keresre"], form["keres_kodja"]), ("igen", "4242"))
        plan, _ = self.build(self.game, "games", self.game_meta, None, category="game_rip", description_file=str(self.tmp / "nincs.txt"))
        self.assertTrue(any(e.startswith("--description: nem olvasható") for e in plan.errors))
        plan, calls = self.build(self.release("Artist - Album", {"01.flac": 5}), "music",
                                 Meta(kind="album", title="Album", creators=["Artist"], tags=["rock"], tracks=[Track("1", "One")]), None, tags=["jazz", "blues"])
        self.assertEqual(calls[0]["hints"]["tags"], ["jazz", "blues"])
        self.assertEqual(plan.data.styles, ["jazz", "blues"])
        self.assertEqual(self.form(plan)["music_style"], "jazz, blues")

    def test_unreachable_sources_note(self):
        def failing(*args, **kwargs):
            net.FAILURES.extend(["port.hu: HTTP 503", "port.hu: HTTP 503", "api.tvmaze.com: ReadTimeout"])
            return copy.deepcopy(self.game_meta)

        with mock.patch("ncore_up.resolve.games.resolve", side_effect=failing):
            plan = plans.build(self.game, plans.Options(category="game_rip"), None, self.work)
        self.assertIn("elérhetetlen források: api.tvmaze.com: ReadTimeout, port.hu: HTTP 503", plan.notes)

    def test_warning_dedup_keeps_order(self):
        meta = copy.deepcopy(self.game_meta)
        meta.warnings = ["b", "a", "b", "c", "a"]
        plan, _ = self.build(self.game, "games", meta, None, category="game_rip")
        self.assertEqual(plan.warnings, ["b", "a", "c"])

    def test_confidence_thresholds(self):
        for confidence, ok in ((0.69, False), (0.7, True)):
            meta = copy.deepcopy(self.game_meta)
            meta.confidence = confidence
            plan, _ = self.build(self.game, "games", meta, None, category="game_rip")
            self.assertEqual(plan.confidence, confidence)
            self.assertFalse(plan.auto_ok)  # missing own screenshots always blocks, regardless of confidence
        meta = copy.deepcopy(self.game_meta)
        plan, _ = self.build(self.game, "games", meta, None, category="game_rip", images=["a.png"])
        self.assertFalse(plan.auto_ok)  # errors always block the automatic mode

    def test_category_errors(self):
        with self.assertRaisesRegex(ValueError, "ismeretlen kategória: nope"):
            plans.build(self.game, plans.Options(category="nope"), None, self.work)
        with self.assertRaisesRegex(ValueError, "XXX"):
            plans.build(self.game, plans.Options(category="xxx_hd"), None, self.work)
        unknown = self.release("Unknown content", {"data.blob": 10})
        with self.assertRaisesRegex(ValueError, "--category game_iso\\|game_rip"):
            plans.build(unknown, plans.Options(), None, self.work)


if __name__ == "__main__":
    unittest.main()
