"""Ordered queues, durable restart, and all network activity stubbed."""
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ncore_up import batch, cli, workflow
from ncore_up.client import NcoreError, SessionExpired
from test_cli_run import CliCase, FakeClient


class ListTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)

    def test_text_bom_spaces_comments_and_relative_paths(self):
        path=self.root/'list.txt'
        path.write_text('\ufeff# comment\n\nAlbum With Spaces\n./Film.mkv\n[FLAC] Album\n',encoding='utf-8')
        self.assertEqual([row['path'] for row in batch.read_list(path)], [str(self.root/'Album With Spaces'),str(self.root/'Film.mkv'),str(self.root/'[FLAC] Album')])

    def test_json_preserves_order_selection_and_per_item_mode(self):
        path=self.root/'list.json'
        path.write_text(json.dumps({'version':1,'items':[{'path':'Film','original':True,'answers':'film.json'},
            {'path':'Album','original':False,'category':'lossless','enabled':False},'Book']}),encoding='utf-8')
        rows=batch.read_list(path)
        self.assertEqual([Path(row['path']).name for row in rows],['Film','Album','Book'])
        self.assertEqual(rows[0]['answers'],str(self.root/'film.json'))
        self.assertFalse(rows[1]['enabled']);self.assertFalse(rows[1]['original'])

    def test_duplicate_alias_paths_unknown_fields_and_empty_list_rejected(self):
        for values in (['Film','./Film'],[],[{'path':'Film','command':'anything'}],[{'path':'Film','original':'false'}],
                       [{'path':'Film','enabled':False}],[{'path':'x\ny'}]):
            with self.subTest(values=values),self.assertRaises(ValueError):batch.entries(values,self.root)

    def test_paths_are_not_shell_commands(self):
        rows=batch.entries(['$(touch hacked); --yes'],self.root)
        self.assertTrue(rows[0]['path'].endswith('$(touch hacked); --yes'))
        self.assertFalse((self.root/'hacked').exists())

    def test_directory_ignores_working_files_and_sidecars(self):
        for name in ['Film.mkv','Film.nfo','Film.ncore.json','cover.jpg','.hidden','checks.sfv']:
            (self.root/name).write_text('x')
        (self.root/'NCUP').mkdir()
        (self.root/'Album').mkdir()
        self.assertEqual([Path(row['path']).name for row in batch.directory(self.root)],['Album','Film.mkv'])

    def test_same_queue_lock_cannot_be_taken_twice(self):
        with batch.lock(self.root):
            with self.assertRaises(ValueError):
                with batch.lock(self.root):pass
        with batch.lock(self.root):pass

    def test_missing_resume_file_reports_queue_error_before_cookie_loading(self):
        with mock.patch.object(cli.Config,'load',return_value=mock.Mock()), mock.patch.object(cli,'load_cookies') as cookies, mock.patch.object(cli,'say') as say:
            self.assertEqual(cli.main(['--resume-batch',str(self.root/'missing.json')]),2)
        cookies.assert_not_called()
        self.assertIn('queue.json',say.call_args.args[0])


class QueueTests(CliCase):
    def args(self, *flags, entries=None):
        path=self.tmp/'list.json'
        path.write_text(json.dumps(entries or ['one','two','three']),encoding='utf-8')
        return cli.build_parser().parse_args(['--batch-list',str(path),*flags])

    def state(self,args):
        return json.loads(Path(args._batch_file).read_text())

    def finished(self,child,config,client):
        folder=config.log_dir/Path(child.path).name
        workflow.private_dir(folder)
        cli._run_folder(child,folder)
        workflow.write_json(folder/'state.json',{'status':'complete','torrent_id':100+len(Path(child.path).name)})
        return 0

    def execute(self,args,runner):
        return batch.execute(args,self.config,FakeClient(),runner,self.out.append)

    def test_order_disabled_entry_and_no_setting_leak(self):
        args=self.args('--auto',entries=[{'path':'one','original':True,'category':'hd'},
            {'path':'disabled','enabled':False}, {'path':'two','original':False}, 'three'])
        seen=[]
        def runner(child,config,client):
            seen.append((Path(child.path).name,child.original,child.category))
            child.title='mutated by prior answer file'
            return self.finished(child,config,client)
        self.assertEqual(self.execute(args,runner),0)
        self.assertEqual(seen,[('one',True,'hd'),('two',False,None),('three',None,None)])
        state=self.state(args)
        self.assertEqual([i['status'] for i in state['items']],['complete','disabled','complete','complete'])
        self.assertFalse(state['options'].get('title'))

    def test_content_problem_continues_and_successful_items_are_skipped_on_resume(self):
        args=self.args('--auto');calls=[]
        def runner(child,config,client):
            calls.append(Path(child.path).name)
            return 10 if Path(child.path).name=='two' else self.finished(child,config,client)
        self.assertEqual(self.execute(args,runner),10)
        self.assertEqual(calls,['one','two','three'])
        resumed=cli.build_parser().parse_args(['--resume-batch',args._batch_file])
        batch.restore(resumed)
        calls.clear()
        def retry(child,config,client):
            calls.append(Path(child.path).name);return self.finished(child,config,client)
        self.assertEqual(self.execute(resumed,retry),0)
        self.assertEqual(calls,['two'])

    def test_session_and_daily_limit_stop_before_remaining_items(self):
        for error,status in ((SessionExpired('expired'),'needs_login'),(workflow.DailyLimitReached('quota'),'waiting_daily_limit')):
            with self.subTest(status=status):
                args=self.args('--auto');calls=[]
                def runner(child,config,client):
                    calls.append(child.path);raise error
                self.assertIn(self.execute(args,runner),(3,10))
                self.assertEqual(len(calls),1)
                self.assertEqual([i['status'] for i in self.state(args)['items']],[status,'pending','pending'])

    def test_progress_is_saved_before_and_during_work(self):
        args=self.args('--auto')
        def interrupt(child,config,client):
            current=self.state(args)
            self.assertEqual(current['items'][0]['status'],'running')
            folder=config.log_dir/'one';workflow.private_dir(folder);cli._run_folder(child,folder)
            self.assertEqual(self.state(args)['items'][0]['run_dir'],str(folder))
            workflow.write_json(folder/'state.json',{'status':'uncertain'})
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.execute(args,interrupt)
        state=self.state(args)
        self.assertEqual(state['status'],'interrupted')
        self.assertEqual([i['status'] for i in state['items']],['interrupted','pending','pending'])
        resumed=cli.build_parser().parse_args(['--resume-batch',args._batch_file]);batch.restore(resumed)
        def retry(child,config,client):
            if Path(child.path).name=='one':
                self.assertEqual(child._resume_run,state['items'][0]['run_dir'])
            return self.finished(child,config,client)
        self.assertEqual(self.execute(resumed,retry),0)

    def test_seeding_or_uncertain_upload_stops_queue(self):
        for status,code in (('seed_pending',11),('uncertain',9)):
            with self.subTest(status=status):
                args=self.args('--auto');seen=[]
                def runner(child,config,client):
                    seen.append(child.path)
                    self.finished(child,config,client)
                    workflow.write_json(Path(child._run_dir)/'state.json',{'status':status})
                    return code
                self.assertEqual(self.execute(args,runner),10)
                self.assertEqual(len(seen),1)
                self.assertEqual(self.state(args)['items'][0]['status'],status)

    def test_preview_is_distinct_from_upload_and_cannot_be_promoted_on_resume(self):
        args=self.args('--dry-run')
        self.assertEqual(self.execute(args,lambda child,config,client:0),0)
        self.assertEqual([i['status'] for i in self.state(args)['items']],['previewed']*3)
        resumed=cli.build_parser().parse_args(['--resume-batch',args._batch_file]);batch.restore(resumed)
        self.assertTrue(resumed.dry_run)
        unsafe=cli.build_parser().parse_args(['--resume-batch',args._batch_file,'--auto'])
        with self.assertRaises(ValueError):batch.restore(unsafe)

    def test_declining_a_prompt_does_not_count_as_uploaded(self):
        args=self.args(entries=['one'])
        def runner(child,config,client):child._outcome='skipped';return 0
        self.assertEqual(self.execute(args,runner),10)
        self.assertEqual(self.state(args)['items'][0]['status'],'skipped')

    def test_run_exception_does_not_leak_previous_run_path(self):
        args=self.args('--auto',entries=['one','two'])
        def runner(child,config,client):
            if Path(child.path).name=='one':return self.finished(child,config,client)
            raise ValueError('invalid data')
        self.assertEqual(self.execute(args,runner),10)
        self.assertNotIn('run_dir',self.state(args)['items'][1])

    def test_actual_cli_dry_runs_two_items_without_uploading(self):
        first=self.book();second=first.parent/'Other book';second.mkdir();(second/'book.epub').write_bytes(b'book')
        args=self.args('--dry-run','--auto','-c','ebook_hun',entries=[str(first),str(second)])
        client=FakeClient()
        with mock.patch('ncore_up.resolve.books.resolve',side_effect=lambda release,*a,**k:self.book_meta(release.path)):
            code=cli.batch(args,self.config,client)
        self.assertEqual(code,0)
        self.assertEqual(client.uploads,[])
        self.assertEqual(len(self.state(args)['items']),2)
        self.assertTrue(all(i['status']=='previewed' for i in self.state(args)['items']))

    def test_actual_already_uploaded_item_uses_ledger(self):
        root=self.book()
        args=self.args('--auto','-c','ebook_hun',entries=[str(root)])
        client=FakeClient()
        with mock.patch('ncore_up.resolve.books.resolve',return_value=self.book_meta(root)):
            self.assertEqual(cli.batch(args,self.config,client),0)
            again=self.args('--auto','-c','ebook_hun',entries=[str(root)])
            self.assertEqual(cli.batch(again,self.config,client),0)
        self.assertEqual(len(client.uploads),1)
        self.assertEqual(self.state(again)['items'][0]['status'],'already_complete')
