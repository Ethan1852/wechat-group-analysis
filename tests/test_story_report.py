import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workbench.analysis import analyze, local_analysis, validate_story
from workbench.digest import compose_digest, brief_lines, focus_items
from workbench.relevance import reference_metadata, relevance
from workbench.reports import render_html, build_report
from workbench.store import Store


def msg(uid='a', text='请确认时间', group=True, **extra):
    return {'id': uid, 'chat_id': 'group@chatroom' if group else 'friend', 'chat_name': '测试群' if group else '小王',
            'is_group': group, 'sender': '小王', 'sender_username': 'friend', 'sender_status': 'resolved', 'is_self': False,
            'time': '2026-09-20T18:00:00+08:00', 'timestamp': 1789898400, 'content': text, 'transcript': None,
            'source_db': 'test.db', 'source_table': 'Msg_test', 'local_id': 1, 'type': '文本', **extra}


META = {'account': 'test', 'start': '2026-09-20T00:00:00+08:00', 'end_exclusive': '2026-09-21T00:00:00+08:00',
        'self_id': 'me', 'self_name': '小陈', 'message_count': 1, 'chat_count': 1, 'coverage_note': '测试数据',
        'unknown_senders': 0, 'parse_errors': []}


def run(summaries=None):
    return {'meta': META, 'analysis': {'mode': 'ai', 'warnings': []}, 'summaries': summaries or []}


class ReferenceTests(unittest.TestCase):
    def test_structured_mention_and_quote(self):
        parser = type('Parser', (), {'decode_blob': staticmethod(lambda x: x)})
        row = {'source': '<msgsource><atuserlist><![CDATA[other,me]]></atuserlist></msgsource>',
               'message_content': '<msg><appmsg><refermsg><fromusr>me</fromusr><svrid>123456789123456789</svrid><displayname>小陈</displayname></refermsg></appmsg></msg>'}
        result = reference_metadata(row, parser, 'me')
        self.assertTrue(result['mentions_self'])
        self.assertTrue(result['quotes_self'])
        self.assertEqual(result['quoted_server_id'], '123456789123456789')
        row['message_content'] = '<msg><refermsg><fromusr>group@chatroom</fromusr><chatusr>me</chatusr></refermsg></msg>'
        self.assertTrue(reference_metadata(row, parser, 'me')['quotes_self'])

    def test_quoted_self_without_question_is_included(self):
        m = msg(text='这里还需要你确认一下。', quotes_self=True)
        items, _ = local_analysis([m], {})
        reply = next(i for i in items if i['kind'] == 'reply')
        self.assertEqual(reply['relation'], 'quote')

    def test_display_name_collision_is_not_confirmed_quote(self):
        self.assertEqual(relevance(msg(text='这个时间\n ↳ 引用 小陈：明天见'), {'self_name': '小陈'}), 'possible_quote')
        self.assertEqual(relevance(msg(text='请处理', quoted_sender_id='another', quoted_name='小陈'), {'self_id': 'me', 'self_name': '小陈'}), 'possible')

    def test_outgoing_request_is_not_my_promise(self):
        self.assertEqual(relevance(msg(text='麻烦你把清单发给我', is_self=True), {}), 'context')
        self.assertEqual(relevance(msg(text='我来准备报告', is_self=True), {}), 'commitment')


class StoryTests(unittest.TestCase):
    def test_story_references_are_validated(self):
        with self.assertRaises(ValueError):
            validate_story({'summary': '形成结论', 'summary_evidence_ids': ['missing']}, {'a': msg()})

    def test_multi_chunk_story_is_reduced_to_one_chat(self):
        messages = [msg(str(i), '讨论方案', timestamp=1789898400+i) for i in range(225)]
        def reply(cfg, prompt):
            data = json.loads(prompt[1]['content'])
            if 'messages' in data:
                uid = data['messages'][0]['id']
            else:
                uid = data['fragments'][0]['evidence_ids'][0]
            return json.dumps({'items': [], 'summary': '小王先提出方案，大家讨论后决定继续核实时间。', 'summary_evidence_ids': [uid],
                               'topics': [{'title': '安排时间', 'story': '从提议开始，最后仍待确认。', 'evidence_ids': [uid]}],
                               'conclusions': [{'text': '时间尚未确认。', 'evidence_ids': [uid]}]}, ensure_ascii=False)
        with patch('workbench.analysis.model_request', side_effect=reply) as request:
            _, summaries, status = analyze(messages, {'external_enabled': True})
        self.assertEqual(len(summaries), 1)
        self.assertEqual(status['mode'], 'ai')
        self.assertEqual(request.call_count, 3)
        self.assertEqual(len(summaries[0]['topics']), 1)

    def test_historical_evidence_is_not_todays_story(self):
        messages = [msg('old', timestamp=1)]
        reply = json.dumps({'items': [], 'summary': '', 'summary_evidence_ids': []})
        with patch('workbench.analysis.model_request', return_value=reply):
            _, summaries, _ = analyze(messages, {'external_enabled': True, 'reporting_window': {'start_ts': 100, 'end_ts': 200}})
        self.assertEqual(summaries, [])


class DigestTests(unittest.TestCase):
    def test_focus_does_not_get_flooded_by_one_chat(self):
        items = [{'chat_id': 'one', 'id': str(i)} for i in range(20)] + [{'chat_id': 'two', 'id': 'other'}]
        focus = focus_items(items, 5)
        self.assertEqual([i['chat_id'] for i in focus[:2]], ['one', 'two'])

    def test_transport_xml_does_not_become_a_task_title(self):
        m = msg(text='[结构化消息；未解析内容] <room_type>1</room_type>', group=False)
        items, _ = local_analysis([m], {})
        digest = compose_digest(run(), [i | {'status': 'open'} for i in items], [m])
        self.assertNotIn('<room_type>', digest['personal'][0]['title'])
        self.assertTrue(digest['personal'][0]['needs_content_review'])

    def test_possible_group_requests_not_in_personal(self):
        m = msg(text='@其他人 请提交报告')
        items, _ = local_analysis([m], {'self_name': '小陈'})
        items = [i | {'status': 'open'} for i in items]
        digest = compose_digest(run(), items, [m])
        self.assertEqual(digest['personal'], [])
        self.assertTrue(digest['possible'])

    def test_task_and_reply_same_anchor_are_one_card(self):
        m = msg(text='请提交报告', group=False)
        items, _ = local_analysis([m], {})
        digest = compose_digest(run(), [i | {'status': 'open'} for i in items], [m])
        self.assertEqual(len(digest['personal']), 1)
        self.assertEqual(set(digest['personal'][0]['kinds']), {'task', 'reply'})

    def test_unreferenced_raw_text_not_embedded_and_script_is_safe(self):
        raw = msg('raw', text='UNREFERENCED_SECRET')
        cited = msg('a', text='</script><img src=x onerror=alert(1)>')
        story = {'chat_id': cited['chat_id'], 'chat_name': '测试', 'text': '讨论了一个问题。', 'evidence_ids': ['a'], 'story_status': 'ai'}
        digest = compose_digest(run([story]), [], [raw, cited])
        page = render_html(digest)
        self.assertNotIn('UNREFERENCED_SECRET', page)
        self.assertNotIn('</script><img', page)
        match = re.search(r'id="chat-0">(.*?)</script>', page, re.S)
        loaded = json.loads(match.group(1))
        self.assertEqual(loaded['evidence']['a']['content'], cited['content'])
        self.assertNotIn('<article', page.split('__PAYLOAD__')[0].split('<script>')[0])

    def test_brief_is_bounded_even_with_many_chats(self):
        messages, items, stories = [], [], []
        for i in range(150):
            m = msg(str(i), text='请提交报告' * 100, chat_id=f'{i}@chatroom', chat_name='长名字' * 60, mentions_self=True)
            found, _ = local_analysis([m], {})
            messages.append(m)
            items.extend(x | {'status': 'open'} for x in found)
            stories.append({'chat_id': m['chat_id'], 'chat_name': m['chat_name'], 'text': '故事内容' * 90, 'evidence_ids': [m['id']], 'story_status': 'ai'})
        brief = '\n'.join(brief_lines(compose_digest(run(stories), items, messages)))
        self.assertLess(len(brief), 4000)
        self.assertEqual(brief.count('### '), 5)

    def test_export_raw_is_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root/'test.sqlite')
            m = msg()
            store.save(META, [m], [], [], {'mode': 'local', 'warnings': []})
            with patch('workbench.reports.DATA', root), patch('workbench.reports.read_config', return_value={}):
                first = build_report(store, 'test')
                second = build_report(store, 'test', include_raw=True)
            self.assertNotIn('messages.json', first['files'])
            self.assertIn('messages.json', second['files'])
            self.assertTrue((root/'reports'/first['folder']/'report.html').exists())


if __name__ == '__main__':
    unittest.main()
