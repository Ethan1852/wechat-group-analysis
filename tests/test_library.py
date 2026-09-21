import tempfile
import unittest
from pathlib import Path

from workbench.store import Store
from test_story_report import msg, META


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.sqlite')
        self.messages = [msg('a', '谐音冷笑话', timestamp=100),
                         msg('b', '同名不同人', sender_username='other', timestamp=200),
                         msg('c', '讨论明天', sender_username='other', mentioned_user_ids=['friend'], timestamp=300, chat_id='second'),
                         msg('d', '引用回应', sender_username='other', quoted_sender_id='friend', timestamp=400)]
        self.store.save(META, self.messages, [], [], {})

    def test_favorite_is_idempotent_and_preserves_notes(self):
        self.store.favorite('test', 'a', note='好笑', tags='冷梗')
        self.store.favorite('test', 'a')
        result = self.store.favorites('test', '冷梗')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['note'], '好笑')
        self.store.unfavorite('test', 'a')
        self.assertEqual(self.store.favorites('test')['total'], 0)
        self.assertEqual(len(self.store.messages('test', ['a'])), 1)

    def test_favorites_account_isolation(self):
        self.store.favorite('test', 'a')
        self.assertEqual(self.store.favorites('another')['total'], 0)
        with self.assertRaises(ValueError):
            self.store.favorite('another', 'a')

    def test_same_name_stays_separate(self):
        users = self.store.people('test', '小王')['people']
        self.assertEqual({u['id'] for u in users}, {'friend', 'other'})
        self.assertEqual(self.store.people('another')['total'], 0)

    def test_authored_mention_quote_across_chats(self):
        for scope, ids in [('all', {'a', 'c', 'd'}), ('authored', {'a'}), ('mentioned', {'c'}), ('quoted', {'d'})]:
            result = self.store.person_messages('test', 'friend', scope=scope)
            self.assertEqual({m['id'] for m in result['messages']}, ids)
        self.assertEqual(self.store.person_messages('another', 'friend')['total'], 0)

    def test_time_keyword_pagination_and_literal_query(self):
        result = self.store.person_messages('test', 'friend', start=100, end=400, limit=1)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['messages'][0]['id'], 'c')
        self.assertEqual(self.store.person_messages('test', 'friend', query='明天')['total'], 1)
        self.assertEqual(self.store.person_messages('test', 'friend', chat_id='second')['total'], 1)
        self.assertEqual(self.store.person_messages('test', 'friend', query="' OR 1=1 --")['total'], 0)


if __name__ == '__main__':
    unittest.main()
