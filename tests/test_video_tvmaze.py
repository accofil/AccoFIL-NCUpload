from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from ncore_up.sources import tvmaze

FIXTURES = Path(__file__).parent / "fixtures" / "video"


def data(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TvmazeTests(unittest.TestCase):
    def test_show(self):
        show = tvmaze.parse_show(data("tvmaze_lookup_tt0903747.json"))
        self.assertEqual((show["id"], show["url"], show["name"], show["year"]), (169, "https://www.tvmaze.com/shows/169/breaking-bad", "Breaking Bad", "2008"))
        self.assertEqual((show["imdb"], show["thetvdb"], show["runtime"]), ("tt0903747", "81189", "60"))
        self.assertTrue(show["summary"].startswith("Breaking Bad follows protagonist Walter White"))
        self.assertNotIn("<", show["summary"])
        self.assertEqual(tvmaze.parse_show({}), {})

    def test_episodes(self):
        episodes = tvmaze.parse_episodes(data("tvmaze_episodes_169.json"), 1)
        self.assertEqual([(e["season"], e["episode"], e["title"]) for e in episodes[:2]], [(1, 1, "Pilot"), (1, 2, "Cat's in the Bag...")])
        self.assertEqual(len(episodes), 7)
        self.assertEqual(len(tvmaze.parse_episodes(data("tvmaze_episodes_169.json"))), 20)
        self.assertEqual(tvmaze.parse_episodes({"bad": 1}), [])

    def test_search(self):
        hits = tvmaze.parse_search([{"score": 0.9, "show": data("tvmaze_lookup_tt0903747.json")}, {"score": 0.1}])
        self.assertEqual([h["id"] for h in hits], [169])

    def test_fetching(self):
        with mock.patch.object(tvmaze.net, "get_json", return_value=data("tvmaze_lookup_tt0903747.json")) as get_json:
            self.assertEqual(tvmaze.lookup("TT0903747")["id"], 169)
        get_json.assert_called_once_with("https://api.tvmaze.com/lookup/shows", params={"imdb": "tt0903747"}, quiet=True)
        with mock.patch.object(tvmaze.net, "get_json", return_value=None) as get_json:
            self.assertEqual(tvmaze.lookup("tt0903747"), {})
            self.assertEqual(tvmaze.episodes(169, 1), [])
            self.assertEqual(tvmaze.lookup("169"), {})
            self.assertEqual(tvmaze.episodes("x"), [])
        self.assertEqual(get_json.call_count, 2)


if __name__ == "__main__":
    unittest.main()
