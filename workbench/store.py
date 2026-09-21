import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA


class Store:
    def __init__(self, path=None):
        self.path = Path(path or DATA / 'workbench.sqlite')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS messages(account TEXT, id TEXT, timestamp INTEGER, data TEXT, PRIMARY KEY(account,id));
            CREATE TABLE IF NOT EXISTS items(account TEXT, id TEXT, status TEXT NOT NULL DEFAULT 'open', note TEXT NOT NULL DEFAULT '', data TEXT, updated TEXT, PRIMARY KEY(account,id));
            CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, account TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS messages_window ON messages(account,timestamp);
            CREATE TABLE IF NOT EXISTS favorites(account TEXT, message_id TEXT, title TEXT, note TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '', created TEXT, PRIMARY KEY(account,message_id));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, meta, messages, items, summaries, analysis):
        account, now = meta['account'], datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO messages VALUES(?,?,?,?)', [(account, m['id'], m['timestamp'], json.dumps(m, ensure_ascii=False)) for m in messages])
            for item in items:
                db.execute('''INSERT INTO items(account,id,data,updated) VALUES(?,?,?,?)
                    ON CONFLICT(account,id) DO UPDATE SET data=excluded.data,updated=excluded.updated''',
                    (account, item['id'], json.dumps(item, ensure_ascii=False), now))
            run = {'meta': meta, 'summaries': summaries, 'analysis': analysis, 'created': now}
            cursor = db.execute('INSERT INTO runs(account,data) VALUES(?,?)', (account, json.dumps(run, ensure_ascii=False)))
            return cursor.lastrowid

    def latest(self, account):
        with self.connect() as db:
            row = db.execute('SELECT id,data FROM runs WHERE account=? ORDER BY id DESC LIMIT 1', (account,)).fetchone()
            return (json.loads(row['data']) | {'id': row['id']}) if row else None

    def latest_id(self, account):
        with self.connect() as db:
            return db.execute('SELECT MAX(id) FROM runs WHERE account=?', (account,)).fetchone()[0]

    def window_messages(self, account, start, end):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM messages WHERE account=? AND timestamp>=? AND timestamp<? ORDER BY timestamp,id', (account, start, end))]

    def items(self, account):
        with self.connect() as db:
            return [json.loads(r['data']) | {'status': r['status'], 'note': r['note']} for r in db.execute('SELECT * FROM items WHERE account=? ORDER BY updated DESC,id', (account,))]

    def set_status(self, account, uid, status, note=''):
        if status not in ('open', 'done', 'dismissed', 'snoozed'):
            raise ValueError('事项状态无效')
        with self.connect() as db:
            result = db.execute('UPDATE items SET status=?,note=? WHERE account=? AND id=?', (status, str(note)[:2000], account, uid))
            if result.rowcount != 1:
                raise ValueError('事项不存在')

    def messages(self, account, ids=None):
        with self.connect() as db:
            if ids is None:
                rows = db.execute('SELECT data FROM messages WHERE account=? ORDER BY timestamp,id', (account,))
                return [json.loads(r[0]) for r in rows]
            result = []
            for uid in ids:
                row = db.execute('SELECT data FROM messages WHERE account=? AND id=?', (account, uid)).fetchone()
                if row:
                    result.append(json.loads(row[0]))
            return result

    def context_for_pending(self, account, messages):
        # Retain original evidence of older open tasks when scanning a new day.
        known = {m['id']: m for m in messages}
        ids = []
        for item in self.items(account):
            if item['status'] in ('open', 'snoozed'):
                ids.extend(item['evidence_ids'])
        for m in self.messages(account, list(dict.fromkeys(ids))):
            known.setdefault(m['id'], m)
        return sorted(known.values(), key=lambda m: (m['timestamp'], m['id']))

    def favorite(self, account, message_id, title=None, note=None, tags=None):
        messages = self.messages(account, [message_id])
        if not messages:
            raise ValueError('当前账号没有这条消息，不能收藏其他账号的记录')
        m = messages[0]
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO favorites VALUES(?,?,?,?,?,?)',
                       (account, message_id, str(title or m['content'] or '收藏的消息')[:100], str(note or '')[:2000], str(tags or '')[:200], now))
            updates, values = [], []
            for key, value, limit in (('title', title, 100), ('note', note, 2000), ('tags', tags, 200)):
                if value is not None:
                    updates.append(key + '=?')
                    values.append(str(value)[:limit])
            if updates:
                db.execute('UPDATE favorites SET ' + ','.join(updates) + ' WHERE account=? AND message_id=?', values + [account, message_id])

    def unfavorite(self, account, message_id):
        with self.connect() as db:
            db.execute('DELETE FROM favorites WHERE account=? AND message_id=?', (account, message_id))

    def favorites(self, account, query='', offset=0, limit=20):
        where = 'f.account=?'
        params = [account]
        if query:
            where += " AND instr(lower(f.title||' '||f.note||' '||f.tags||' '||json_extract(m.data,'$.content')||' '||json_extract(m.data,'$.sender')||' '||json_extract(m.data,'$.chat_name')),lower(?))>0"
            params.append(query)
        with self.connect() as db:
            base = ' FROM favorites f JOIN messages m ON m.account=f.account AND m.id=f.message_id WHERE ' + where
            total = db.execute('SELECT COUNT(*)' + base, params).fetchone()[0]
            rows = db.execute('SELECT f.*,m.data' + base + ' ORDER BY f.created DESC,f.message_id LIMIT ? OFFSET ?', params + [limit, offset])
            result = []
            for row in rows:
                m = json.loads(row['data'])
                result.append({k: row[k] for k in ('message_id', 'title', 'note', 'tags', 'created')} | {'message': m | {'content': str(m['content'])[:1200], 'excerpt': len(str(m['content'])) > 1200}})
        return {'total': total, 'items': result, 'offset': offset, 'limit': limit}

    def people(self, account, query='', offset=0, limit=20):
        with self.connect() as db:
            rows = db.execute("""SELECT json_extract(data,'$.sender_username') AS uid,
                json_extract(data,'$.sender') AS name, COUNT(*) AS n,
                MAX(timestamp) AS recent FROM messages WHERE account=?
                AND json_extract(data,'$.sender_username') IS NOT NULL
                AND json_extract(data,'$.sender_status')='resolved'
                GROUP BY uid,name ORDER BY recent DESC""", (account,))
            users = {}
            for row in rows:
                if not row['uid']:
                    continue
                user = users.setdefault(row['uid'], {'id': row['uid'], 'names': [], 'message_count': 0, 'recent': row['recent']})
                if row['name'] and row['name'] not in user['names']:
                    user['names'].append(row['name'])
                user['message_count'] += row['n']
        latest = self.latest(account)
        if latest and latest['meta'].get('self_id') in users:
            users[latest['meta']['self_id']]['names'].append(latest['meta'].get('self_name', '本人'))
        filtered = [u for u in users.values() if not query or query.casefold() in (' '.join(u['names']) + ' ' + u['id']).casefold()]
        return {'total': len(filtered), 'people': filtered[offset:offset + limit], 'offset': offset, 'limit': limit}

    def person_messages(self, account, user_id, query='', scope='all', start=None, end=None, chat_id='', offset=0, limit=25):
        if scope not in ('all', 'authored', 'mentioned', 'quoted'):
            raise ValueError('搜索范围无效')
        source = "json_extract(m.data,'$.sender_username')=?"
        mentions = "EXISTS(SELECT 1 FROM json_each(m.data,'$.mentioned_user_ids') a WHERE a.value=?)"
        quotes = "json_extract(m.data,'$.quoted_sender_id')=?"
        latest = self.latest(account)
        self_id = (latest or {}).get('meta', {}).get('self_id')
        if user_id == self_id:
            quotes = '(' + quotes + " OR json_extract(m.data,'$.quotes_self')=1)"
        expressions = {'authored': source, 'mentioned': mentions, 'quoted': quotes}
        keys = ['authored', 'mentioned', 'quoted'] if scope == 'all' else [scope]
        where = 'm.account=? AND (' + ' OR '.join(expressions[k] for k in keys) + ')'
        params = [account] + [user_id] * len(keys)
        if query:
            where += " AND instr(lower(COALESCE(json_extract(m.data,'$.content'),'')||' '||COALESCE(json_extract(m.data,'$.transcript'),'')),lower(?))>0"
            params.append(query)
        if start is not None:
            where += ' AND m.timestamp>=?'
            params.append(start)
        if end is not None:
            where += ' AND m.timestamp<?'
            params.append(end)
        if chat_id:
            where += " AND json_extract(m.data,'$.chat_id')=?"
            params.append(chat_id)
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM messages m WHERE ' + where, params).fetchone()[0]
            rows = db.execute('SELECT m.data FROM messages m WHERE ' + where + ' ORDER BY m.timestamp DESC,m.id DESC LIMIT ? OFFSET ?', params + [limit, offset])
            result = []
            for row in rows:
                m = json.loads(row[0])
                relation = []
                if m.get('sender_username') == user_id:
                    relation.append('本人发言')
                if user_id in m.get('mentioned_user_ids', []):
                    relation.append('被 @')
                if m.get('quoted_sender_id') == user_id or (user_id == self_id and m.get('quotes_self')):
                    relation.append('被引用')
                result.append(m | {'content': str(m['content'])[:900], 'excerpt': len(str(m['content'])) > 900, 'relations': relation})
            groups = [{'id': r[0], 'name': r[1], 'count': r[2]} for r in db.execute("SELECT json_extract(m.data,'$.chat_id'),MAX(json_extract(m.data,'$.chat_name')),COUNT(*) FROM messages m WHERE " + where + ' GROUP BY 1 ORDER BY 3 DESC LIMIT 100', params)]
        # Associate tasks via actual evidence, not by matching a possibly duplicate name.
        items = self.items(account)
        refs = {uid for i in items for uid in i.get('evidence_ids', [])}
        evidence = {m['id']: m for m in self.messages(account, list(refs))}
        related = []
        for item in items:
            if any(evidence.get(uid, {}).get('sender_username') == user_id or user_id in evidence.get(uid, {}).get('mentioned_user_ids', []) or evidence.get(uid, {}).get('quoted_sender_id') == user_id for uid in item.get('evidence_ids', [])):
                related.append(item)
        return {'total': total, 'messages': result, 'chats': groups, 'related_items': related[:20],
                'related_items_total': len(related), 'offset': offset, 'limit': limit,
                'note': '只搜索本地已导入记录；关联事项是引用此用户的事项，不等于由此用户负责。旧记录缺少结构化 @/引用标识时可能检索不到。'}
