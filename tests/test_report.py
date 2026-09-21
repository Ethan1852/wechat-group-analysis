"""Offline checks using fictional data only; never opens a WeChat process."""
import copy
import importlib.util
import json
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('renderer', ROOT / 'scripts/render_report.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = (ROOT / '.test-output').resolve()
        self.temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.run = Path(self.temp.name).resolve()
        self.assertTrue(self.run.is_relative_to(self.temp_root))
        self.addCleanup(self.temp.cleanup)
        self.raw = json.loads((ROOT / 'examples/synthetic/messages.json').read_text(encoding='utf-8'))
        self.report = json.loads((ROOT / 'examples/synthetic/report.json').read_text(encoding='utf-8'))

    def load(self):
        (self.run / 'messages.json').write_text(json.dumps(self.raw, ensure_ascii=False), encoding='utf-8')
        (self.run / 'report.json').write_text(json.dumps(self.report, ensure_ascii=False), encoding='utf-8')
        return renderer.load_data(self.run, self.run / 'report.json')

    def test_counts_are_derived_and_all_text_reaches_html(self):
        data = self.load()
        self.assertEqual((data['count'], data['senders'], data['media']), (6, 3, 1))
        renderer.write_text_outputs(data, self.run)
        page = (self.run / 'index.html').read_text(encoding='utf-8')
        for section in data['sections']:
            for card in section['cards']:
                for paragraph in card['paragraphs']:
                    self.assertIn(paragraph, page)

    def test_unknown_and_boolean_reference_ids_rejected(self):
        for refs in ([99999], [True]):
            self.report['topics'][0]['message_ids'] = refs
            with self.assertRaises(ValueError):
                self.load()

    def test_end_of_window_is_exclusive(self):
        self.raw['messages'][0]['time'] = self.raw['end_exclusive']
        with self.assertRaises(ValueError):
            self.load()

    def test_bad_message_count_and_duplicate_ids_rejected(self):
        self.raw['message_count'] += 1
        with self.assertRaises(ValueError):
            self.load()
        self.raw['message_count'] -= 1
        self.raw['messages'][1]['id'] = self.raw['messages'][0]['id']
        with self.assertRaises(ValueError):
            self.load()

    def test_html_escapes_text_instead_of_executing_it(self):
        self.report['topics'][0]['paragraphs'] = ['<script>alert(1)</script><img src=x onerror=alert(2)> & text']
        renderer.write_text_outputs(self.load(), self.run)
        page = (self.run / 'index.html').read_text(encoding='utf-8')
        tags = Tags()
        tags.feed(page)
        self.assertNotIn('script', tags.tags)
        self.assertNotIn('img', tags.tags)
        self.assertIn('&lt;script&gt;', page)

    def test_empty_different_group_and_duration(self):
        self.raw.update(group='虚构零消息群', messages=[], message_count=0,
                        start='2026-02-01T00:00:00+08:00', end_exclusive='2026-02-01T12:00:00+08:00')
        self.report = {'intro': '此窗口内没有本地记录。', 'topics': [], 'followups': [], 'resolved': [], 'other': []}
        data = self.load()
        self.assertEqual((data['count'], data['senders'], data['hours'], data['date']), (0, 0, '12', '2026.02.01'))
        renderer.write_text_outputs(data, self.run)
        page = (self.run / 'index.html').read_text(encoding='utf-8')
        self.assertIn('虚构零消息群', page)
        self.assertNotIn('网页评审', page)

    @unittest.skipUnless(Path('C:/Windows/Fonts/msyh.ttc').is_file(), 'PNG smoke test uses Windows Chinese fonts')
    def test_png_is_valid(self):
        from PIL import Image
        size = renderer.render_png(self.load(), self.run)
        with Image.open(self.run / 'report.png') as image:
            self.assertEqual(image.format, 'PNG')
            self.assertEqual(image.size, size)
            image.verify()


if __name__ == '__main__':
    unittest.main()
