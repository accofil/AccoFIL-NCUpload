from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ncore_up import keys, net
from ncore_up.resolve import video

FIXTURES = Path(__file__).parent / "fixtures" / "video"
PORT_7241 = "https://port.hu/adatlap/film/tv/matrix-the-matrix/movie-7241"


def load(name: str):
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(raw) if name.endswith(".json") else raw


class FakeNet:
    """Stands in for net.get_json/get_text: (url regex, required params, fixture name or value); None otherwise."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((url, params))
        for pattern, wanted, value in self.routes:
            if re.search(pattern, url) and all(params.get(k) == v for k, v in wanted.items()):
                return load(value) if isinstance(value, str) else copy.deepcopy(value)
        return None

    def hosts(self) -> set[str]:
        return {re.sub(r"^https?://([^/]+).*$", r"\1", url) for url, _ in self.calls}


def resolve(routes, name, code, hints, key="testkey"):
    fake = FakeNet(routes)
    with mock.patch.object(net, "get_json", fake), mock.patch.object(net, "get_text", fake), \
            mock.patch.object(keys, "get", side_effect=lambda n: key if n == "TMDB_API" else ""):
        meta = video.resolve(SimpleNamespace(name=name, imdb_id=None), code, original=False, hints=hints)
    return meta, fake


MATRIX = [
    (r"/3/find/tt0133093$", {}, "tmdb_find_tt0133093.json"),
    (r"/3/movie/603$", {"language": "hu-HU"}, "tmdb_movie_603_hu.json"),
    (r"/3/movie/603$", {"language": "en-US"}, "tmdb_movie_603_en.json"),
    (r"EntityData/Q83495\.json$", {}, "wikidata_Q83495.json"),
    (r"^https://port\.hu/adatlap/film/tv/-/movie-7241$", {}, "porthu_movie_7241.html"),
    (r"suggestion/x/tt0133093\.json$", {}, "imdb_suggest_tt0133093.json"),
]
BREAKING = [
    (r"/3/find/tt0903747$", {}, "tmdb_find_tt0903747.json"),
    (r"/3/find/tt0959621$", {}, "tmdb_find_episode.json"),
    (r"/3/tv/1396$", {"language": "hu-HU"}, "tmdb_tv_1396_hu.json"),
    (r"/3/tv/1396$", {"language": "en-US"}, "tmdb_tv_1396_en.json"),
    (r"/3/tv/1396/season/1$", {"language": "hu-HU"}, "tmdb_tv_1396_s1_hu.json"),
    (r"/3/tv/1396/season/1$", {"language": "en-US"}, "tmdb_tv_1396_s1_en.json"),
    (r"EntityData/Q1079\.json$", {}, "wikidata_Q1079.json"),
    (r"^https://port\.hu/adatlap/film/tv/-/movie-107826$", {}, "porthu_movie_107826.html"),
    (r"^https://port\.hu/adatlap/sorozat/tv/total-szivas-breaking-bad/evad-szerint/movie-107826/1$", {}, "porthu_season_107826_1.html"),
    (r"^https://port\.hu/adatlap/sorozat/tv/a-luzer-pilot/total-szivas-breaking-bad/episode-205805$", {}, "porthu_episode_205805.html"),
    (r"tvmaze\.com/lookup/shows$", {"imdb": "tt0903747"}, "tvmaze_lookup_tt0903747.json"),
    (r"tvmaze\.com/shows/169/episodes$", {}, "tvmaze_episodes_169.json"),
    (r"suggestion/x/tt0903747\.json$", {}, "imdb_suggest_tt0903747.json"),
]
DARK = [
    (r"/3/search/tv$", {"query": "Dark Matter", "first_air_date_year": "2024"}, "tmdb_search_tv_darkmatter.json"),
    (r"/3/tv/196322$", {"language": "hu-HU"}, "tmdb_tv_196322_hu.json"),
    (r"/3/tv/196322$", {"language": "en-US"}, "tmdb_tv_196322_en.json"),
    (r"/3/tv/196322/season/2$", {"language": "hu-HU"}, "tmdb_tv_196322_s2_hu.json"),
    (r"/3/tv/196322/season/2$", {"language": "en-US"}, "tmdb_tv_196322_s2_en.json"),
    (r"port\.hu/search/suggest-list$", {"q": "Dark Matter"}, "porthu_suggest_dark.json"),
]


def english_only_matrix():
    """The Matrix with an untranslated (English) hu-HU overview."""

    hu = load("tmdb_movie_603_hu.json")
    english = load("tmdb_movie_603_en.json")["overview"]
    hu["overview"] = english
    for item in hu["translations"]["translations"]:
        if item["iso_639_1"] == "hu":
            item["data"]["overview"] = english
    routes = [r for r in MATRIX if "port\\.hu" not in r[0] and "movie/603" not in r[0]]
    return routes + [(r"/3/movie/603$", {"language": "hu-HU"}, hu), (r"/3/movie/603$", {"language": "en-US"}, "tmdb_movie_603_en.json")]


class FilmTests(unittest.TestCase):
    def test_matrix_by_imdb_id(self):
        ncore = {"movie_angol_cim": "The Matrix", "movie_magyar_cim": "Mátrix", "movie_megjelenes_eve": "1997", "movie_hossz": "160",
                 "movie_orszag": "United States"}
        meta, fake = resolve(MATRIX, "The.Matrix.1999.1080p.BluRay.x264-GRP", "hd_hun", {"imdb": "tt0133093", "ncore_imdb": ncore})
        self.assertEqual((meta.kind, meta.title, meta.title_hu, meta.title_original, meta.year), ("movie", "The Matrix", "Mátrix", "The Matrix", "1999"))
        self.assertEqual(meta.creators, ["Lana Wachowski", "Lilly Wachowski"])
        self.assertEqual(meta.extra["cast"], "Keanu Reeves, Laurence Fishburne, Carrie-Anne Moss, Hugo Weaving, Gloria Foster")
        self.assertEqual((meta.extra["countries"], meta.extra["runtime"]), ("USA", "136"))
        self.assertEqual(meta.genres, ["sci-fi", "akció"])
        self.assertEqual((meta.description_source, meta.machine_translated), ("PORT.hu", False))
        self.assertTrue(meta.description_hu.startswith("Ahogy látjuk:"))
        self.assertTrue(meta.description_en.startswith("Set in the 22nd century"))
        self.assertEqual(meta.cover_url, "https://image.tmdb.org/t/p/w342/zgJgkFJrLPWQaBtVx9OtTeMrJmo.jpg")
        self.assertEqual(meta.identifiers, {"imdb": "tt0133093", "tmdb": "603", "tmdb_kind": "movie", "porthu": "7241", "wikidata": "Q83495"})
        self.assertEqual(meta.proof_links, [PORT_7241, "https://www.imdb.com/title/tt0133093/"])
        self.assertEqual(meta.extra["other_link"], PORT_7241)
        self.assertEqual(meta.confidence, 1.0)
        text = "\n".join(meta.warnings)
        self.assertIn("nCore IMDb-adat eltér (év): nCore „1997” / TMDB „1999”", text)
        self.assertIn("(hossz)", text)
        self.assertNotIn("(angol cím)", text)
        self.assertNotIn("(magyar cím)", text)
        self.assertNotIn("query.wikidata.org", fake.hosts())  # TMDB gave the Wikidata id: no SPARQL query

    def test_english_category_prefers_verified_hungarian_link(self):
        meta, _ = resolve(MATRIX, "The.Matrix.1999.1080p.BluRay.x264-GRP", "hd", {"imdb": "tt0133093"})
        self.assertEqual(meta.extra["other_link"], PORT_7241)

    def test_without_tmdb_key(self):
        sparql = {"results": {"bindings": [{"item": {"value": "http://www.wikidata.org/entity/Q83495"}}]}}
        routes = MATRIX + [(r"query\.wikidata\.org/sparql$", {}, sparql)]
        meta, fake = resolve(routes, "The.Matrix.1999.1080p.BluRay.x264-GRP", "hd", {"imdb": "tt0133093"}, key="")
        self.assertEqual((meta.title, meta.title_hu, meta.year, meta.extra["runtime"]), ("The Matrix", "Mátrix", "1999", "144"))
        self.assertEqual(meta.cover_url, "https://media.port.hu/images/000/225/169.jpg")
        self.assertEqual(meta.confidence, 1.0)
        self.assertTrue(any("TMDB_API" in w for w in meta.warnings))
        self.assertNotIn("api.themoviedb.org", fake.hosts())

    def test_huwiki_when_no_hungarian_plot(self):
        routes = english_only_matrix() + [(r"hu\.wikipedia\.org/api/rest_v1/page/summary/M%C3%A1trix_%28film%29$", {}, "wikipedia_matrix.json")]
        meta, _ = resolve(routes, "The.Matrix.1999.1080p.BluRay.x264-GRP", "hd", {"imdb": "tt0133093"})
        self.assertEqual(meta.description_source, "Wikipédia (CC BY-SA)")
        self.assertTrue(meta.description_hu.startswith("A Mátrix 1999-ben bemutatott"))
        self.assertEqual(meta.proof_links, ["https://www.imdb.com/title/tt0133093/"])
        self.assertNotIn("porthu", meta.identifiers)

    def test_machine_translation_then_english(self):
        with mock.patch.object(video.translate, "to_hungarian", return_value=("Gépi magyar ismertető a filmről.", True)):
            meta, _ = resolve(english_only_matrix(), "The.Matrix.1999.1080p.BluRay.x264-GRP", "hd", {"imdb": "tt0133093"})
        self.assertEqual((meta.description_hu, meta.description_source, meta.machine_translated),
                         ("Gépi magyar ismertető a filmről.", "TMDB", True))
        self.assertTrue(meta.description_en.startswith("Set in the 22nd century"))
        meta, _ = resolve(english_only_matrix(), "The.Matrix.1999.1080p.BluRay.x264-GRP", "hd", {"imdb": "tt0133093"})
        self.assertEqual((meta.description_hu, meta.description_source, meta.machine_translated), ("", "TMDB", False))
        self.assertTrue(any("nincs magyar ismertető" in w for w in meta.warnings))

    def test_broken_source_becomes_a_warning(self):
        with mock.patch.object(video.porthu, "find_by_imdb", side_effect=RuntimeError("boom")):
            meta, _ = resolve(MATRIX, "The.Matrix.1999.1080p.BluRay.x264-GRP", "hd_hun", {"imdb": "tt0133093"})
        self.assertEqual((meta.title, meta.title_hu), ("The Matrix", "Mátrix"))  # TMDB still answered
        self.assertTrue(any(w.startswith("PORT.hu: hiba") for w in meta.warnings))
        self.assertEqual(meta.confidence, 1.0)

    def test_nothing_found(self):
        meta, _ = resolve([], "Xyzzy.Plugh.2019.1080p.WEB-DL-GRP", "hd", {})
        self.assertEqual(meta.confidence, 0.0)
        self.assertTrue(any("nem sikerült azonosítani" in w for w in meta.warnings))
        self.assertEqual(meta.identifiers, {})

    def test_odd_ncore_data_is_ignored(self):
        meta, _ = resolve(MATRIX, "The.Matrix.1999.1080p.BluRay.x264-GRP", "hd", {"imdb": "tt0133093", "ncore_imdb": ["x"], "year": 1999})
        self.assertEqual((meta.title, meta.confidence), ("The Matrix", 1.0))
        self.assertFalse(any("váratlan hiba" in w for w in meta.warnings))

    def test_series_category_for_a_film_is_not_automatic(self):
        meta, _ = resolve(MATRIX, "The.Matrix.1999.1080p.BluRay.x264-GRP", "hdser", {"imdb": "tt0133093"})
        self.assertEqual((meta.kind, meta.confidence), ("movie", 0.5))
        self.assertTrue(any("ellenőrizd a kategóriát" in w for w in meta.warnings))

    def test_release_year_against_the_id_is_not_automatic(self):
        # e.g. a remake's NFO linking the original film
        meta, _ = resolve(MATRIX, "The.Matrix.2021.1080p.BluRay.x264-GRP", "hd", {"imdb": "tt0133093"})
        self.assertEqual((meta.year, meta.confidence), ("1999", 0.5))
        self.assertTrue(any("a release nevében más év áll (2021)" in w for w in meta.warnings))


class SeriesTests(unittest.TestCase):
    def test_season_pack_by_imdb_id(self):
        meta, _ = resolve(BREAKING, "Breaking.Bad.S01.1080p.BluRay.x264-GRP", "hdser", {"imdb": "tt0903747", "season": 1})
        self.assertEqual((meta.kind, meta.title, meta.title_hu, meta.year), ("tv", "Breaking Bad", "Totál szívás", "2008"))
        self.assertEqual(len(meta.episodes), 7)
        self.assertEqual(meta.episodes[0], {"season": 1, "episode": 1, "title_hu": "A lúzer", "title": "Pilot"})
        self.assertEqual(meta.episodes[6]["title_hu"], "Ha egy üzlet beindul...")
        self.assertNotIn("episode_title", meta.extra)
        self.assertEqual(meta.identifiers["tvmaze"], "169")
        self.assertEqual(meta.extra["other_link"], "https://port.hu/adatlap/film/tv/total-szivas-breaking-bad/movie-107826")
        self.assertEqual(meta.creators, ["Vince Gilligan"])
        self.assertEqual((meta.description_source, meta.confidence), ("PORT.hu", 1.0))
        self.assertIn("krimisorozat", meta.genres)
        self.assertEqual(meta.extra["runtime"], "49")  # median of the season's episodes, not the finale's runtime

    def test_episode_imdb_id_and_single_episode(self):
        meta, _ = resolve(BREAKING, "Breaking.Bad.S01E01.720p.BluRay.x264-GRP", "hdser_hun", {"imdb": "tt0959621"})
        self.assertEqual(meta.identifiers["imdb"], "tt0903747")
        self.assertEqual(meta.identifiers["imdb_episode"], "tt0959621")
        self.assertEqual(meta.episodes, [{"season": 1, "episode": 1, "title_hu": "A lúzer", "title": "Pilot"}])
        self.assertEqual(meta.extra["episode_title"], "A lúzer")
        self.assertEqual(meta.extra["runtime"], "59")
        self.assertTrue(meta.extra["episode_plot"].startswith("Walter, a kémiatanár az 50. születésnapján"))
        self.assertEqual(meta.extra["other_link"], "https://port.hu/adatlap/film/tv/total-szivas-breaking-bad/movie-107826")
        self.assertTrue(any("egy epizódé" in w for w in meta.warnings))

    def test_episode_range(self):
        meta, _ = resolve(BREAKING, "Breaking.Bad.S01.1080p.BluRay.x264-GRP", "hdser", {"imdb": "tt0903747", "season": "1", "episodes": "2-3"})
        self.assertEqual([e["episode"] for e in meta.episodes], [2, 3])
        self.assertNotIn("episode_title", meta.extra)

    def test_dark_matter_by_name_only(self):
        meta, fake = resolve(DARK, "Dark.Matter.2024.S02E08.1080p.WEB.h264-ETHEL", "hdser", {})
        self.assertEqual((meta.title, meta.title_hu, meta.year, meta.confidence), ("Dark Matter", "Sötét anyag", "2024", 0.7))
        self.assertEqual(meta.identifiers["imdb"], "tt19231492")
        self.assertEqual(meta.episodes, [{"season": 2, "episode": 8, "title_hu": "A világmindenség alapvető egysége",
                                          "title": "The Basic Oneness of Everything"}])
        self.assertEqual(meta.extra["episode_title"], "A világmindenség alapvető egysége")
        self.assertEqual(meta.description_source, "TMDB")
        self.assertEqual(meta.description_hu, meta.extra["episode_plot"])
        self.assertEqual(meta.description_source, "TMDB")
        self.assertTrue(any("IMDb-azonosító a TMDB keresésből: tt19231492" in w for w in meta.warnings))
        self.assertFalse(any("/adatlap/" in url for url, _ in fake.calls))  # no port.hu candidate survived the year filter
        self.assertFalse(any("suggestion/x/dark" in url for url, _ in fake.calls))  # TMDB had the id: no IMDb name search

    def test_untranslated_episode_falls_back_to_english(self):
        meta, _ = resolve(DARK, "Dark.Matter.2024.S02E09.1080p.WEB.h264-ETHEL", "hdser", {})
        self.assertEqual(meta.episodes[0]["title_hu"], "")
        self.assertEqual(meta.extra["episode_title"], "How to Build the Box")
        self.assertTrue(any("nincs magyar epizódcím" in w for w in meta.warnings))

    def test_episode_id_unknown_to_tmdb_searches_the_show_by_name(self):
        empty = {"movie_results": [], "person_results": [], "tv_results": [], "tv_episode_results": [], "tv_season_results": []}
        routes = DARK + [(r"/3/find/tt41538945$", {}, empty), (r"suggestion/x/tt41538945\.json$", {}, "imdb_suggest_tt41538945.json")]
        meta, _ = resolve(routes, "Dark.Matter.2024.S02E08.1080p.WEB.h264-ETHEL", "hdser", {"imdb": "tt41538945"})
        self.assertEqual((meta.title, meta.identifiers["imdb"], meta.identifiers["imdb_episode"]), ("Dark Matter", "tt19231492", "tt41538945"))
        self.assertEqual(meta.extra["episode_title"], "A világmindenség alapvető egysége")
        self.assertEqual(meta.confidence, 0.7)
        self.assertTrue(any("egy epizódé (tt41538945)" in w for w in meta.warnings))

    def test_episode_list_hint(self):
        meta, _ = resolve(BREAKING, "Breaking.Bad.S01.1080p.BluRay.x264-GRP", "hdser", {"imdb": "tt0903747", "season": "01", "episodes": [2, 3]})
        self.assertEqual([e["episode"] for e in meta.episodes], [2, 3])


class HelperTests(unittest.TestCase):
    def test_episode_set(self):
        self.assertEqual(video._episode_set("1-6", None), {1, 2, 3, 4, 5, 6})
        self.assertEqual(video._episode_set("E01-E03, 7", None), {1, 2, 3, 7})
        self.assertEqual(video._episode_set("", 8), {8})
        self.assertEqual(video._episode_set("", None), set())
        self.assertEqual(video._episode_set([2, "3"], None), {2, 3})
        self.assertEqual(video._episode_set(None, None), set())

    def test_merge_prefers_a_real_english_title(self):
        # TMDB has only a Hungarian name (merge_season puts it in "title" too); TVmaze has the English one
        tmdb_eps = [{"episode": 1, "title_hu": "A lúzer", "title": "A lúzer"}, {"episode": 2, "title_hu": "", "title": "Cat's in the Bag..."}]
        maze_eps = [{"episode": 1, "title": "Pilot"}, {"episode": 2, "title": "Other"}, {"episode": 3, "title": "3"}]
        port_eps = [{"episode": 2, "title_hu": "Minden kezdet nehéz", "url": "u2"}]
        rows = video._merge_episodes(1, port_eps, tmdb_eps, maze_eps)
        self.assertEqual([(r["episode"], r["title_hu"], r["title"]) for r in rows],
                         [(1, "A lúzer", "Pilot"), (2, "Minden kezdet nehéz", "Cat's in the Bag..."), (3, "", "")])

    def test_imdb_suggest_parser(self):
        hits = video.parse_imdb_suggest(load("imdb_suggest_darkmatter.json"))
        self.assertEqual(hits[0], {"id": "tt19231492", "title": "Dark Matter", "year": "2024", "kind": "TV series",
                                   "stars": "Joel Edgerton, Jennifer Connelly"})
        self.assertEqual(video.parse_imdb_suggest([]), [])

    def test_pick(self):
        results = [{"title": "Dark Matter", "original_title": "Dark Matter", "year": "2015", "popularity": 50.0},
                   {"title": "Dark Matter", "original_title": "Dark Matter", "year": "2024", "popularity": 80.0}]
        self.assertEqual(video._pick(results, "Dark Matter", "2024"), (results[1], True))
        self.assertEqual(video._pick(results, "Dark Matters", "2025"), (results[1], False))
        self.assertEqual(video._pick(results[:1], "Dark Matter", "2016", filtered=True), (results[0], True))
        self.assertEqual(video._pick(results, "Something Else", "2024"), ({}, False))


if __name__ == "__main__":
    unittest.main()
