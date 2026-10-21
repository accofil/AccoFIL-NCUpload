"""Wiki 41 enforcement, data-only updates, and the last gate before upload."""
from __future__ import annotations

import html
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ncore_up import banlist
from ncore_up.client import NcoreError
from ncore_up.meta import Meta
from test_cli_run import CliCase, FakeClient
from test_plan_build import PlanCase

FIXTURE = Path(__file__).parent / "fixtures/banlist/wiki41.html"


def snapshot():
    return banlist.parse_page(FIXTURE.read_text(encoding="utf-8"))


class ParserTests(unittest.TestCase):
    def test_current_live_wiki_has_no_unhandled_rules(self):
        data = snapshot()
        self.assertEqual(data["version"], "v1")
        self.assertFalse(data["unknown_rules"], data["unknown_rules"])
        self.assertGreater(len(data["rules"]), 55)
        self.assertEqual({r["section"] for r in data["rules"]}, set(banlist.SECTIONS.values()))

    def test_new_names_and_groups_are_loaded_without_program_changes(self):
        page = FIXTURE.read_text(encoding="utf-8").replace("- RENDSZERINFO64", "- RENDSZERINFO64<br/>- New Product")
        page = page.replace('- összes &quot;-TESTBAN&quot; release', '- összes &quot;-TESTBAN&quot; release<br/>- összes &quot;-NEWTEAM&quot; release')
        data = banlist.parse_page(page)
        self.assertTrue(any(r.get("names") == ["New Product"] for r in data["rules"]))
        self.assertTrue(any(r.get("names") == ["NEWTEAM"] for r in data["rules"]))
        self.assertFalse(data["unknown_rules"])

    def test_new_conditional_prose_is_unknown_and_not_silently_accepted(self):
        page = FIXTURE.read_text(encoding="utf-8").replace("- RENDSZERINFO64", "- RENDSZERINFO64<br/>- Csak engedély esetén tölthető fel: valami")
        self.assertEqual(len(banlist.parse_page(page)["unknown_rules"]), 1)

    def test_missing_or_new_sections_and_login_html_fail_closed(self):
        for page in ('<form>login</form>', FIXTURE.read_text(encoding="utf-8").replace('Tiltott könyvek:', 'Új fejezet:'),
                     FIXTURE.read_text(encoding="utf-8").replace('Verzió:', 'revision:')):
            with self.subTest(page=page[:30]), self.assertRaises(NcoreError):
                banlist.parse_page(page)


class BanRulesTests(PlanCase):
    def plan(self, name, *, group="program", original=False, title="", creators=(), source="", nfo_text="", codec=""):
        release = self.release(name, {"content.bin": b"A", **({"info.nfo": nfo_text.encode()} if nfo_text else {})})
        return SimpleNamespace(release=release, category=SimpleNamespace(group=group),
            data=SimpleNamespace(name="Renamed upload", original=original, artist="", album="", book_title="", infobar=None),
            meta=Meta(title=title, creators=list(creators)), options=SimpleNamespace(source=source),
            video_info=SimpleNamespace(codec=codec))

    def states(self, plan, data=None):
        return [r.status for r in banlist.evaluate(data or snapshot(), plan)]

    def test_renaming_and_own_mode_do_not_hide_banned_software(self):
        for name in ("Lemez.Orzo.6.30", "RENDSZERINFO64.Extreme", "Tesztkresz.2026"):
            self.assertIn("FAIL", self.states(self.plan(name)))
        self.assertIn("FAIL", self.states(self.plan("Unrelated", title="Lemez Őrző")))
        self.assertEqual(self.states(self.plan("Normal.Software.1.0", title="Normal Software")), ["PASS"])

    def test_banned_groups_and_aliases_apply_in_both_modes(self):
        for name in ("Film.2026.1080p-TESTBAN", "Film.2026.1080p-ALIASA", "Film.2026.1080p-ALIASB", "Film.2026.1080p-R.X.Mechanix"):
            for original in (True, False):
                self.assertIn("FAIL", self.states(self.plan(name, group="video", original=original)))
        self.assertEqual(self.states(self.plan("Film.2026.1080p-TESTBANX", group="video")), ["PASS"])
        self.assertIn("FAIL", self.states(self.plan("Renamed.Film", group="video", source="Film.2026.1080p-TESTBAN")))
        plan=self.plan("Description.Source",group="video")
        plan.data.description="Forrás: Film.2026.1080p-TESTBAN\n"
        self.assertIn("FAIL",self.states(plan))

    def test_explicit_nogrp_and_no1_own_exceptions(self):
        self.assertIn("FAIL", self.states(self.plan("Album.2026-NoGrp", original=True)))
        self.assertEqual(self.states(self.plan("Album.2026-NoGrp", original=False)), ["PASS"])
        for original, expected in ((True, "FAIL"), (False, "PASS")):
            self.assertIn(expected, self.states(self.plan("Film.2026-No1", original=original, nfo_text="Visit BitHUmen.org")))

    def test_artist_album_pair_and_author_all_works(self):
        self.assertIn("FAIL", self.states(self.plan("Music", group="music", title="Képzelt dalok", creators=["Kitalált Zenekar"])))
        self.assertEqual(self.states(self.plan("Music2", group="music", title="Different album", creators=["Kitalált Zenekar"])), ["PASS"])
        self.assertIn("FAIL", self.states(self.plan("Book", group="ebook", title="Novel", creators=["Képzelt Író"])))

    def test_video_names_and_aliases(self):
        for title in ("A lázadó teszt", "Splash", "Lecsapva", "Mosolygó – Holnap helyett MA"):
            self.assertIn("FAIL", self.states(self.plan("Video"+title, group="video", title=title)))

    def test_source_formats_and_name_without_year_are_blocked(self):
        for name in ("Film.2026.BRRip-GRP", "Film.1080p.HDCAM-GRP", "Film.CAM-GRP", "Film.2026.mHD-GRP", "Film.2026.R5.LINE-GRP"):
            self.assertIn("FAIL", self.states(self.plan(name, group="video", title="Film")))
        self.assertEqual(self.states(self.plan("Cam.2018.1080p.BluRay-GRP", group="video", title="Cam")), ["PASS"])

    def test_conditional_portable_and_xvid_cannot_auto_pass(self):
        self.assertIn("UNKNOWN", self.states(self.plan("Tool.Portable.1.0")))
        self.assertIn("UNKNOWN", self.states(self.plan("Film.2026.XviD-GRP", group="video", codec="mpeg4")))
        self.assertIn("FAIL", self.states(self.plan("Game.Hypervisor.Crack", group="game")))

    def test_stale_or_new_unknown_rule_blocks_affected_uploads(self):
        data=snapshot();data["checked_at"]-=banlist.MAX_AGE+1
        self.assertEqual(self.states(self.plan("Normal"),data),["UNKNOWN"])
        data=snapshot();data["rules"].append({"section":"program","kind":"unknown","text":"new semantic rule"})
        self.assertIn("UNKNOWN",self.states(self.plan("Normal2"),data))

    def test_refresh_records_history_and_never_uses_success_after_failure(self):
        client=SimpleNamespace(banlist_page=lambda:FIXTURE.read_text(encoding="utf-8"))
        current=banlist.refresh(client,self.work)
        self.assertTrue((self.work/'banlist/history'/(current['sha256']+'.json')).exists())
        self.assertEqual(banlist.load(self.work)['sha256'],current['sha256'])
        previous=(self.work/'banlist/current.json').read_bytes()
        client.banlist_page=mock.Mock(side_effect=NcoreError('unavailable'))
        with self.assertRaises(NcoreError):banlist.refresh(client,self.work)
        self.assertEqual((self.work/'banlist/current.json').read_bytes(),previous)
        self.assertFalse(json.loads((self.work/'banlist/status.json').read_text())['ok'])
        with self.assertRaises(NcoreError):banlist.load(self.work)

    def test_weekly_mirror_age_and_missing_list(self):
        with self.assertRaises(NcoreError):banlist.load(self.work)
        client=SimpleNamespace(banlist_page=lambda:FIXTURE.read_text(encoding='utf-8'))
        current=banlist.refresh(client,self.work)
        with mock.patch.object(banlist.time,'time',return_value=current['checked_at']+8*24*3600):
            self.assertEqual(banlist.load(self.work)['sha256'],current['sha256'])
        with mock.patch.object(banlist.time,'time',return_value=current['checked_at']+banlist.MAX_AGE+1):
            with self.assertRaises(NcoreError):banlist.load(self.work)


class LastUploadGateTests(CliCase):
    def test_new_ban_after_preview_prevents_post_and_reservation(self):
        root=self.book()
        class ChangedClient(FakeClient):
            calls=0
            def banlist_page(self):
                self.calls+=1
                page=super().banlist_page()
                return page.replace(html.escape("- Képzelt Író"), "- Szerző Elek<br/>" + html.escape("- Képzelt Író"))
        client=ChangedClient()
        from test_cli_run import LOCAL_TORRENT
        def hash_and_refresh(*a,**kw):
            banlist.refresh(client,self.config.work_dir)  # the scheduled script runs during hashing
            return LOCAL_TORRENT
        self.create.side_effect=hash_and_refresh
        code,_=self.run_cli(root,'-c','ebook_hun','--yes','--force',client=client,meta=self.book_meta(root))
        self.assertEqual(code,5)
        self.assertEqual(client.uploads,[])
        self.assertEqual(client.calls,1)
        self.assertFalse((self.run_dir()/'state.json').exists())
        self.assertTrue((self.run_dir()/'banlist-before-upload.json').exists())

    def test_unavailable_refresh_after_preview_prevents_post(self):
        root=self.book()
        class FailedClient(FakeClient):
            calls=0
            def banlist_page(self):
                self.calls+=1
                if self.calls>1:raise NcoreError('unavailable')
                return super().banlist_page()
        client=FailedClient()
        from test_cli_run import LOCAL_TORRENT
        def failed_refresh(*a,**kw):
            client.calls=1
            try:banlist.refresh(client,self.config.work_dir)
            except NcoreError:pass  # failed timer is recorded in status.json
            return LOCAL_TORRENT
        self.create.side_effect=failed_refresh
        with self.assertRaises(NcoreError):
            self.run_cli(root,'-c','ebook_hun','--yes',client=client,meta=self.book_meta(root))
        self.assertEqual(client.uploads,[])
        self.assertFalse((self.run_dir()/'state.json').exists())

    def test_live_hint_receives_source_and_resolved_names_with_actual_mode(self):
        root=self.book();seen=[]
        client=FakeClient()
        client.banlist_page=mock.Mock(side_effect=AssertionError('uploads must use the weekly mirror'))
        def hint(token,category,name,**kwargs):
            seen.append((name,kwargs))
            return ''
        client.banned_notice=hint
        code,_=self.run_cli(root,'-c','ebook_hun','--dry-run',client=client,meta=self.book_meta(root))
        self.assertEqual(code,0)
        self.assertTrue(all(not kw['original'] for _,kw in seen))
        self.assertTrue(any(name==root.name for name,_ in seen))
        self.assertTrue(any(name=='A könyv' for name,_ in seen))
        self.assertTrue(all(kw['fields']['konyv_cime']=='A könyv' for _,kw in seen))
        client.banlist_page.assert_not_called()
