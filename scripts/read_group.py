"""Read one authorized Weixin group locally; export only a bounded time window.

Adapted from the original read_group_24h.py workflow. Third-party
database and parsing implementations remain in vendor with their licenses.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import types
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def dependencies():
    # Load only database/media definitions; package __init__ imports UI automation.
    pkg = types.ModuleType('wechatauto')
    pkg.__path__ = [str(ROOT / 'vendor/wechatauto-replica/wechatauto')]
    sys.modules['wechatauto'] = pkg
    database = load_module('wechatauto.db', Path(pkg.__path__[0]) / 'db.py')
    media = load_module('wechatauto.media', Path(pkg.__path__[0]) / 'media.py')
    pkg.WeChatDB = database.WeChatDB
    pkg.MediaDownloader = media.MediaDownloader
    exporter = load_module('exporter_core', ROOT / 'vendor/wechat-chat-export/exporter_core.py')
    return database, exporter


def readable_content(content, low_type):
    # Some quoted media fall through the upstream parser as transport XML.
    # Keep a readable marker rather than CDN keys, tickets or opaque identifiers.
    if low_type == 42:
        try:
            name = ET.fromstring(content).get('nickname', '')
        except ET.ParseError:
            name = ''
        return '[联系人名片]' + (' ' + name if name else '')

    def replace_xml(match):
        xml = match.group(0)
        if '<videomsg' in xml:
            return '[视频；未解析内容]'
        if '<img' in xml:
            return '[图片；未解析内容]'
        return '[结构化消息；未解析内容]'

    content = re.sub(r'(?:<\?xml[^>]*>\s*)?<msg\b[^>]*(?:/>|>[\s\S]*?</msg>)', replace_xml, content)
    content = re.sub(r'(?i)((?:[\w]*aeskey|antispamticket)\s*=\s*[\"\x27])[^\"\x27]*', r'\1[已省略]', content)
    return content


def run(args):
    database, parser = dependencies()

    class ScopedDB(database.WeChatDB):
        def _collect_db_files(self):
            return [item for item in super()._collect_db_files()
                    if item[0].replace('\\', '/') == 'contact/contact.db'
                    or re.fullmatch(r'message/message_\d+\.db', item[0].replace('\\', '/'))]

        def _save_keys(self):
            pass  # Keep keys in memory for this process only.

    supplied_end = datetime.fromisoformat(args.end) if args.end else datetime.now(TZ)
    if supplied_end.tzinfo is None:
        raise ValueError('--end 必须包含时区，例如 2026-09-15T11:00:00+08:00')
    end = supplied_end.astimezone(TZ).replace(microsecond=0)
    start = end - timedelta(hours=args.hours)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    print(f'读取窗口：{start.isoformat()} 至 {end.isoformat()}', flush=True)
    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    group_tag = hashlib.sha256(args.group.encode()).hexdigest()[:8]
    out = Path(tempfile.mkdtemp(prefix=end.strftime('%Y%m%d-%H%M%S') + '-' + group_tag + '-', dir=output_root))
    private_root = output_root / '.private'
    private_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='read-', dir=private_root) as work:
        print('正在读取当前微信进程中的数据库解密配置…', flush=True)
        db = ScopedDB(db_dir=args.db_dir, account=args.account, workdir=work)
        if db.unkeyed:
            raise RuntimeError('所需数据库未全部取得可用密钥：' + ', '.join(db.unkeyed))
        print(f'已验证 {len(db._keys)} 个所需数据库的密钥。', flush=True)
        matches = [x for x in db.search_contact(args.group)
                   if x['username'].endswith('@chatroom')
                   and args.group in (x.get('remark'), x.get('nick_name'))]
        if args.group_id:
            matches = [x for x in matches if x['username'] == args.group_id]
        unique = {x['username']: x for x in matches}
        if len(unique) != 1:
            raise RuntimeError(f'准确匹配到 {len(unique)} 个群，无法唯一确定目标。')
        target = next(iter(unique.values()))
        username = target['username']
        table = 'Msg_' + hashlib.md5(username.encode()).hexdigest()
        print(f'已准确匹配群名：{args.group}', flush=True)
        nicks = db._nickname_index()
        rows, checks = [], []
        wanted = ['local_id', 'local_type', 'server_id', 'real_sender_id',
                  'create_time', 'message_content', 'source', 'compress_content',
                  'packed_info_data', 'sort_seq']
        for rel in db._message_dbs():
            print(f'正在读取消息分片：{rel}', flush=True)
            conn = db._open(rel)
            try:
                integrity = [r[0] for r in conn.execute('PRAGMA quick_check')]
                if integrity != ['ok']:
                    raise RuntimeError(f'{rel} 完整性检查未通过：{integrity[:3]}')
                present = bool(conn.execute('SELECT 1 FROM sqlite_master WHERE name=? AND type=\'table\'', (table,)).fetchone())
                check = {'source_db': rel, 'quick_check': integrity, 'group_present': present}
                checks.append(check)
                if not present:
                    continue
                cols = parser.table_columns(conn, table)
                if 'create_time' not in cols:
                    raise RuntimeError('消息表缺少时间字段')
                # Support both seconds and milliseconds without inspecting other chats.
                stamp = '(CASE WHEN create_time > 10000000000 THEN CAST(create_time / 1000 AS INTEGER) ELSE create_time END)'
                bounds = conn.execute(f'SELECT MIN({stamp}), MAX({stamp}), COUNT(*) FROM {table}').fetchone()
                check.update(earliest_timestamp=bounds[0], latest_timestamp=bounds[1], group_total_count=bounds[2])
                selected = [c for c in wanted if c in cols]
                smap, smap_status = parser._sender_map(conn)
                result = conn.execute(f'SELECT {", ".join(selected)} FROM {table} WHERE {stamp} >= ? AND {stamp} < ? ORDER BY create_time, local_id', (start_ts, end_ts))
                count = 0
                for raw in result:
                    row = dict(raw)
                    for c in wanted:
                        row.setdefault(c, None)
                    row['_db_rel'] = rel
                    sid = parser._sender_id(row['real_sender_id'])
                    su = parser._identity_username(smap.get(sid))
                    row['_sender_username'] = su
                    row['_sender_status'] = 'resolved' if su else smap_status if smap_status != 'resolved' else 'unmapped_sender_id'
                    rows.append(row)
                    count += 1
                check['window_count'] = count
            finally:
                conn.close()
        rows.sort(key=lambda r: (parser._normalize_timestamp(r['create_time']), r.get('sort_seq') or 0, r.get('local_id') or 0))
        messages, seen, duplicates = [], set(), 0
        for row in rows:
            ts = parser._normalize_timestamp(row['create_time'])
            assert start_ts <= ts < end_ts
            identity, strip_prefix = parser.resolve_sender(row, True, username, args.group, nicks, db.wxid)
            content = parser.parse_content(row['local_type'], row['message_content'], row['compress_content'], group_prefix_strip=strip_prefix)
            server_id = row.get('server_id')
            dedup = (str(server_id), ts, identity['sender_username'], row['local_type'], content) if server_id not in (None, 0, '0', '') else (row['_db_rel'], row['local_id'])
            if dedup in seen:
                duplicates += 1
                continue
            seen.add(dedup)
            low_type = parser.low_type(row['local_type'])
            content = readable_content(content, low_type)
            transcript = parser._voice_transcript(row) if low_type == 34 else ''
            messages.append({'id': len(messages) + 1, 'time': datetime.fromtimestamp(ts, TZ).isoformat(),
                             'timestamp': ts, **identity, 'type': '联系人名片' if low_type == 42 else parser.TYPE_LABEL.get(low_type, str(low_type)),
                             'type_code': row['local_type'], 'content': content,
                             'transcript': transcript or None, 'source_db': row['_db_rel'],
                             'local_id': row['local_id'], 'server_id': parser._json_message_id(server_id)})
        report = {'group': args.group, 'start': start.isoformat(), 'end_exclusive': end.isoformat(),
                  'hours': args.hours, 'self_name': args.self_name,
                  'message_count': len(messages), 'duplicates_removed': duplicates,
                  'sender_count': len({m['sender_username'] for m in messages if m['sender_username']}),
                  'type_counts': dict(Counter(m['type'] for m in messages)),
                  'sender_status_counts': dict(Counter(m['sender_status'] for m in messages)),
                  'database_checks': checks, 'media_note': '图片、音视频只保留消息标记；可用的微信语音转写保留。',
                  'messages': messages}
        out.mkdir(parents=True, exist_ok=True)
        (out / 'messages.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        text = [f'# {args.group}', f'时间：{start.isoformat()} 至 {end.isoformat()}（不含结束时刻）', f'共 {len(messages)} 条消息。', '']
        for m in messages:
            text += [f'[{m["id"]:04d}] {m["time"]} | {m["sender"]} | {m["type"]}', m['content']]
            if m['transcript']:
                text += ['微信语音转写：' + m['transcript']]
            text.append('')
        (out / 'messages.txt').write_text('\n'.join(text), encoding='utf-8')
        del db
        gc.collect()
    print(json.dumps({k: v for k, v in report.items() if k != 'messages'}, ensure_ascii=False, indent=2), flush=True)
    print('导出完成：' + str(out), flush=True)
    print('本次解密临时目录已清理。', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    settings_path = ROOT / 'settings.local.json'
    settings = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else {}
    p = argparse.ArgumentParser()
    p.add_argument('--group', required=True)
    p.add_argument('--group-id', help='同名群消歧；仍要求该群准确匹配 --group')
    p.add_argument('--db-dir', default=settings.get('db_dir'))
    p.add_argument('--account', default=settings.get('account'))
    p.add_argument('--self-name', default=settings.get('self_name'))
    p.add_argument('--hours', type=float, default=24)
    p.add_argument('--output-root', default=settings.get('output_root', str(Path.cwd() / 'wechat-reports')))
    p.add_argument('--end', help='ISO 8601 timestamp with explicit timezone')
    args = p.parse_args()
    if not math.isfinite(args.hours) or args.hours <= 0:
        p.error('--hours 必须是大于 0 的有限数值')
    if not args.group.strip():
        p.error('--group 不能为空')
    if os.name != 'nt':
        p.error('当前打包的读取器仅支持 Windows 微信 4.x；不能直接用于 macOS')
    if not args.account:
        p.error('请明确 --account 或在 settings.local.json 中配置；不要自动猜测多个账号')
    run(args)
