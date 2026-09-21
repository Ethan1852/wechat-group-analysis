"""One concise editorial model shared by HTML, Markdown and PNG."""
import re
from collections import Counter, defaultdict
from datetime import datetime

from .relevance import relevance, RELATION_LABELS


def short(value, limit=100):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def focus_items(items, limit=8):
    """Give different contacts/groups a place in the brief before repeat entries."""
    queues = defaultdict(list)
    for item in items:
        queues[item['chat_id']].append(item)
    result = []
    while len(result) < limit and any(queues.values()):
        for queue in queues.values():
            if queue and len(result) < limit:
                result.append(queue.pop(0))
    return result


def paragraph(value, limit=280):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if len(text) <= limit:
        return text
    ends = [m.end() for m in re.finditer(r'[。！？；]', text[:limit])]
    return text[:ends[-1]] if ends and ends[-1] >= limit // 2 else short(text, limit)


def compose_digest(latest, items, messages, cfg=None):
    cfg = cfg or {}
    meta = latest['meta']
    context = cfg | {'self_id': meta.get('self_id'), 'self_name': cfg.get('self_name') or meta.get('self_name', '')}
    start = datetime.fromisoformat(meta['start']).timestamp()
    end = datetime.fromisoformat(meta['end_exclusive']).timestamp()
    lookup = {m['id']: m for m in messages}
    current = [m for m in messages if start <= m['timestamp'] < end]
    groups = defaultdict(list)
    for m in current:
        groups[m['chat_id']].append(m)
    summaries = defaultdict(list)
    for summary in latest.get('summaries', []):
        summaries[summary['chat_id']].append(summary)
    compact_items = []
    # Avoid showing one request twice as both a task and a reply. Preserve every
    # backing item ID so users can still manage the underlying records.
    buckets = {}
    for item in items:
        if item['status'] not in ('open', 'snoozed'):
            continue
        m = lookup.get(item.get('anchor_id'))
        relation = relevance(m, context) if m else 'possible'
        if relation in ('possible', 'possible_quote') and item.get('relation') == 'context':
            relation = 'context'
        personal = relation in ('private', 'mention', 'quote', 'commitment', 'context')
        refs = list(dict.fromkeys(uid for uid in item.get('evidence_ids', []) if uid in lookup))
        row = {k: item.get(k, '') for k in ('id', 'chat_id', 'chat_name', 'sender', 'time', 'timestamp', 'kind', 'status', 'deadline', 'suggested_status')}
        row.update(title=short(item.get('title'), 65), reason=short(item.get('reason'), 120),
                   owner=item.get('owner', '未明确'), relation=relation,
                   relation_label=RELATION_LABELS.get(relation, '可能相关'), personal=personal,
                   historical=item.get('timestamp', end) < start, evidence_ids=refs,
                   item_ids=[item['id']], kinds=[item['kind']])
        raw_title = str(item.get('title', ''))
        media_only = bool(m and not m.get('transcript') and m.get('type') in ('语音', '图片', '视频', '动画表情'))
        transport = bool(re.search(r'<[/\w][^>]*>', raw_title)) or '未解析内容' in raw_title
        row['needs_content_review'] = media_only or transport
        if transport:
            row['title'] = f'核对{short(item.get("sender"), 18)}发来的消息，确认是否需要回应'
            row['reason'] = '这条消息的内部内容尚未解析，需回微信核对，不能确定具体任务。'
        elif media_only and (raw_title.startswith('[') or len(raw_title) < 8):
            row['title'] = f'查看{short(item.get("sender"), 18)}发来的{m["type"]}，确认是否需要回应'
            row['reason'] = '消息内部内容尚未识别，需人工核对。'
        key = (item['chat_id'], item.get('anchor_id') or item['id'])
        if key in buckets:
            existing = buckets[key]
            existing['item_ids'].append(item['id'])
            existing['kinds'] = list(dict.fromkeys(existing['kinds'] + [item['kind']]))
            existing['evidence_ids'] = list(dict.fromkeys(existing['evidence_ids'] + refs))
            if item['kind'] == 'reply':
                existing['kind'] = 'reply'
            continue
        buckets[key] = row
    compact_items = list(buckets.values())
    compact_items.sort(key=lambda x: (not x['personal'], x['status'] == 'snoozed', x.get('needs_content_review', False), x.get('suggested_status') == 'completed', -float(x.get('timestamp') or 0)))
    by_chat = defaultdict(list)
    for item in compact_items:
        by_chat[item['chat_id']].append(item)
    chats = []
    for chat_id in set(groups) | set(by_chat):
        msgs, parts = groups[chat_id], summaries.get(chat_id, [])
        name = (msgs[0]['chat_name'] if msgs else by_chat[chat_id][0]['chat_name'])
        group = msgs[0].get('is_group', chat_id.endswith('@chatroom')) if msgs else chat_id.endswith('@chatroom')
        if len(parts) == 1:
            story = parts[0]
        elif parts:
            # Compatibility for older runs: never silently concatenate 100 snippets.
            useful = [p for p in parts if p.get('evidence_ids')]
            story = (useful[0] if useful else parts[0]) | {'story_status': 'legacy', 'topics': [], 'conclusions': []}
        else:
            story = {'text': '此会话只有历史未完成事项，本时间窗没有新消息。' if not msgs else '尚未生成故事摘要。', 'story_status': 'local'}
        evidence_ids = set(story.get('evidence_ids', []))
        topics, conclusions = [], []
        for t in story.get('topics', [])[:3]:
            valid = [uid for uid in t.get('evidence_ids', []) if uid in lookup and lookup[uid]['chat_id'] == chat_id]
            if valid:
                topics.append({'title': short(t.get('title'), 36), 'story': short(t.get('story'), 220), 'evidence_ids': valid})
                evidence_ids.update(valid)
        for c in story.get('conclusions', [])[:3]:
            valid = [uid for uid in c.get('evidence_ids', []) if uid in lookup and lookup[uid]['chat_id'] == chat_id]
            if valid:
                conclusions.append({'text': short(c.get('text'), 140), 'evidence_ids': valid})
                evidence_ids.update(valid)
        for item in by_chat[chat_id]:
            evidence_ids.update(item['evidence_ids'])
        evidence = {}
        for uid in evidence_ids:
            if uid in lookup and lookup[uid]['chat_id'] == chat_id:
                m = lookup[uid]
                evidence[uid] = {k: m.get(k) for k in ('id', 'time', 'sender', 'is_self', 'source_db', 'source_table', 'local_id')}
                evidence[uid]['content'] = str(m['content'])[:2000]
                evidence[uid]['excerpt'] = len(str(m['content'])) > 2000
        chats.append({'id': chat_id, 'name': name, 'is_group': group, 'message_count': len(msgs),
                      'story': short(story.get('text'), 420), 'story_status': story.get('story_status', 'legacy'),
                      'topics': topics, 'conclusions': conclusions, 'items': by_chat[chat_id],
                      'evidence_ids': [uid for uid in story.get('evidence_ids', []) if uid in evidence], 'evidence': evidence,
                      'personal_count': sum(i['personal'] for i in by_chat[chat_id]), 'fragment_count': len(parts)})
    chats.sort(key=lambda c: (-c['personal_count'], not bool(c['topics']), -c['message_count'], c['id']))
    personal = [i for i in compact_items if i['personal']]
    possible = [i for i in compact_items if not i['personal']]
    return {'version': 2, 'meta': meta, 'analysis': latest['analysis'], 'personal': personal, 'possible': possible, 'focus': focus_items(personal),
            'chats': chats, 'stats': {'messages': len(current), 'groups': sum(c['is_group'] for c in chats),
                                    'personal': len(personal), 'possible': len(possible)},
            'highlights': [{'chat_id': c['id'], 'name': c['name'], 'text': short(c['story'], 100)}
                           for c in chats if c['is_group'] and c['story_status'] not in ('local',)] [:5]}


def brief_lines(digest):
    """A bounded executive brief. Full per-chat stories are browsable in HTML."""
    lines = ['# 晚笺 · 微信重点复盘', '', f'{digest["meta"]["start"][:16]} — {digest["meta"]["end_exclusive"][:16]}',
             f'{digest["stats"]["groups"]} 个群 · {digest["stats"]["messages"]} 条消息 · {len(digest["personal"])} 项与我相关', '', '## 先处理这些事', '']
    chosen = digest['focus']
    if not chosen:
        lines.append('未识别到明确与我相关的未处理事项；可能相关事项在 HTML 中单独核实。')
    for i, item in enumerate(chosen, 1):
        lines += [f'{i}. {item["title"]}', f'   {short(item["chat_name"], 28)} · {item["relation_label"]}' + (' · 历史待办' if item['historical'] else ''),
                  '   ' + short(item['reason'], 80)]
    if len(digest['personal']) > len(chosen):
        lines.append(f'另有 {len(digest["personal"]) - len(chosen)} 项与我相关事项，见 HTML 的“与我相关”。')
    lines += ['', '## 群里发生了什么', '']
    selected = [c for c in digest['chats'] if c['is_group'] and c['story_status'] != 'local'][:5]
    if not selected:
        lines.append('尚未生成群聊故事，请在工作台运行 AI 整理。')
    for chat in selected:
        lines += ['### ' + short(chat['name'], 32), paragraph(chat['story'])]
        if chat['conclusions']:
            lines.append('结论 / 未决：' + short(chat['conclusions'][0]['text'], 90))
        if chat['story_status'] in ('partial', 'legacy'):
            lines.append('注：此群摘要尚不完整，详见网页。')
        lines.append('')
    lines += ['## 阅读说明', f'{len(digest["possible"])} 项仅可能相关的事项未混入重点清单，详见 HTML。',
              'PNG 和本摘要只展示重点；HTML 按群查看全部主题、故事线、结论和待办。',
              '原文依据点击查看。待回复属于推断，不等于微信未读；仅覆盖本机已同步记录。']
    if digest['analysis'].get('warnings'):
        lines.append(f'有 {len(digest["analysis"]["warnings"])} 条分析提示，详见 HTML 的数据范围。')
    return lines
