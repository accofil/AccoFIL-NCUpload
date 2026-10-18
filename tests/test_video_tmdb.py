from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from ncore_up.sources import tmdb

FIXTURES = Path(__file__).parent / "fixtures" / "video"


def data(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ParseTests(unittest.TestCase):
    def test_find(self):
        self.assertEqual(tmdb.parse_find(data("tmdb_find_tt0133093.json")), {"kind": "movie", "id": 603})
        self.assertEqual(tmdb.parse_find(data("tmdb_find_tt0903747.json")), {"kind": "tv", "id": 1396})
        self.assertEqual(tmdb.parse_find(data("tmdb_find_episode.json")), {"kind": "tv", "id": 1396, "season": 1, "episode": 1})
        self.assertEqual(tmdb.parse_find({"movie_results": [], "tv_results": []}), {})
        self.assertEqual(tmdb.parse_find(None), {})

    def test_search(self):
        results = tmdb.parse_search(data("tmdb_search_movie_kontroll.json"), "movie")
        self.assertEqual({k: results[0][k] for k in ("id", "title", "original_title", "year", "original_language")},
                         {"id": 5204, "title": "Kontroll", "original_title": "Kontroll", "year": "2003", "original_language": "hu"})
        results = tmdb.parse_search(data("tmdb_search_tv_darkmatter.json"), "tv")
        self.assertEqual((results[0]["id"], results[0]["title"], results[0]["year"], results[0]["kind"]), (196322, "Dark Matter", "2024", "tv"))

    def test_movie_details(self):
        info = tmdb.parse_details(data("tmdb_movie_603_hu.json"), data("tmdb_movie_603_en.json"), "movie")
        self.assertEqual((info["id"], info["kind"], info["imdb"], info["wikidata"]), (603, "movie", "tt0133093", "Q83495"))
        self.assertEqual((info["title"], info["title_hu"], info["title_original"], info["year"]), ("The Matrix", "Mátrix", "The Matrix", "1999"))
        self.assertEqual(info["runtime"], "136")  # the base (en-US) runtime, not the hu translation's 144
        self.assertEqual(info["countries"], ["USA"])
        self.assertEqual(info["directors"], ["Lana Wachowski", "Lilly Wachowski"])
        self.assertEqual(info["cast"], ["Keanu Reeves", "Laurence Fishburne", "Carrie-Anne Moss", "Hugo Weaving", "Gloria Foster"])
        self.assertEqual(info["genres_hu"], ["Akció", "Sci-Fi"])
        self.assertEqual(info["genres_en"], ["Action", "Science Fiction"])
        self.assertTrue(info["overview_hu"].startswith("Két valóság létezik."))
        self.assertTrue(info["overview_en"].startswith("Set in the 22nd century"))
        self.assertEqual(info["poster_url"], "https://image.tmdb.org/t/p/w342/zgJgkFJrLPWQaBtVx9OtTeMrJmo.jpg")
        self.assertEqual(info["url"], "https://www.themoviedb.org/movie/603")

    def test_series_details(self):
        info = tmdb.parse_details(data("tmdb_tv_1396_hu.json"), data("tmdb_tv_1396_en.json"), "tv")
        self.assertEqual((info["title"], info["title_hu"], info["year"], info["kind"]), ("Breaking Bad", "Totál szívás", "2008", "tv"))
        self.assertEqual(info["directors"], ["Vince Gilligan"])
        self.assertEqual(info["seasons"][1], 7)
        self.assertEqual(info["imdb"], "tt0903747")
        self.assertEqual(info["countries"], ["USA"])

    def test_hungarian_film_has_its_original_title_as_hungarian_title(self):
        info = tmdb.parse_details(data("tmdb_movie_5204_hu.json"), {}, "movie")
        self.assertEqual((info["title"], info["title_hu"], info["original_language"]), ("Kontroll", "Kontroll", "hu"))
        self.assertEqual(info["countries"], ["Hungary"])
        self.assertTrue(info["overview_en"].startswith("A subway enforcement officer"))  # from the translations list
        self.assertEqual(info["runtime"], "107")

    def test_no_hungarian_translation(self):
        hu = {"id": 1, "title": "Obscure", "original_title": "Obscure", "original_language": "en", "overview": "",
              "translations": {"translations": [{"iso_639_1": "en", "data": {"title": "", "overview": "An English plot."}}]}}
        info = tmdb.parse_details(hu, {"id": 1, "title": "Obscure", "overview": "An English plot."}, "movie")
        self.assertEqual((info["title_hu"], info["overview_hu"], info["overview_en"]), ("", "", "An English plot."))
        self.assertEqual(tmdb.parse_details({}, {}, "movie"), {})

    def test_countries(self):
        self.assertEqual(tmdb.countries([{"iso_3166_1": "GB", "name": "United Kingdom"}, {"iso_3166_1": "DE", "name": "Germany"}]), ["UK", "Germany"])
        self.assertEqual(tmdb.countries([], ["US", "fr", "XY"]), ["USA", "France", "XY"])

    def test_season_merge_drops_placeholders_and_english_text(self):
        hu = tmdb.parse_season(data("tmdb_tv_196322_s2_hu.json"))
        en = tmdb.parse_season(data("tmdb_tv_196322_s2_en.json"))
        merged = tmdb.merge_season(hu, en, "en")
        self.assertEqual(len(merged), 10)
        self.assertEqual((merged[0]["title_hu"], merged[0]["title"]), ("Nyugis élet", "A Quiet Life"))
        self.assertEqual((merged[8]["title_hu"], merged[8]["title"], merged[8]["overview_hu"]), ("", "How to Build the Box", ""))
        same = tmdb.merge_season([{"season": 1, "episode": 1, "title": "Pilot", "overview": "The pilot of the show, in English words here."}],
                                 [{"season": 1, "episode": 1, "title": "Pilot", "overview": "The pilot of the show, in English words here."}])
        self.assertEqual((same[0]["title_hu"], same[0]["title"], same[0]["overview_hu"]), ("", "Pilot", ""))

    def test_placeholders(self):
        for name in ("9. epizód", "Episode 9", "Epizód 3", "12. rész", "2"):
            self.assertTrue(tmdb.is_placeholder(name), name)
        self.assertFalse(tmdb.is_placeholder("A lúzer"))
        self.assertTrue(tmdb.is_placeholder("4", 4))
        self.assertFalse(tmdb.is_placeholder("1984", 3))  # a real title that is a number

    def test_odd_answers(self):
        self.assertEqual(tmdb.parse_find({"movie_results": [{"title": "no id"}], "tv_results": [{"id": 5}]}), {"kind": "tv", "id": 5})
        self.assertEqual(tmdb.parse_find(["not", "a", "dict"]), {})
        self.assertEqual(tmdb.parse_search({"results": ["x", {"id": None}]}, "movie"), [])
        self.assertEqual(tmdb.parse_season({"episodes": [None, {"episode_number": None}, {"episode_number": 2, "name": "B"}]})[0]["episode"], 2)
        hu = {"id": 7, "title": "T", "credits": {"cast": [{"name": "B", "order": None}, {"name": "A", "order": 0}, "junk"]}}
        self.assertEqual(tmdb.parse_details(hu, {}, "movie")["cast"], ["A", "B"])
        merged = tmdb.merge_season([{"season": 1, "episode": 2, "title": "2", "overview": ""}], [{"season": 1, "episode": 2, "title": "2", "overview": ""}])
        self.assertEqual((merged[0]["title_hu"], merged[0]["title"]), ("", ""))


class FetchTests(unittest.TestCase):
    def test_without_key_nothing_is_fetched(self):
        with mock.patch.object(tmdb.keys, "get", return_value=""), mock.patch.object(tmdb.net, "get_json") as get_json:
            self.assertEqual(tmdb.find("tt0133093"), {})
            self.assertEqual(tmdb.search("The Matrix", "1999"), [])
            self.assertEqual(tmdb.details("movie", 603), {})
            self.assertEqual(tmdb.season_episodes(1396, 1), [])
        get_json.assert_not_called()

    def test_v3_key_and_v4_token(self):
        with mock.patch.object(tmdb.keys, "get", return_value="0123456789abcdef0123456789abcdef"), \
                mock.patch.object(tmdb.net, "get_json", return_value=data("tmdb_find_tt0133093.json")) as get_json:
            self.assertEqual(tmdb.find("TT0133093"), {"kind": "movie", "id": 603})
        args, kwargs = get_json.call_args
        self.assertEqual(args[0], "https://api.themoviedb.org/3/find/tt0133093")
        self.assertEqual(kwargs["params"]["api_key"], "0123456789abcdef0123456789abcdef")
        self.assertIsNone(kwargs["headers"])
        with mock.patch.object(tmdb.keys, "get", return_value="eyJhbGciOiJIUzI1NiJ9.test"), \
                mock.patch.object(tmdb.net, "get_json", return_value={"results": []}) as get_json:
            self.assertEqual(tmdb.search("Dark Matter", "2024", "tv"), [])
        kwargs = get_json.call_args.kwargs
        self.assertNotIn("api_key", kwargs["params"])
        self.assertEqual(kwargs["params"]["first_air_date_year"], "2024")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.test"})

    def test_bad_input_and_failures(self):
        with mock.patch.object(tmdb.keys, "get", return_value="k"), mock.patch.object(tmdb.net, "get_json", return_value=None):
            self.assertEqual(tmdb.find("tt0133093"), {})
            self.assertEqual(tmdb.details("movie", 603), {})
            self.assertEqual(tmdb.season(1396, 1), [])
            self.assertEqual(tmdb.find("nonsense"), {})
            self.assertEqual(tmdb.details("person", 1), {})


if __name__ == "__main__":
    unittest.main()
