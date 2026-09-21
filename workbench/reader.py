"""Read all local personal/group conversations using the pinned upstream decoder."""
import hashlib
import json
import re
import shutil
import sqlite3
import struct
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.read_group import dependencies, readable_content
from .config import DATA
from .relevance import reference_metadata

TZ = timezone(timedelta(hours=8))


def checksum(data, state=(0, 0), endian='<'):
    words = struct.unpack(endian + str(len(data) // 4) + 'I', data)
    a, b = state
    for i in range(0, len(words), 2):
        a = (a + words[i] + b) & 0xffffffff
        b = (b + words[i + 1] + a) & 0xffffffff
    return a, b


def merge_committed_wal(dst, wal_path, key, decrypt_page):
    """Validate WAL checksums; apply only frames ending in a valid commit."""
    raw = Path(wal_path).read_bytes()
    if len(raw) < 32:
        return 0
    magic, version, size = struct.unpack('>III', raw[:12])
    if magic not in (0x377f0682, 0x377f0683) or size != 4096:
        raise RuntimeError('不支持的 WAL 格式，未合并')
    endian = '<' if magic == 0x377f0682 else '>'
    state = checksum(raw[:24], endian=endian)
    if state != struct.unpack('>II', raw[24:32]):
        raise RuntimeError('WAL 头校验失败')
    frames, commit, pages = [], 0, 0
    for offset in range(32, len(raw) - 4119, 4120):
        header, page = raw[offset:offset + 24], raw[offset + 24:offset + 4120]
        if header[8:16] != raw[16:24]:
            break  # Old WAL generation after a reset.
        new_state = checksum(header[:8] + page, state, endian)
        if new_state != struct.unpack('>II', header[16:24]):
            break  # Incomplete tail: retain only prior committed transactions.
        state = new_state
        pgno, dbsize = struct.unpack('>II', header[:8])
        if not pgno:
            raise RuntimeError('WAL 页号无效')
        frames.append((pgno, page))
        if dbsize:
            commit, pages = len(frames), dbsize
    if commit:
        with open(dst, 'r+b') as target:
            for pgno, page in frames[:commit]:
                target.seek((pgno - 1) * 4096)
                target.write(decrypt_page(key, page, pgno))
            target.truncate(pages * 4096)
    return commit


def read_all(cfg, hours, progress=lambda _: None, end=None):
    database, parser = dependencies()
    if not cfg.get('account'):
        raise ValueError('请先在设置中选择本人微信账号')
    source_root = Path(cfg['db_dir']) / cfg['account'] / 'db_storage'
    if not source_root.is_dir():
        raise ValueError('账号目录下没有 db_storage')
    selected = [p for p in source_root.rglob('*.db') if
                p.relative_to(source_root).as_posix() in ('contact/contact.db', 'session/session.db')
                or re.fullmatch(r'message/message_\d+\.db', p.relative_to(source_root).as_posix())]
    if not any(p.name == 'contact.db' for p in selected) or not any(re.fullmatch(r'message_\d+\.db', p.name) for p in selected):
        raise RuntimeError('联系人库或消息库缺失，当前数据库结构不受支持')
    end = end or datetime.now(TZ).replace(microsecond=0)
    start = end - timedelta(hours=hours)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    DATA.mkdir(exist_ok=True)
    private = DATA / 'private'
    private.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='read-', dir=private) as work:
        snapshot = Path(work) / cfg['account'] / 'db_storage'
        progress('正在创建稳定的数据库副本（原库只读）')
        def signature():
            result = {}
            for path in selected:
                for candidate in (path, Path(str(path) + '-wal')):
                    if candidate.exists():
                        stat = candidate.stat()
                        result[str(candidate)] = (stat.st_size, stat.st_mtime_ns)
            return result
        for attempt in range(4):
            before = signature()
            for path in selected:
                target = snapshot / path.relative_to(source_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                wal = Path(str(path) + '-wal')
                target_wal = Path(str(target) + '-wal')
                target_wal.unlink(missing_ok=True)
                if wal.exists():
                    shutil.copyfile(wal, target_wal)
            if before == signature():
                break
        else:
            raise RuntimeError('微信正在频繁改写数据库，无法取得稳定副本，请稍后重试')

        class LocalDB(database.WeChatDB):
            def _save_keys(self):
                pass

            def _merge_wal(self, dst, path, key, from_frame):
                return merge_committed_wal(dst, path, key, database._decrypt_page)

        progress('正在读取已登录微信的解密配置，密钥不落盘')
        db = LocalDB(db_dir=work, account=cfg['account'], workdir=str(Path(work) / 'decoded'))
        if db.unkeyed:
            raise RuntimeError('部分数据库没有取得密钥。请确认所选账号已登录；当前微信版本也可能需要适配。')
        names, chat_ids, checks = {}, {}, []
        for rel, _, _ in db._db_files:
            if Path(rel).name not in ('contact.db', 'session.db'):
                continue
            conn = db._open(rel)
            try:
                if [r[0] for r in conn.execute('PRAGMA quick_check')] != ['ok']:
                    raise RuntimeError('联系人或会话数据库完整性检查失败')
                if Path(rel).name == 'contact.db':
                    for user, nick, remark in conn.execute('SELECT username, nick_name, remark FROM contact'):
                        if user:
                            names[user] = remark or nick or user
                            chat_ids[hashlib.md5(user.encode()).hexdigest()] = user
                else:
                    for row in conn.execute('SELECT username FROM SessionTable'):
                        if row[0]:
                            chat_ids[hashlib.md5(row[0].encode()).hexdigest()] = row[0]
            finally:
                conn.close()
        self_id = db.wxid
        if self_id not in names:
            raise RuntimeError('无法在联系人库中确认本人身份，停止分析以免错误判断谁没有回复')
        wanted = ['local_id', 'local_type', 'server_id', 'real_sender_id', 'create_time',
                  'message_content', 'source', 'compress_content', 'packed_info_data', 'sort_seq']
        messages, errors, unknown_tables, duplicates, seen = [], [], [], 0, set()
        message_dbs = db._message_dbs()
        for index, rel in enumerate(message_dbs):
            progress(f'正在解析消息分片 {index + 1}/{len(message_dbs)}')
            conn = db._open(rel)
            try:
                integrity = [r[0] for r in conn.execute('PRAGMA quick_check')]
                if integrity != ['ok']:
                    raise RuntimeError(f'{rel} 完整性检查失败')
                checks.append({'database': rel, 'quick_check': 'ok'})
                smap, smap_status = parser._sender_map(conn)
                # Supplement contact mapping with usernames present in this shard.
                for value in smap.values():
                    user = parser._identity_username(value)
                    if user:
                        chat_ids.setdefault(hashlib.md5(user.encode()).hexdigest(), user)
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'") if re.fullmatch(r'Msg_[a-fA-F0-9]{32}', r[0])]
                for table in tables:
                    user = chat_ids.get(table[4:])
                    if not user:
                        unknown_tables.append({'database': rel, 'table': table})
                        continue
                    if user.startswith('gh_') or user in ('weixin', 'filehelper', 'fmessage', 'medianote', 'floatbottle', 'newsapp'):
                        continue
                    cols = parser.table_columns(conn, table)
                    if not {'create_time', 'local_id', 'local_type'} <= set(cols):
                        raise RuntimeError(f'{rel} 消息表结构不支持')
                    selected_cols = [c for c in wanted if c in cols]
                    stamp = '(CASE WHEN create_time > 10000000000 THEN CAST(create_time / 1000 AS INTEGER) ELSE create_time END)'
                    sql = f'SELECT {", ".join(selected_cols)} FROM {table} WHERE {stamp} >= ? AND {stamp} < ? ORDER BY create_time, local_id'
                    for raw in conn.execute(sql, (start_ts, end_ts)):
                        row = {k: None for k in wanted} | dict(raw)
                        row['_sender_username'] = parser._identity_username(smap.get(parser._sender_id(row['real_sender_id'])))
                        row['_sender_status'] = 'resolved' if row['_sender_username'] else smap_status if smap_status != 'resolved' else 'unmapped_sender_id'
                        group = user.endswith('@chatroom')
                        identity, strip = parser.resolve_sender(row, group, user, names.get(user, user), names, self_id)
                        ts = parser._normalize_timestamp(row['create_time'])
                        server_id = parser._json_message_id(row['server_id'])
                        key = [cfg['account'], user, str(server_id)] if server_id not in (None, 0, '0', '') else [cfg['account'], user, rel, table, str(row['local_id'])]
                        uid = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32]
                        if uid in seen:
                            duplicates += 1
                            continue
                        seen.add(uid)
                        try:
                            content = parser.parse_content(row['local_type'], row['message_content'], row['compress_content'], group_prefix_strip=strip)
                            content = readable_content(content, parser.low_type(row['local_type']))
                            transcript = parser._voice_transcript(row) if parser.low_type(row['local_type']) == 34 else ''
                        except Exception as exc:
                            content, transcript = '[消息解析失败]', ''
                            errors.append({'id': uid, 'error_type': type(exc).__name__})
                        references = reference_metadata(row, parser, self_id)
                        messages.append({'id': uid, 'chat_id': user, 'chat_name': names.get(user, user), 'is_group': group, **references,
                                         'timestamp': ts, 'time': datetime.fromtimestamp(ts, TZ).isoformat(), **identity,
                                         'type': parser.TYPE_LABEL.get(parser.low_type(row['local_type']), '其他'),
                                         'content': content, 'transcript': transcript or None, 'source_db': rel,
                                         'source_table': table, 'local_id': row['local_id'], 'server_id': server_id})
            finally:
                conn.close()
        own_server_ids = {(m['chat_id'], str(m['server_id'])) for m in messages
                          if m['is_self'] is True and m['server_id'] not in (None, 0, '0', '')}
        for m in messages:
            if m.get('quoted_server_id') and (m['chat_id'], m['quoted_server_id']) in own_server_ids:
                m['quotes_self'] = True
        messages.sort(key=lambda m: (m['timestamp'], m['id']))
        meta = {'account': cfg['account'], 'self_id': self_id, 'self_name': cfg.get('self_name') or names[self_id],
                'start': start.isoformat(), 'end_exclusive': end.isoformat(), 'message_count': len(messages),
                'chat_count': len({m['chat_id'] for m in messages}), 'database_checks': checks,
                'duplicates_removed': duplicates, 'parse_errors': errors, 'unmapped_tables': unknown_tables,
                'unknown_senders': sum(m['sender_status'] != 'resolved' and m['sender_status'] != 'system' for m in messages),
                'earliest': messages[0]['time'] if messages else None, 'latest': messages[-1]['time'] if messages else None,
                'coverage_note': '仅本机已同步的普通群聊和私聊；不含公众号。完整性检查不代表同步完整。图片、音视频内容默认不识别。'}
        del db
    return meta, messages
