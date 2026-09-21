"""Render one grounded report.json into HTML, Markdown and a measured PNG."""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, SCALE = 1600, 2
INK, MUTED, BLUE, BG = '#18324b', '#52677e', '#245ac4', '#f2f5fa'


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} 必须是非空字符串')
    return value.strip()


def load_data(run, report_path):
    raw = json.loads((run / 'messages.json').read_text(encoding='utf-8'))
    report = json.loads(report_path.read_text(encoding='utf-8'))
    messages = raw['messages']
    if len(messages) != raw['message_count']:
        raise ValueError('messages.json 的消息数与数组长度不一致')
    ids = {m['id'] for m in messages}
    if len(ids) != len(messages):
        raise ValueError('原始导出消息编号重复')
    intro = nonempty(report.get('intro'), 'intro')
    sections = []
    for key, heading, tone in [('topics', '主要讨论', 'white'),
                               ('followups', '需要跟进的事', 'amber'),
                               ('resolved', '已有回应与结论', 'green'),
                               ('other', '其他交流', 'white')]:
        values = report.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f'{key} 必须是数组')
        cards = []
        for index, item in enumerate(values):
            name = f'{key}[{index}]'
            title = nonempty(item.get('title'), name + '.title')
            paragraphs = item.get('paragraphs')
            if not isinstance(paragraphs, list) or not paragraphs:
                raise ValueError(name + '.paragraphs 必须是非空数组')
            paragraphs = [nonempty(p, name + '.paragraphs') for p in paragraphs]
            refs = item.get('message_ids')
            if not isinstance(refs, list) or not refs or any(type(n) is not int or n not in ids for n in refs):
                raise ValueError(name + '.message_ids 必须引用本次 messages.json 中真实存在的整数编号')
            cards.append({'title': title, 'paragraphs': paragraphs, 'message_ids': sorted(set(refs))})
        if cards:
            sections.append({'heading': heading, 'tone': tone, 'cards': cards})
    if messages and not sections:
        raise ValueError('有消息时必须至少填写一个有出处的报告条目')
    start = datetime.fromisoformat(raw['start'])
    end = datetime.fromisoformat(raw['end_exclusive'])
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError('导出时间窗口无效')
    for message in messages:
        stamp = datetime.fromisoformat(message['time'])
        if not start <= stamp < end:
            raise ValueError('存在超出统计窗口的消息')
    from collections import Counter
    counts = dict(Counter(m['type'] for m in messages))
    participants = len({m['sender_username'] for m in messages if m.get('sender_username')})
    hours = (end-start).total_seconds()/3600
    first_last = ('本窗口首条消息：' + messages[0]['time'] + '；末条：' + messages[-1]['time'] + '。') if messages else '本机记录在该窗口内为零条；不据此断言群里没有聊天。'
    all_ok = bool(raw.get('database_checks')) and all(c.get('quick_check') == ['ok'] for c in raw['database_checks'])
    coverage = ('精确窗口：' + start.isoformat() + ' 至 ' + end.isoformat() + '，不含结束时刻。' +
                '、'.join(f'{value} 条{kind}' for kind, value in counts.items()) +
                f'。读取器去重 {raw.get("duplicates_removed", 0)} 条。' +
                ('消息分片完整性检查通过。' if all_ok else '导出中没有完整的数据库检查通过记录。'))
    limits = '依据本机已同步记录；数据库检查通过不能证明手机消息已全部同步。图片和音视频内部内容未识别，仅保留可用的微信语音转写。群友观点与活动规则未独立核实；报告提出的建议不等于群内已有承诺。'
    if raw.get('self_name'):
        limits += '本账号“我”对应 ' + raw['self_name'] + '。'
    return {'group': raw['group'], 'date': end.strftime('%Y.%m.%d'), 'hours': f'{hours:g}',
            'period': f'{start:%m.%d %H:%M} — {end:%m.%d %H:%M} · UTC{end:%z}',
            'intro': intro, 'sections': sections, 'coverage': coverage, 'first_last': first_last,
            'limits': limits, 'count': len(messages), 'senders': participants,
            'media': sum(counts.get(t, 0) for t in ('图片', '视频', '语音'))}


def sources(ids):
    groups = []
    first = last = ids[0]
    for n in ids[1:]:
        if n == last + 1:
            last = n
        else:
            groups.append(str(first) if first == last else f'{first}–{last}')
            first = last = n
    groups.append(str(first) if first == last else f'{first}–{last}')
    return '来源：消息 ' + '、'.join(groups)


def write_text_outputs(data, output):
    esc = html.escape
    md = [f'# {data["group"]}｜最近 {data["hours"]} 小时总结', data['period'],
          f'{data["count"]} 条消息 · {data["senders"]} 个发送者账号', data['intro']]
    sections = []
    for section in data['sections']:
        md.append('## ' + section['heading'])
        cards = []
        for card in section['cards']:
            citation = sources(card['message_ids'])
            md += ['### ' + card['title'], *card['paragraphs'], citation]
            paragraphs = ''.join('<p>' + esc(p) + '</p>' for p in card['paragraphs'])
            cards.append(f'<article class="card {section["tone"]}"><h3>{esc(card["title"])}</h3>{paragraphs}<p class="source">{esc(citation)}</p></article>')
        sections.append(f'<section><h2>{esc(section["heading"])}</h2>{"".join(cards)}</section>')
    md += ['## 数据范围', data['coverage'], data['first_last'], data['limits']]
    (output / 'summary.md').write_text('\n\n'.join(md) + '\n', encoding='utf-8')
    css = '''
    :root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f2f5fa;color:#18324b;font:17px/1.85 "Microsoft YaHei","PingFang SC",sans-serif}
    header{background:#152f4b;color:white;padding:44px max(24px,calc((100% - 1080px)/2)) 42px}header p{color:#d9e5f5;margin:8px 0}header small{color:#91b9ff}h1{font-size:clamp(28px,4vw,44px);line-height:1.4;margin:18px 0}main{max-width:1128px;margin:30px auto;padding:0 24px 48px}
    .stats{display:grid;grid-template-columns:repeat(3,1fr);background:white;border:1px solid #dce5ef;border-radius:14px;padding:22px;gap:20px}.stats b{font-size:36px;color:#245ac4;display:block}.stats span{color:#52677e;font-size:15px}
    .intro{font-size:20px;font-weight:600;margin:32px 0}h2{font-size:26px;margin:32px 0 18px}h3{font-size:22px;line-height:1.5;margin:0 0 18px}p{margin:0 0 16px;overflow-wrap:anywhere}.card{background:white;border:1px solid #dce5ef;border-radius:14px;padding:28px 32px;margin-bottom:20px}.amber{background:#fff7e9;border-color:#ecd9b7}.green{background:#e8f1ee}.source{font-size:14px;color:#52677e;margin-bottom:0}footer{border-top:1px solid #d1dce8;padding-top:24px;color:#52677e;font-size:14px}summary{cursor:pointer;color:#245ac4;padding:8px 0}details p{margin-top:12px}
    @media(max-width:600px){body{font-size:16px}.stats{padding:16px;gap:10px}.stats b{font-size:30px}.stats span{font-size:12px}.card{padding:22px}.intro{font-size:18px}h3{font-size:20px}}
    @media print{body{background:white}header{background:white;color:#18324b;padding:0}header p,header small{color:#52677e}main{max-width:none;padding:0}.card{break-inside:avoid}.stats{margin-top:20px}details{display:block}}
    '''
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer"><title>{esc(data['group'])} · {esc(data['date'])} 群聊日报</title><style>{css}</style></head><body>
    <header><small>群聊日报 / {esc(data['date'])}</small><h1>这 {esc(data['hours'])} 小时，群里聊了什么</h1><p>{esc(data['group'])}</p><p>{esc(data['period'])}</p></header><main>
    <div class="stats"><div><b>{data['count']}</b><span>条消息</span></div><div><b>{data['senders']}</b><span>个发送者账号</span></div><div><b>{data['media']}</b><span>条图片 / 音视频消息</span></div></div>
    <p class="intro">{esc(data['intro'])}</p>{''.join(sections)}
    <footer><h2>数据范围与阅读口径</h2><p>{esc(data['limits'])}</p><details><summary>查看统计口径与消息覆盖时间</summary><p>{esc(data['coverage'])}</p><p>{esc(data['first_last'])}</p></details><p>{esc(data['group'])} · {esc(data['date'])}</p></footer></main></body></html>'''
    (output / 'index.html').write_text(page, encoding='utf-8')


def render_png(data, output, regular=None, bold=None):
    system = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    regular = Path(regular) if regular else system / 'msyh.ttc'
    bold = Path(bold) if bold else system / 'msyhbd.ttc'
    if not regular.is_file() or not bold.is_file():
        raise ValueError('缺少中文字体，请使用 --font 和 --bold-font 指定字体文件')
    fonts, commands = {}, []
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))

    def font(size, heavy=False):
        key = size, heavy
        if key not in fonts:
            fonts[key] = ImageFont.truetype(str(bold if heavy else regular), size*SCALE)
        return fonts[key]

    def label(x, y, value, size=30, color=INK, heavy=False):
        commands.append(('text', x, y, value, size, color, heavy))

    def box(coords, fill='white', outline=None, radius=20):
        commands.append(('box', coords, fill, outline, radius))

    def wrap(value, width, size=30, heavy=False):
        lines = []
        for part in value.split('\n'):
            line = ''
            for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9._/／+–-]*|.', part):
                pieces = [token] if measure.textlength(token, font=font(size, heavy)) <= width*SCALE else list(token)
                for char in pieces:
                    if line and measure.textlength(line+char, font=font(size, heavy)) > width*SCALE:
                        lines.append(line); line = char
                    else:
                        line += char
            if line:
                lines.append(line)
        return lines

    def paragraph(x, y, value, width, size=30, color=INK, heavy=False):
        for line in wrap(value, width, size, heavy):
            label(x, y, line, size, color, heavy)
            y += math.ceil(size*1.65)
        return y

    def ph(value, width, size=30, heavy=False):
        return len(wrap(value, width, size, heavy))*math.ceil(size*1.65)

    head_height = 300 + ph(data['group'], 1410, 31)
    box((0, 0, W, head_height), '#152f4b', radius=0)
    label(90, 52, '群聊日报 / ' + data['date'], 26, '#91b9ff', True)
    title = '这 ' + data['hours'] + ' 小时，群里聊了什么'
    # Realistic periods fit a single line; long titles are measured, not clipped.
    title_size = 60
    while measure.textlength(title, font=font(title_size, True)) > 1420*SCALE and title_size > 32:
        title_size -= 2
    label(90, 110, title, title_size, 'white', True)
    gy = paragraph(90, 214, data['group'], 1410, 31, '#d9e5f5')
    label(90, gy+8, data['period'], 28, '#d9e5f5')
    y = head_height + 40
    box((80, y, 1520, y+155), 'white', '#dce5ef')
    for i, (value, caption) in enumerate([(data['count'], '条消息'), (data['senders'], '个发送者账号'), (data['media'], '条图片 / 音视频')]):
        x = 125 + i*475
        label(x, y+12, str(value), 54, BLUE, True)
        label(x, y+95, caption, 25, MUTED)
    y = paragraph(95, y+195, data['intro'], 1410, 33, INK, True) + 34
    for section in data['sections']:
        label(95, y, section['heading'], 39, INK, True)
        y += 82
        fill, border = {'white': ('white', '#dce5ef'), 'amber': ('#fff7e9', '#ecd9b7'), 'green': ('#e8f1ee', '#d5e5df')}[section['tone']]
        for card in section['cards']:
            citation = sources(card['message_ids'])
            height = 68 + ph(card['title'], 1320, 34, True) + sum(ph(p, 1320)+20 for p in card['paragraphs']) + ph(citation, 1320, 23)
            box((80, y, 1520, y+height), fill, border)
            cy = paragraph(125, y+26, card['title'], 1320, 34, INK, True) + 18
            for p in card['paragraphs']:
                cy = paragraph(125, cy, p, 1320) + 20
            paragraph(125, cy, citation, 1320, 23, MUTED)
            y += height+24
        y += 20
    label(95, y, '数据范围与阅读口径', 31, INK, True)
    y += 65
    for p in (data['coverage'], data['first_last'], data['limits']):
        y = paragraph(95, y, p, 1410, 25, MUTED) + 20
    y = paragraph(95, y+15, data['group'] + ' · ' + data['date'], 1410, 24, MUTED) + 55
    if y > 16000:
        raise ValueError('摘要排版超过 16000 像素，请精简报告或明确拆分；没有截断正文')
    image = Image.new('RGB', (W*SCALE, y*SCALE), BG)
    draw = ImageDraw.Draw(image)
    for command in commands:
        if command[0] == 'text':
            _, x, cy, value, size, color, heavy = command
            draw.text((x*SCALE, cy*SCALE), value, font=font(size, heavy), fill=color)
        else:
            _, coords, fill, outline, radius = command
            draw.rounded_rectangle(tuple(round(v*SCALE) for v in coords), radius=radius*SCALE, fill=fill, outline=outline, width=2*SCALE)
    image.resize((W, y), Image.Resampling.LANCZOS).save(output / 'report.png', 'PNG', optimize=True)
    return W, y


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser()
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--report', type=Path)
    p.add_argument('--out-dir', type=Path)
    p.add_argument('--font')
    p.add_argument('--bold-font')
    args = p.parse_args()
    run = args.run_dir.resolve()
    data = load_data(run, args.report or run / 'report.json')
    output = (args.out_dir or run).resolve()
    output.mkdir(parents=True, exist_ok=True)
    width, height = render_png(data, output, args.font, args.bold_font)
    write_text_outputs(data, output)
    print(json.dumps({'png': str(output / 'report.png'), 'html': str(output / 'index.html'), 'markdown': str(output / 'summary.md'), 'width': width, 'height': height}, ensure_ascii=False))


if __name__ == '__main__':
    main()
