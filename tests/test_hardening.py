from __future__ import annotations
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ncore_up import privacy, safeio, release, torrent, net, plan, requirements, categories, form, compose, duplicates
from ncore_up.meta import Meta
from ncore_up.local import apk, ipa, ebookmeta, installer, audiotags
from ncore_up.resolve import music


class BoundedReaderTests(unittest.TestCase):
    def test_file_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'note.nfo'
            path.write_bytes(b'12345')
            self.assertEqual(safeio.read_file(path, 5), b'12345')
            with self.assertRaises(safeio.LimitError):
                safeio.read_file(path, 4)

    def test_zip_size_and_compression_ratio(self):
        with io.BytesIO() as data:
            with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr('bomb', b'A' * 100000)
                z.writestr('small', b'OK')
            data.seek(0)
            with zipfile.ZipFile(data) as z:
                self.assertEqual(safeio.read_zip(z, 'small'), b'OK')
                with self.assertRaises(safeio.LimitError):
                    safeio.read_zip(z, 'bomb')
                with self.assertRaises(safeio.LimitError):
                    safeio.read_zip(z, 'small', 1)

    def test_apk_epub_ipa_decline_oversized_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'large.apk'
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                for name in ('AndroidManifest.xml','META-INF/container.xml','Payload/Test.app/Info.plist'):
                    z.writestr(name, b'X' * (safeio.META_LIMIT + 1))
            self.assertEqual(apk.manifest_from_zip(path), {})
            self.assertFalse(ebookmeta.read_epub(path)['title'])
            self.assertTrue(ipa.inspect(path)['warnings'])

    def test_package_copy_checks_size_before_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'bundle.zip'
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('base.apk', b'12345')
            with zipfile.ZipFile(path) as z:
                with self.assertRaises(safeio.LimitError):
                    safeio.copy_zip(z,'base.apk',Path(tmp)/'out.apk',limit=4)
            self.assertFalse((Path(tmp)/'out.apk').exists())

    def test_package_copy_never_deletes_a_preexisting_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bundle.zip';target=Path(tmp)/'out.apk'
            target.write_bytes(b'keep')
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('base.apk',b'new')
            with zipfile.ZipFile(path) as z,self.assertRaises(FileExistsError):
                safeio.copy_zip(z,'base.apk',target)
            self.assertEqual(target.read_bytes(),b'keep')

    def test_extract_refuses_unknown_or_excessive_size(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(installer,'_sevenzip',return_value='7z'), mock.patch.object(installer,'_tool') as run:
            for size in (0, -1, safeio.PACKAGE_LIMIT+1):
                self.assertIsNone(installer.extract_member('a.zip','a.exe',tmp,size))
        run.assert_not_called()


class ProcessLimitsTests(unittest.TestCase):
    def test_stdin_is_closed_and_output_is_returned(self):
        result = safeio.run([sys.executable,'-c','import sys;print(repr(sys.stdin.read()));sys.stderr.write("err")'])
        self.assertEqual(result.returncode,0)
        self.assertEqual(result.stdout.strip(), b"''")
        self.assertEqual(result.stderr,b'err')

    def test_output_overflow_and_prefix(self):
        command = [sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*1000000)']
        with self.assertRaises(safeio.LimitError):
            safeio.run(command,stdout_limit=1000)
        self.assertEqual(safeio.run(command,stdout_limit=1000,prefix_only=True).stdout,b'x'*1000)

    def test_timeout_kills_the_process(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            safeio.run([sys.executable,'-c','import time;time.sleep(30)'],timeout=0.15)

    def test_linux_limits_are_applied_before_the_tool(self):
        with mock.patch.object(safeio.sys,'platform','linux'),mock.patch.object(safeio.shutil,'which',return_value='/usr/bin/prlimit'):
            cmd=safeio.command(['7z','l'])
        self.assertEqual(cmd[-3:],['--','7z','l'])
        self.assertIn('--as=4294967296',cmd)
        self.assertIn('--fsize=2147483648',cmd)


class PrivateDiagnosticsTests(unittest.TestCase):
    def test_html_queries_headers_and_path_keys_are_redacted(self):
        secret='FAKE-PRIVATE-VALUE'
        body=(f'<input value="{secret}" type="hidden" name="getUnique">'
              f'<a href="torrents.php?id=7&amp;key={secret}">download</a> '
              f'password={secret}\nAuthorization: Bearer {secret}\n'
              'https://tracker.example/0123456789abcdef0123456789abcdef/announce\n'
              'https://ncore.pro/torrents.php?action=details&id=7')
        result=privacy.redact(body)
        self.assertNotIn(secret,result)
        self.assertNotIn('0123456789abcdef0123456789abcdef',result)
        self.assertIn('action=details&id=7',result)

    def test_json_token_is_redacted(self):
        self.assertNotIn('FAKE-TOKEN',privacy.redact('{"access_token":"FAKE-TOKEN","id":1}'))
        self.assertIn('"id":1',privacy.redact('{"access_token":"FAKE-TOKEN","id":1}'))

    def test_acoustid_client_is_an_ignored_cache_parameter(self):
        self.assertIn('client',net.SECRET_PARAMS)

    def test_binary_download_is_streamed_and_bounded(self):
        response=mock.Mock(headers={})
        response.iter_content.return_value=iter([b'123',b'456'])
        with mock.patch.object(net,'request',return_value=response) as request:
            self.assertEqual(net.get_bytes('https://example.test/image',max_bytes=5,quiet=True),b'')
        self.assertTrue(request.call_args.kwargs['stream'])
        response.close.assert_called_once()

    def test_content_length_refuses_before_reading(self):
        response=mock.Mock(headers={'Content-Length':'100'})
        with mock.patch.object(net,'request',return_value=response):
            self.assertEqual(net.get_bytes('https://example.test/image',max_bytes=5,quiet=True),b'')
        response.iter_content.assert_not_called()

    def test_oauth_never_uses_cached_session(self):
        plain=mock.MagicMock()
        plain.request.return_value=mock.Mock(status_code=200,from_cache=False)
        cached=mock.Mock(headers={'User-Agent':'test-agent'})
        with mock.patch.object(net,'session',return_value=cached),mock.patch.object(net.requests,'Session') as factory,mock.patch.object(net,'_pace'):
            factory.return_value.__enter__.return_value=plain
            net.request('POST','https://accounts.spotify.com/api/token',expire=0)
        cached.request.assert_not_called()
        plain.request.assert_called_once()


class LinkSafetyTests(unittest.TestCase):
    def test_release_refuses_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'release';root.mkdir()
            outside=Path(tmp)/'outside';outside.mkdir();(outside/'secret').write_text('private')
            try:
                (root/'linked').symlink_to(outside/'secret')
                (root/'linked-dir').symlink_to(outside,target_is_directory=True)
            except OSError:
                self.skipTest('symlink privilege unavailable')
            with mock.patch.object(release,'hungarian_audio',return_value=None):
                result=release.inspect(root)
            self.assertEqual(result.files,[])
            self.assertEqual(len(result.problems),2)
            with self.assertRaises(ValueError):
                release.assert_safe_tree(root)
            with mock.patch.object(torrent.subprocess,'run') as run,self.assertRaises(RuntimeError):
                torrent.create('mkbrr',root,Path(tmp)/'out.torrent')
            run.assert_not_called()


class CategoryGateTests(unittest.TestCase):
    def test_wrong_audio_format_is_a_mandatory_failure_even_for_originals(self):
        for code,codec in [('mp3','flac'),('lossless_hun','mp3')]:
            with self.subTest(code=code):
                meta=Meta()
                local=music.Local(files=[audiotags.AudioTags(Path('track'),codec=codec)])
                music._category_checks(meta,local,code,320)
                self.assertTrue(meta.extra['category_errors'])
                rel=release.Release(Path('release'),'release',[],None,False,False)
                p=plan.Plan(rel,categories.CATEGORIES[code],form.UploadData(category=code,name='release',original=True),meta=meta,confidence=1)
                checks=requirements.evaluate(p)
                self.assertTrue(any(x.id=='content_category' and x.status=='FAIL' for x in checks))
                self.assertFalse(p.auto_ok)

    def test_mixed_lossy_lossless_is_not_suggested_as_a_valid_mp3_album(self):
        meta=Meta();local=music.Local(files=[audiotags.AudioTags(Path('a.flac'),codec='flac'),audiotags.AudioTags(Path('b.mp3'),codec='mp3')])
        music._category_checks(meta,local,'lossless',320)
        self.assertTrue(meta.extra['category_errors'])
        self.assertNotIn('category_hint',meta.extra)

    def test_audiobook_respects_category_bitrate_minimum(self):
        meta=Meta();local=music.Local(audiobook=True,files=[audiotags.AudioTags(Path('book.m4b'),codec='aac')])
        music._category_checks(meta,local,'mp3_hun',64)
        self.assertIn('category_errors',meta.extra)

    def test_mp3_category_refuses_mixed_audio_too(self):
        meta=Meta();local=music.Local(files=[audiotags.AudioTags(Path('a'),codec='flac'),audiotags.AudioTags(Path('b'),codec='mp3')])
        music._category_checks(meta,local,'mp3',320)
        self.assertTrue(meta.extra['category_errors'])
        self.assertNotIn('category_hint',meta.extra)

    def test_review_reasons_block_automatic_upload(self):
        rel=release.Release(Path('release'),'release',[],None,False,False)
        p=plan.Plan(rel,categories.CATEGORIES['mp3'],form.UploadData(category='mp3',name='release',original=True))
        plan._take_meta(p,Meta(confidence=1,extra={'review_reasons':['HUN kategória ellenőrzése']}))
        self.assertEqual(p.doubts,['HUN kategória ellenőrzése'])
        self.assertFalse(p.auto_ok)

    def test_every_description_source_is_credited(self):
        for source in ('PORT.hu','TMDB','Google Books','TVmaze'):
            result='\n'.join(compose.plot_block(Meta(description_hu='Rövid saját tesztszöveg.',description_source=source)))
            self.assertIn('Forrás: '+source,result)
        self.assertIn('CC BY-SA',result)

    def test_duplicate_book_page_images_are_a_mandatory_failure(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            rel=release.Release(Path(tmp),'book',[],None,False,False)
            p=plan.Plan(rel,categories.CATEGORIES['ebook_hun'],form.UploadData(category='ebook_hun',name='book',original=False))
            for i in range(1,4):
                path=Path(tmp)/f'page{i}.png'
                Image.new('RGB',(10,10),'white').save(path)
                p.attachments[f'kep{i}']=path
            checks=requirements.evaluate(p)
            self.assertEqual(next(x.status for x in checks if x.id=='book_distinct_pages'),'FAIL')
            for i in range(1,4):Image.new('RGB',(10,10),(i,0,0)).save(p.attachments[f'kep{i}'])
            checks=requirements.evaluate(p)
            self.assertEqual(next(x.status for x in checks if x.id=='book_distinct_pages'),'PASS')


class SeriesDuplicateTests(unittest.TestCase):
    def decision(self,own,other,answers=None):
        rel=release.Release(Path(own),own,[],None,False,False)
        p=plan.Plan(rel,categories.CATEGORIES['hdser'],form.UploadData(category='hdser',name=own,original=False))
        return duplicates.decide(p,[{'torrent_id':'1','release_name':other,'category':'hdser'}],answers)[0]

    def test_only_proven_disjoint_episodes_are_allowed(self):
        own='Show.S02E08.1080p.WEB.x264-GRP'
        cases=[('Show.S02E07.1080p-GRP','ALLOW'),('Show.S01.1080p-GRP','ALLOW'),
               ('Show.S02E08.2160p-GRP','REVIEW'),('Show.S02.1080p-GRP','REVIEW'),
               ('Show.S01-S02.1080p-GRP','REVIEW'),('Show.S01-02.1080p-GRP','REVIEW'),
               ('Show.S02E07E09E10.1080p-GRP','REVIEW'),('Show.1080p-GRP','REVIEW')]
        for other,expected in cases:
            with self.subTest(other=other):self.assertEqual(self.decision(own,other).status,expected)

    def test_episode_ranges_and_packs(self):
        self.assertEqual(self.decision('Show.S02E01-E03.1080p','Show.S02E04.1080p').status,'ALLOW')
        self.assertEqual(self.decision('Show.S02E01-E03.1080p','Show.S02E03.1080p').status,'REVIEW')
        self.assertEqual(self.decision('Show.S02.1080p','Show.S02E03.1080p').status,'REVIEW')

    def test_explicit_user_block_is_preserved(self):
        d=self.decision('Show.S02E08.1080p','Show.S02E07.1080p',{'1':{'action':'block','reason':'Felhasználó által ellenőrzött tiltás'}})
        self.assertEqual(d.status,'BLOCK')
