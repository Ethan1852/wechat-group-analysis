import json
import zipfile
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from .config import DATA, ROOT, read_config
from .reader import TZ
from .digest import compose_digest, brief_lines


def json_script(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')


def render_html(digest):
    payloads, catalog = [], []
    keys = {c['id']: f'chat-{i}' for i, c in enumerate(digest['chats'])}
    def item(row):
        return row | {'chat_key': keys[row['chat_id']]}
    for c in digest['chats']:
        key = keys[c['id']]
        detail = c | {'items': [item(x) for x in c['items']]}
        payloads.append(f'<script type="application/json" id="{key}">{json_script(detail)}</script>')
        catalog.append({'key': key, 'name': c['name'], 'is_group': c['is_group'], 'message_count': c['message_count'],
                        'personal_count': c['personal_count'], 'topic_count': len(c['topics']), 'teaser': c['story'][:85]})
    index = {k: digest[k] for k in ('meta', 'analysis', 'stats')}
    index.update(chats=catalog, personal=[item(x) for x in digest['personal']], possible=[item(x) for x in digest['possible']], focus=[item(x) for x in digest['focus']],
                 highlights=[x | {'key': keys[x['chat_id']]} for x in digest['highlights']])
    payloads.insert(0, f'<script type="application/json" id="report-index">{json_script(index)}</script>')
    return (ROOT / 'workbench/static/report.html').read_text('utf-8').replace('__PAYLOAD__', '\n'.join(payloads))


def prepare_digest(store, account):
    latest = store.latest(account)
    if not latest:
        raise ValueError('请先成功读取一次微信记录')
    items = store.items(account)
    meta = latest['meta']
    start, end = (datetime.fromisoformat(meta[k]).timestamp() for k in ('start', 'end_exclusive'))
    messages = store.window_messages(account, start, end)
    known = {m['id']: m for m in messages}
    refs = {uid for item in items if item['status'] in ('open', 'snoozed') for uid in item.get('evidence_ids', [])}
    refs.update(item.get('anchor_id') for item in items if item.get('anchor_id'))
    for m in store.messages(account, list(refs - set(known))):
        known[m['id']] = m
    return compose_digest(latest, items, list(known.values()), read_config())


def build_report(store, account, include_raw=False):
    digest = prepare_digest(store, account)
    out = DATA / 'reports' / datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f')
    out.mkdir(parents=True)
    lines = brief_lines(digest)
    (out / 'summary.md').write_text('\n'.join(lines), 'utf-8')
    (out / 'report.html').write_text(render_html(digest), 'utf-8')
    (out / 'report-data.json').write_text(json.dumps(digest, ensure_ascii=False, indent=2), 'utf-8')
    files = ['report.html', 'summary.md'] + render_png(lines, out) + ['report-data.json']
    if include_raw:
        messages = store.messages(account)
        (out / 'messages.json').write_text(json.dumps({'scope': '该账号历次本地已导入消息，非仅本报告时间窗', 'messages': messages}, ensure_ascii=False, indent=2), 'utf-8')
        (out / 'messages.txt').write_text('\n\n'.join(f'[{m["id"]}] {m["time"]} {m["chat_name"]} / {m["sender"]}\n{m["content"]}' for m in messages), 'utf-8')
        (out / 'tasks.json').write_text(json.dumps(store.items(account), ensure_ascii=False, indent=2), 'utf-8')
        files += ['messages.json', 'messages.txt', 'tasks.json']
    with zipfile.ZipFile(out / 'evening-review.zip', 'w', zipfile.ZIP_DEFLATED) as z:
        for name in files:
            z.write(out / name, name)
    return {'folder': out.name, 'files': files + ['evening-review.zip'], 'brief_items': min(8, len(digest['personal'])), 'groups': digest['stats']['groups']}


def render_png(lines, out):
    font_path = Path('C:/Windows/Fonts/msyh.ttc')
    if not font_path.exists():
        raise RuntimeError('未找到微软雅黑字体，无法生成中文 PNG')
    font = ImageFont.truetype(str(font_path), 24)
    title_font = ImageFont.truetype(str(font_path), 38)
    wrapped = []
    physical_lines = [part for line in lines for part in (line.splitlines() or [''])]
    for line in physical_lines:
        f = title_font if line.startswith('#') else font
        value = line.lstrip('# ').replace('\t', '    ')
        current = ''
        for char in value:
            if f.getlength(current + char) > 1072:
                wrapped.append((current, f))
                current = char
            else:
                current += char
        wrapped.append((current, f))
    pages, batch, height = [], [], 100
    def save(content, h):
        image = Image.new('RGB', (1200, h + 80), '#f3f2e9')
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 1200, 12), fill='#235d47')
        y = 50
        for text, f in content:
            draw.text((64, y), text, font=f, fill='#233d35')
            y += 56 if f == title_font else 38
        name = f'report-{len(pages) + 1:02}.png'
        image.save(out / name)
        pages.append(name)
    for text, f in wrapped:
        step = 56 if f == title_font else 38
        if height + step > 6000:
            save(batch, height)
            batch, height = [], 100
        batch.append((text, f))
        height += step
    if batch:
        save(batch, height)
    return pages
