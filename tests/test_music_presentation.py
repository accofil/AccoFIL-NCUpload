"""Regressions for the duplicate own-music header reported on 2026-10-18."""
from __future__ import annotations

import html
import re
import unittest
from pathlib import Path

from ncore_up import client, compose, form
from ncore_up.meta import Meta, Track
from test_plan_build import PlanCase


LABELS = ("Előadó", "Album címe", "Megjelenés éve")


def heading_counts(body):
    text = client.parse_description(body)
    return {label: len(re.findall(r"(?im)^\s*" + re.escape(label) + r"\s*:", text)) for label in LABELS}


def rendered_music(payload):
    """Replay the observed nCore header around the submitted free text; no live POST."""
    head = "<br/>".join('<span style="text-decoration:underline">' + label + "</span>: " + html.escape(payload[field])
                         for label, field in zip(LABELS, ("eloado", "album", "album_megjelenes")) if payload[field])
    text = html.escape(payload["szoveg"]).replace("\n", "<br/>")
    return '<div class="torrent_leiras"></div><div class="torrent_leiras proba42">' + head + "<br/><br/>" + text + "</div>"


class MusicPresentationTests(unittest.TestCase):
    def data(self, **changes):
        values = dict(category="lossless", name="Artist - Album", original=False,
                      artist="Artist", album="Album", album_year="2026",
                      description="Előadó: Artist\nAlbum címe: Album\nMegjelenés éve: 2026\nKiadó: Label\n\nTracklista:\n01. Song (3:00)\n\nIgazoló link: https://www.discogs.com/release/1\n")
        return form.UploadData(**(values | changes))

    def test_live_html_reproduces_two_headers_including_underlined_fields(self):
        page = (Path(__file__).parent / "fixtures/presentation/music_duplicate.html").read_text(encoding="utf-8")
        self.assertEqual(heading_counts(page), dict.fromkeys(LABELS, 2))
        # The old extraction silently reported one: inline markup split the label from ':'.
        from bs4 import BeautifulSoup
        old_text = BeautifulSoup(page, "html.parser").select(".torrent_leiras")[-1].get_text("\n", strip=True)
        self.assertEqual(old_text.count("Előadó:"), 1)

    def test_all_music_form_codes_render_each_header_once(self):
        for category in sorted(form.MUSIC_CODES):
            with self.subTest(category=category):
                payload = form.build("fixture", self.data(category=category))
                self.assertEqual(heading_counts(rendered_music(payload)), dict.fromkeys(LABELS, 1))
                for label in LABELS:
                    self.assertNotIn(label + ":", payload["szoveg"])
                for text in ("Kiadó: Label", "Tracklista:", "01. Song (3:00)", "https://www.discogs.com/release/1"):
                    self.assertIn(text, payload["szoveg"])

    def test_bbcode_headers_from_saved_descriptions_are_removed(self):
        data = self.data(description="[u]Előadó[/u]: Artist\r\n[b]Album címe:[/b] Album\r\n[i]Megjelenés éve[/i]: 2026\r\n\r\nTracklista:\r\n01. Song\r\n")
        self.assertEqual(form.build("fixture", data)["szoveg"], "Tracklista:\n01. Song\n")

    def test_normalization_is_idempotent_after_an_editor_change(self):
        data = self.data()
        data.description = form.description_text(data)
        once = data.description
        self.assertEqual(form.description_text(data), once)
        data.description += "\nElőadó: Artist\nAlbum címe: Album\nMegjelenés éve: 2026\n"
        self.assertEqual(form.build("fixture", data)["szoveg"], once)

    def test_no_structured_replacement_keeps_information(self):
        data = self.data(artist="", album="", album_year="")
        self.assertEqual(form.description_text(data), data.description)

    def test_original_and_other_categories_keep_description(self):
        for changes in ({"original": True}, {"category": "ebook_hun"}, {"category": "misc"}):
            data = self.data(**changes)
            self.assertEqual(form.description_text(data), data.description)

    def test_audiobook_keeps_author_and_moves_narrator_title_year_to_fields(self):
        meta = Meta(kind="audiobook", title="A könyv", creators=["Szerző Elek"], year="2026", publisher="Kiadó",
                    extra={"narrator": "Felolvasó Ferenc", "duration": "1:00:00"}, tracks=[Track("01", "Első fejezet", "30:00")])
        data = self.data(artist="Felolvasó Ferenc", album="A könyv", description=compose.audiobook(meta))
        payload = form.build("fixture", data)
        self.assertEqual(heading_counts(rendered_music(payload)), dict.fromkeys(LABELS, 1))
        self.assertIn("Szerző: Szerző Elek", payload["szoveg"])
        self.assertNotIn("Cím:", payload["szoveg"])
        self.assertIn("Kiadó: Kiadó", payload["szoveg"])
        self.assertIn("Teljes hossz: 1:00:00", payload["szoveg"])
        self.assertIn("Fejezetek:\n01. Első fejezet (30:00)", payload["szoveg"])

    def test_clip_title_alias_is_not_repeated(self):
        data = self.data(category="clip", description="Előadó: Artist\nKiadvány címe: Album\nMegjelenés éve: 2026\nAlcím: Live\n\nTracklista:\n01. Song\n")
        payload = form.build("fixture", data)
        self.assertNotIn("Kiadvány címe:", payload["szoveg"])
        self.assertIn("Alcím: Live", payload["szoveg"])

    def test_duplicate_facts_skip_empty_first_block_and_join_inline_labels(self):
        body = '<div class="torrent_leiras"></div><div class="torrent_leiras"><b>Formátum</b>: EPUB<br/><u>Nyelv</u>: magyar</div>'
        self.assertEqual(client.parse_duplicate_facts(body), {"format": "EPUB", "language": "magyar"})

    def test_nested_blocks_are_counted_once(self):
        self.assertEqual(client.parse_description('<div class="torrent_leiras"><div class="torrent_leiras">Előadó: Artist</div></div>'), "Előadó: Artist")


class MusicPlanPresentationTests(PlanCase):
    def test_supplied_description_and_plan_preview_match_serialization(self):
        release = self.release("Artist-Album-FLAC-2026-GRP", {"01.flac": b"A"})
        meta = Meta(kind="album", title="Album", creators=["Artist"], year="2026", confidence=1,
                    tags=["jazz"], tracks=[Track("01", "Song", "3:00")], proof_links=["https://www.discogs.com/release/1"])
        override = self.tmp / "supplied.txt"
        override.write_text("[b]Előadó[/b]: Artist\nAlbum címe: Album\nMegjelenés éve: 2026\nKiadó: Label\n\nTracklista:\n01. Song\n\nIgazoló link: https://www.discogs.com/release/1\n", encoding="utf-8")
        plan, _ = self.build(release, "music", meta, original=False, description_file=override)
        payload = form.build("fixture", plan.data)
        self.assertEqual(plan.data.description, payload["szoveg"])
        self.assertTrue(plan.data.description.startswith("Kiadó: Label\n"))
        self.assertEqual(heading_counts(rendered_music(payload)), dict.fromkeys(LABELS, 1))
