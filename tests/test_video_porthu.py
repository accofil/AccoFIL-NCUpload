from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from ncore_up.sources import porthu

FIXTURES = Path(__file__).parent / "fixtures" / "video"


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def data(name: str):
    return json.loads(text(name))


class ParseTests(unittest.TestCase):
    def test_suggest_keeps_only_films_and_series(self):
        hits = porthu.parse_suggest(data("porthu_suggest_matrix.json"))
        self.assertEqual([(h["id"], h["title"], h["year"]) for h in hits],
                         [("7241", "Mátrix", "1999"), ("263997", "Keanu Reeves a mátrixon túl", "2022")])
        self.assertEqual(hits[0]["url"], "https://port.hu/adatlap/film/tv/matrix-the-matrix/movie-7241")
        hits = porthu.parse_suggest(data("porthu_suggest_kontroll.json"))
        self.assertEqual([(h["id"], h["year"]) for h in hits], [("210548", "2019"), ("82081", "")])
        self.assertEqual(porthu.parse_suggest({"error": 1}), [])

    def test_full_search(self):
        hits = porthu.parse_search(text("porthu_search_kontroll.html"))
        self.assertEqual(hits[0], {"id": "58807", "title": "Kontroll", "year": "2003", "subtitle": "magyar thriller, 2003",
                                   "url": "https://port.hu/adatlap/film/tv/kontroll-kontroll/movie-58807"})
        self.assertEqual(len(hits), 3)

    def test_film_page(self):
        page = porthu.parse_detail(text("porthu_movie_7241.html"))
        self.assertEqual((page["id"], page["kind"], page["title"], page["title_original"]), ("7241", "movie", "Mátrix", "The Matrix"))
        self.assertEqual((page["year"], page["runtime"], page["genre"], page["countries"]), ("1999", "144", "sci-fi", ["amerikai"]))
        self.assertEqual(page["imdb"], "tt0133093")
        self.assertEqual(page["url"], "https://port.hu/adatlap/film/tv/matrix-the-matrix/movie-7241")
        self.assertEqual(page["directors"], ["Lilly Wachowski", "Lana Wachowski"])
        self.assertEqual(page["cast"][0], "Keanu Reeves")
        self.assertEqual(page["image"], "https://media.port.hu/images/000/225/169.jpg")
        self.assertTrue(page["description"].startswith("Ahogy látjuk: Szürke, egyhangú világunk valóságos.\nA valóság:"))
        self.assertNotIn("\r", page["description"])
        self.assertEqual(page["seasons"], {})

    def test_series_page_and_session_token(self):
        page = porthu.parse_detail(text("porthu_movie_107826.html").replace("movie-107826\">", "movie-107826?token=abc123\">", 1))
        self.assertEqual((page["kind"], page["title"], page["title_original"], page["year"], page["runtime"]),
                         ("tv", "Totál szívás", "Breaking Bad", "2008", "45"))
        self.assertEqual(page["url"], "https://port.hu/adatlap/film/tv/total-szivas-breaking-bad/movie-107826")
        self.assertEqual(sorted(page["seasons"]), [1, 2, 3, 4, 5])
        self.assertEqual(page["seasons"][1], "https://port.hu/adatlap/sorozat/tv/total-szivas-breaking-bad/evad-szerint/movie-107826/1")
        self.assertEqual(page["imdb"], "tt0903747")

    def test_series_page_without_season_list(self):
        page = porthu.parse_detail(text("porthu_movie_254858.html"))
        self.assertEqual((page["kind"], page["year"], page["runtime"], page["seasons"]), ("tv", "2024", "", {}))
        self.assertEqual(page["genre"], "sci-fi sorozat")

    def test_not_a_detail_page(self):
        self.assertEqual(porthu.parse_detail("<html><body>Hiba</body></html>"), {})
        self.assertEqual(porthu.parse_detail(""), {})

    def test_season_page(self):
        episodes = porthu.parse_season(text("porthu_season_107826_1.html"), 1)
        self.assertEqual(len(episodes), 7)
        self.assertEqual(episodes[0], {"season": 1, "episode": 1, "title_hu": "A lúzer", "label": "I/1",
                                       "url": "https://port.hu/adatlap/sorozat/tv/a-luzer-pilot/total-szivas-breaking-bad/episode-205805"})
        self.assertEqual(episodes[6]["title_hu"], "Ha egy üzlet beindul...")
        self.assertEqual(porthu.parse_season(text("porthu_season_254858_2.html"), 2), [])

    def test_season_labels_and_placeholders(self):
        page = ('<div itemprop="episode" itemscope><meta itemprop="episodeNumber" content="3"/><a class="url" href="/x/episode-1">'
                '<span class="ep-number">XIV/3</span> 3. rész </a></div>')
        self.assertEqual(porthu.parse_season(page), [{"season": 14, "episode": 3, "title_hu": "", "url": "https://port.hu/x/episode-1", "label": "XIV/3"}])

    def test_bare_episode_number_is_a_placeholder(self):
        # the Chernobyl (movie-210414) season page as served on 2026-10-17: only episode 1 has a title
        page = ('<div itemprop="episode"> <meta itemprop="episodeNumber" content="1"/> <a class="url" href="/adatlap/sorozat/tv/012345-12345/'
                'csernobil-chernobyl/episode-3691609" itemprop="url"> <span class="ep-number"> I/1 </span> 01:23:45 </a> </div>'
                '<div itemprop="episode"> <meta itemprop="episodeNumber" content="2"/> <a class="url" href="/adatlap/sorozat/tv/please-remain-calm/'
                'csernobil-chernobyl/episode-3701933" itemprop="url"> <span class="ep-number"> I/2 </span> 2 </a> </div>')
        self.assertEqual([(e["episode"], e["title_hu"]) for e in porthu.parse_season(page, 1)], [(1, "01:23:45"), (2, "")])

    def test_odd_json_ld(self):
        page = ('<meta property="og:url" content="http://port.hu/adatlap/film/tv/x/movie-5">'
                '<script type="application/ld+json">{"@type": ["Movie"], "name": ["X"], "image": ["https://media.port.hu/a.jpg"]}</script>')
        info = porthu.parse_detail(page)
        self.assertEqual((info["id"], info["title"], info["image"]), ("5", "", "https://media.port.hu/a.jpg"))

    def test_episode_page(self):
        episode = porthu.parse_episode(text("porthu_episode_205805.html"))
        self.assertEqual((episode["title"], episode["title_original"], episode["season"], episode["episode"]), ("A lúzer", "Pilot", 1, 1))
        self.assertEqual((episode["runtime"], episode["year"]), ("58", "2008"))
        self.assertTrue(episode["description"].startswith("Walter, a kémiatanár az 50. születésnapján"))
        self.assertTrue(episode["show_description"].startswith("Walter (Bryan Cranston)"))

    def test_norm(self):
        self.assertEqual(porthu.norm("Mátrix"), "matrix")
        self.assertEqual(porthu.norm("Tom & Jerry: The Movie!"), "tom and jerry the movie")
        self.assertEqual(porthu.norm("Ha egy üzlet beindul..."), "ha egy uzlet beindul")
        self.assertEqual(porthu.norm("Schindler’s List"), porthu.norm("Schindlers List"))
        self.assertEqual(porthu.norm("Ocean's Eleven"), "oceans eleven")


class FetchTests(unittest.TestCase):
    def test_suggest_passes_the_query_as_a_parameter(self):
        with mock.patch.object(porthu.net, "get_json", return_value=data("porthu_suggest_matrix.json")) as get_json:
            hits = porthu.suggest(" Mátrix ")
        get_json.assert_called_once_with(porthu.SUGGEST, params={"q": "Mátrix"})
        self.assertEqual(hits[0]["id"], "7241")
        with mock.patch.object(porthu.net, "get_json") as get_json:
            self.assertEqual(porthu.suggest(""), [])
        get_json.assert_not_called()

    def test_failures_give_empty_results(self):
        with mock.patch.object(porthu.net, "get_json", return_value=None), mock.patch.object(porthu.net, "get_text", return_value=None):
            self.assertEqual(porthu.suggest("x"), [])
            self.assertEqual(porthu.search("x"), [])
            self.assertEqual(porthu.detail("7241"), {})
            self.assertEqual(porthu.season("7241", 1), [])
            self.assertEqual(porthu.episode("https://port.hu/adatlap/sorozat/tv/a/b/episode-1"), {})
        self.assertEqual(porthu.detail("not-an-id"), {})
        self.assertEqual(porthu.episode("https://example.com/x"), {})

    def test_detail_url(self):
        with mock.patch.object(porthu.net, "get_text", return_value=text("porthu_movie_7241.html")) as get_text:
            self.assertEqual(porthu.detail("movie-7241")["id"], "7241")
        get_text.assert_called_once_with("https://port.hu/adatlap/film/tv/-/movie-7241")


def page(port_id, title, year, imdb="", original="", kind="movie", runtime=""):
    return {"id": port_id, "title": title, "title_original": original, "year": year, "imdb": imdb, "kind": kind, "runtime": runtime,
            "url": f"https://port.hu/adatlap/film/tv/x/movie-{port_id}"}


class FindTests(unittest.TestCase):
    def run_find(self, pages, suggest=(), search=(), **kwargs):
        details = []

        def detail(port_id):
            details.append(str(port_id))
            return dict(pages.get(str(port_id), {}))

        with mock.patch.object(porthu, "detail", side_effect=detail), \
                mock.patch.object(porthu, "suggest", side_effect=lambda q: list(suggest)), \
                mock.patch.object(porthu, "search", side_effect=lambda q: list(search)):
            return porthu.find(**kwargs), details

    def test_wikidata_id_is_taken_when_the_imdb_link_agrees(self):
        found, details = self.run_find({"7241": page("7241", "Mátrix", "1999", "tt0133093")}, titles=["Mátrix"], year="1999",
                                       imdb="tt0133093", porthu_id="7241")
        self.assertEqual((found["id"], found["match"]), ("7241", "wikidata"))
        self.assertEqual(details, ["7241"])

    def test_wrong_wikidata_id_falls_back_to_search_with_imdb_check(self):
        pages = {"1": page("1", "Végállomás", "1999", "tt0000001"), "2": page("2", "Mátrix", "1999", "tt0000002"),
                 "3": page("3", "Mátrix", "1999", "tt0133093")}
        suggest = [{"id": "2", "title": "Mátrix", "year": "1999"}, {"id": "3", "title": "Mátrix", "year": "1999"},
                   {"id": "4", "title": "Mátrix", "year": "2021"}]
        found, details = self.run_find(pages, suggest=suggest, titles=["Mátrix"], year="1999", imdb="tt0133093", porthu_id="1")
        self.assertEqual((found["id"], found["match"]), ("3", "imdb"))
        self.assertEqual(details, ["1", "2", "3"])  # the 2021 candidate is never fetched

    def test_title_and_year_only_when_the_page_has_no_imdb_link(self):
        pages = {"5": page("5", "Kontroll", "2003", original="Kontroll"), "6": page("6", "Kontroll", "2003", imdb="tt9999999")}
        suggest = [{"id": "6", "title": "Kontroll", "year": "2003"}, {"id": "5", "title": "Kontroll", "year": "2003"}]
        found, _ = self.run_find(pages, suggest=suggest, titles=["Kontroll"], year="2003", imdb="tt0373981")
        self.assertEqual((found["id"], found["match"]), ("5", "title"))
        found, _ = self.run_find(pages, suggest=suggest, titles=["Kontroll"], year="2010", imdb="tt0373981")
        self.assertEqual(found, {})
        found, _ = self.run_find(pages, suggest=suggest[1:], titles=["Kontroll"], year="2003", kind="tv")
        self.assertEqual(found, {})  # a film page does not match a series

    def test_full_search_when_the_suggest_list_has_no_exact_title(self):
        pages = {"58807": page("58807", "Kontroll", "2003", "tt0373981")}
        found, details = self.run_find(pages, suggest=[{"id": "210548", "title": "Kontroll nélkül", "year": "2019"}],
                                       search=[{"id": "58807", "title": "Kontroll", "year": "2003"}],
                                       titles=["Kontroll"], year="2003", imdb="tt0373981")
        self.assertEqual(found["id"], "58807")
        self.assertEqual(details, ["58807"])

    def test_budget_limits_detail_pages(self):
        suggest = [{"id": str(i), "title": "Valami", "year": "2000"} for i in range(10)]
        found, details = self.run_find({}, suggest=suggest, search=suggest, titles=["Valami", "Something"], year="2000",
                                       imdb="tt0000001", budget=4)
        self.assertEqual(found, {})
        self.assertEqual(len(details), 4)

    def test_find_by_imdb_asks_wikidata_only_when_needed(self):
        with mock.patch.object(porthu.wikidata, "lookup", return_value={"claims": {"P905": ["7241"]}}) as lookup, \
                mock.patch.object(porthu, "find", return_value={}) as find:
            porthu.find_by_imdb("tt0133093", ["Mátrix"], "1999")
            porthu.find_by_imdb("tt0133093", ["Mátrix"], "1999", porthu_id="")
        lookup.assert_called_once_with("imdb", "tt0133093")
        self.assertEqual(find.call_args_list[0].kwargs["porthu_id"], "7241")
        self.assertEqual(find.call_args_list[1].kwargs["porthu_id"], "")


if __name__ == "__main__":
    unittest.main()
