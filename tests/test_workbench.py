"""Synthetic data only: candidate semantics, persistence, WAL commit boundaries, API."""
import json
import sqlite3
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from workbench.analysis import local_analysis, validate_model, analyze
from workbench.reader import checksum, merge_committed_wal
from workbench.store import Store
from workbench.config import protect


def message(uid, text, own=False, group=False, timestamp=1):
    return {'id': uid, 'content': text, 'is_self': own, 'is_group': group, 'sender_status': 'resolved', 'type': '文本',
            'chat_id': 'group@chatroom' if group else 'friend', 'chat_name': '测试会话', 'sender': '我' if own else '小王',
            'timestamp': timestamp, 'time': '2026-09-20T18:00:00+08:00', 'transcript': None, 'sender_username': 'me' if own else 'friend'}


class AnalysisTests(unittest.TestCase):
    def test_private_reply_and_ack(self):
        items, _ = local_analysis([message('a', '明天能给我报价吗？')], {})
        self.assertEqual({i['kind'] for i in items}, {'task', 'reply'})
        self.assertEqual(local_analysis([message('b', '谢谢！')], {})[0], [])

    def test_reply_does_not_complete_task(self):
        items, _ = local_analysis([message('a', '请提交报告'), message('b', '收到', True, timestamp=2)], {})
        self.assertIn('task', [i['kind'] for i in items])
        self.assertNotIn('reply', [i['kind'] for i in items])
        self.assertTrue(all(i['suggested_status'] != 'completed' for i in items))

    def test_group_other_person_not_assigned(self):
        items, _ = local_analysis([message('a', '@小李 请完成报告', group=True)], {'self_name': '小陈'})
        self.assertEqual(items[0]['owner'], '待确认是否与我有关')
        self.assertNotIn('reply', [i['kind'] for i in items])

    def test_mention_has_boundary(self):
        items, _ = local_analysis([message('a', '@小陈老师 请回复？', group=True)], {'self_name': '小陈'})
        self.assertNotIn('reply', [i['kind'] for i in items])
        items, _ = local_analysis([message('a', '@小陈\u2005请回复？', group=True)], {'self_name': '小陈'})
        self.assertIn('reply', [i['kind'] for i in items])

    def test_unknown_sender_not_guessed(self):
        m = message('a', '请给我报告') | {'sender_status': 'unmapped_sender_id', 'is_self': None}
        self.assertEqual(local_analysis([m], {})[0], [])

    def test_invented_evidence_rejected(self):
        with self.assertRaises(ValueError):
            validate_model({'items': [{'kind': 'task', 'anchor_id': 'a', 'evidence_ids': ['missing']}]}, {'a': message('a', '请做报告')})

    def test_model_failure_marked(self):
        with patch('workbench.analysis.model_request', side_effect=RuntimeError('unavailable')):
            items, _, result = analyze([message('a', '可以回复吗？')], {'external_enabled': True})
        self.assertTrue(items)
        self.assertEqual(result['mode'], 'local')
        self.assertTrue(result['warnings'])

    def test_valid_model_response(self):
        output = {'items': [{'kind': 'task', 'anchor_id': 'm1', 'evidence_ids': ['m1'], 'title': '提交报告', 'owner': '我', 'deadline': '未明确'}],
                  'summary': '对方请求提交报告，待完成。', 'summary_evidence_ids': ['m1']}
        with patch('workbench.analysis.model_request', return_value=json.dumps(output)):
            items, summaries, result = analyze([message('a', '请提交报告')], {'external_enabled': True})
        self.assertEqual(result['mode'], 'ai')
        self.assertEqual(items[0]['anchor_id'], 'a')
        self.assertEqual(summaries[0]['evidence_ids'], ['a'])

    def test_dpapi_roundtrip(self):
        original = b'synthetic-test-key-not-real'
        ciphertext = protect(original)
        self.assertNotIn(original, ciphertext)
        self.assertEqual(protect(ciphertext, True), original)


class StoreTests(unittest.TestCase):
    def test_reimport_keeps_decisions_and_accounts_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'test.sqlite')
            m = message('a', '请提交报告')
            items, summaries = local_analysis([m], {})
            store.save({'account': 'account1'}, [m], items, summaries, {})
            store.set_status('account1', items[0]['id'], 'done')
            store.save({'account': 'account1'}, [m], items, summaries, {})
            self.assertEqual(next(i for i in store.items('account1') if i['id'] == items[0]['id'])['status'], 'done')
            self.assertEqual(len(store.messages('account1')), 1)
            self.assertEqual(store.items('account2'), [])
            store.save({'account': 'account1'}, [], [], [], {})
            self.assertTrue(store.items('account1'))

    def test_pending_evidence_survives_window(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'test.sqlite')
            m = message('a', '请给我资料')
            items, summaries = local_analysis([m], {})
            store.save({'account': 'a'}, [m], items, summaries, {})
            context = store.context_for_pending('a', [message('b', '还没收到', timestamp=2)])
            self.assertEqual({m['id'] for m in context}, {'a', 'b'})


class WalTests(unittest.TestCase):
    def test_uncommitted_tail_not_applied(self):
        salt = b'12345678'
        head = struct.pack('>IIII', 0x377f0682, 3007000, 4096, 0) + salt
        state = checksum(head)
        raw = head + struct.pack('>II', *state)
        for committed, value in [(1, b'A'), (0, b'B')]:
            h = struct.pack('>II', 1, committed) + salt
            page = value * 4096
            state = checksum(h[:8] + page, state)
            raw += h + struct.pack('>II', *state) + page
        with tempfile.TemporaryDirectory() as temp:
            db, wal = Path(temp) / 'db', Path(temp) / 'wal'
            db.write_bytes(b'X' * 4096)
            wal.write_bytes(raw)
            n = merge_committed_wal(db, wal, b'', lambda k, p, i: p)
            self.assertEqual(n, 1)
            self.assertEqual(db.read_bytes(), b'A' * 4096)

    def test_header_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            db, wal = Path(temp) / 'db', Path(temp) / 'wal'
            db.write_bytes(b'X' * 4096)
            wal.write_bytes(struct.pack('>III', 0x377f0682, 3007000, 4096) + b'0' * 20)
            with self.assertRaises(RuntimeError):
                merge_committed_wal(db, wal, b'', lambda k, p, i: p)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from workbench.server import Handler, ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_page_and_token_required(self):
        with urllib.request.urlopen(self.url) as response:
            page = response.read().decode()
        self.assertIn('晚笺', page)
        self.assertNotIn('__TOKEN__', page)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url + '/api/state')
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_foreign_origin_rejected(self):
        from workbench.server import TOKEN
        req = urllib.request.Request(self.url + '/api/status', b'{}', {'X-Workbench-Token': TOKEN, 'Origin': 'https://foreign.example'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()


class PngTests(unittest.TestCase):
    def test_embedded_newlines_are_layout_lines(self):
        from workbench.reports import render_png
        from PIL import Image, ImageChops
        with tempfile.TemporaryDirectory() as temp:
            a, b = Path(temp) / 'a', Path(temp) / 'b'
            a.mkdir()
            b.mkdir()
            render_png(['第一行\n第二行\n第三行'], a)
            render_png(['第一行', '第二行', '第三行'], b)
            with Image.open(a / 'report-01.png') as first, Image.open(b / 'report-01.png') as second:
                self.assertEqual(first.size, second.size)
                self.assertIsNone(ImageChops.difference(first, second).getbbox())


if __name__ == '__main__':
    unittest.main()
