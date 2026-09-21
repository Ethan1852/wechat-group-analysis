import argparse
import json
import math
import mimetypes
import secrets
import threading
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .analysis import analyze, model_request
from .config import DATA, ROOT, accounts, public_config, read_config, write_config
from .reader import TZ, read_all
from .reports import build_report, prepare_digest, render_html
from .relevance import relevance
from .store import Store

TOKEN = secrets.token_urlsafe(32)
JOB = {'running': False, 'phase': '准备就绪', 'error': None}
LOCK = threading.Lock()
REVISION = 0
PREVIEW_CACHE = {}


def progress(message):
    JOB['phase'] = message


def scan(cfg, hours, end, reuse=False):
    try:
        store = Store()
        if reuse:
            latest = store.latest(cfg['account'])
            if not latest:
                raise ValueError('没有可重新整理的记录，请先读取微信')
            meta = dict(latest['meta'])
            start_ts = datetime.fromisoformat(meta['start']).timestamp()
            end_ts = datetime.fromisoformat(meta['end_exclusive']).timestamp()
            messages = store.window_messages(cfg['account'], start_ts, end_ts)
            progress('正在重新整理已读记录，无需重新解密')
        else:
            meta, messages = read_all(cfg, hours, progress, end)
        progress('正在保存读取结果')
        # Persist a usable export even if the model is unreachable.
        inputs = store.context_for_pending(cfg['account'], messages)
        progress('正在提取待办与待回复')
        effective_cfg = cfg | {'self_name': cfg.get('self_name') or meta['self_name'], 'self_id': meta['self_id'],
                               'reporting_window': {'start_ts': datetime.fromisoformat(meta['start']).timestamp(), 'end_ts': datetime.fromisoformat(meta['end_exclusive']).timestamp()}}
        items, summaries, mode = analyze(inputs, effective_cfg, progress)
        by_id = {i['id']: i for i in items}
        for old in store.items(cfg['account']):
            if old['kind'] != 'reply' or old['status'] not in ('open', 'snoozed') or old['id'] in by_id:
                continue
            later = [m for m in inputs if m['chat_id'] == old['chat_id'] and m['is_self'] is True and m['timestamp'] > old['timestamp']]
            if later:
                revised = old | {'reason': '此消息之后已发现本人发言，是否已经答复该问题需核实；原待回复事项保留至手动处理。',
                                 'suggested_status': 'review', 'evidence_ids': list(dict.fromkeys(old['evidence_ids'] + [later[-1]['id']]))}
                items.append(revised)
        meta['analysis_context_count'] = len(inputs)
        store.save(meta, messages, items, summaries, mode)
        progress(f'完成：读取 {len(messages)} 条消息，提取 {len(items)} 项候选事项')
    except Exception as exc:
        JOB['error'] = str(exc)[:1000]
        JOB['phase'] = '本次读取未完成；历史结果仍保留'
        # No full chat content or credentials in logs.
        print(f'读取失败：{type(exc).__name__}: {str(exc)[:1000]}', flush=True)
    finally:
        JOB['running'] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def allowed(self):
        valid = (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')
        if self.headers.get('Host') not in valid:
            return False
        origin = self.headers.get('Origin')
        return not origin or origin in ('http://' + value for value in valid)

    def respond(self, body, status=200, mime='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed():
            return self.respond({'error': '仅允许本机访问'}, 403)
        parsed = urlparse(self.path)
        if parsed.path == '/':
            page = (ROOT / 'workbench/static/index.html').read_text('utf-8').replace('__TOKEN__', TOKEN).replace('__LIBRARY_SCRIPT__', (ROOT / 'workbench/static/library.js').read_text('utf-8'))
            return self.respond(page.encode(), mime='text/html; charset=utf-8')
        query = parse_qs(parsed.query)
        if self.headers.get('X-Workbench-Token') != TOKEN and query.get('token', [''])[0] != TOKEN:
            return self.respond({'error': '访问令牌无效，请刷新页面'}, 403)
        try:
            cfg, store = read_config(), Store()
            if parsed.path in ('/api/favorites', '/api/people', '/api/person-messages'):
                value = lambda k, default='': query.get(k, [default])[0]
                offset = max(0, int(value('offset', '0')))
                limit = min(50, max(1, int(value('limit', '20'))))
                search = value('q')[:300]
                if parsed.path == '/api/favorites':
                    return self.respond(store.favorites(cfg['account'], search, offset, limit))
                if parsed.path == '/api/people':
                    return self.respond(store.people(cfg['account'], search, offset, limit))
                uid = value('user_id')[:300]
                if not uid:
                    raise ValueError('请先选择用户')
                dates = []
                for key in ('start', 'end'):
                    date = datetime.fromisoformat(value(key)) if value(key) else None
                    if date and date.tzinfo is None:
                        raise ValueError('筛选时间必须包含时区')
                    dates.append(date.timestamp() if date else None)
                if all(d is not None for d in dates) and dates[0] >= dates[1]:
                    raise ValueError('结束时间须晚于开始时间')
                return self.respond(store.person_messages(cfg['account'], uid, search, value('scope', 'all'),
                                                         *dates, value('chat_id'), offset, limit))
            if parsed.path == '/api/pulse':
                return self.respond({'account': cfg['account'], 'latest_id': store.latest_id(cfg['account']), 'revision': REVISION, 'job': JOB.copy()})
            if parsed.path == '/api/state':
                latest, items = store.latest(cfg['account']), store.items(cfg['account'])
                lookup = {m['id']: m for m in store.messages(cfg['account'], list({i.get('anchor_id') for i in items if i.get('anchor_id')}))}
                context = cfg | {'self_name': cfg.get('self_name') or (latest or {}).get('meta', {}).get('self_name', ''), 'self_id': (latest or {}).get('meta', {}).get('self_id')}
                for item in items:
                    direct = relevance(lookup[item['anchor_id']], context) if item.get('anchor_id') in lookup else 'possible'
                    item['relation'] = 'context' if direct in ('possible', 'possible_quote') and item.get('relation') == 'context' else direct
                    item['personal'] = item['relation'] in ('private', 'mention', 'quote', 'commitment', 'context')
                if latest:
                    latest = latest | {'summary_count': len(latest['summaries']), 'summaries': []}
                return self.respond({'config': public_config(), 'accounts': accounts(), 'job': JOB.copy(),
                                     'latest': latest, 'items': items})
            if parsed.path == '/api/preview':
                key = (cfg['account'], store.latest_id(cfg['account']), REVISION)
                if key not in PREVIEW_CACHE:
                    page = render_html(prepare_digest(store, cfg['account']))
                    PREVIEW_CACHE.clear()
                    PREVIEW_CACHE[key] = page
                return self.respond(PREVIEW_CACHE[key].encode(), mime='text/html; charset=utf-8')
            if parsed.path == '/api/messages':
                return self.respond(store.messages(cfg['account'], query.get('ids', [''])[0].split(',')[:500]))
            if parsed.path.startswith('/download/'):
                root = (DATA / 'reports').resolve()
                target = (root / parsed.path[len('/download/'):]).resolve()
                if not target.is_relative_to(root) or not target.is_file() or target.suffix not in ('.html', '.png', '.json', '.txt', '.md', '.zip'):
                    return self.respond({'error': '文件不存在'}, 404)
                return self.respond(target.read_bytes(), mime=mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
            self.respond({'error': '未找到'}, 404)
        except Exception as exc:
            self.respond({'error': str(exc)}, 400)

    def do_POST(self):
        global REVISION
        if not self.allowed() or self.headers.get('X-Workbench-Token') != TOKEN:
            return self.respond({'error': '请求来源无效，请刷新页面'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 100_000:
                raise ValueError('请求大小无效')
            data = json.loads(self.rfile.read(length))
            cfg = read_config()
            if self.path in ('/api/favorite', '/api/unfavorite'):
                store = Store()
                uid = str(data.get('message_id', ''))[:300]
                if self.path == '/api/favorite':
                    store.favorite(cfg['account'], uid, **{k: data[k] for k in ('title', 'note', 'tags') if k in data})
                else:
                    store.unfavorite(cfg['account'], uid)
                return self.respond({'ok': True})
            if self.path == '/api/settings':
                if JOB['running']:
                    raise ValueError('读取期间请勿切换账号或模型设置')
                write_config(data)
                REVISION += 1
                return self.respond(public_config())
            if self.path == '/api/test-model':
                if not cfg.get('external_enabled'):
                    raise ValueError('请在设置中启用模型并保存')
                model_request(cfg, [{'role': 'user', 'content': '仅回答 OK。这是连接测试，不包含微信记录。'}])
                return self.respond({'ok': True})
            if self.path in ('/api/scan', '/api/reanalyze'):
                hours = float(data.get('hours', 24))
                if not math.isfinite(hours) or not 0 < hours <= 8760:
                    raise ValueError('读取时长须介于 0 至 8760 小时')
                end = datetime.fromisoformat(data['end']) if data.get('end') else None
                if end is not None and end.tzinfo is None:
                    raise ValueError('截止时间必须包含时区')
                with LOCK:
                    if JOB['running']:
                        raise ValueError('已有读取任务正在运行')
                    if cfg['account'] not in accounts():
                        raise ValueError('请先选择有效的本人账号')
                    JOB.update(running=True, phase='开始读取', error=None)
                    threading.Thread(target=scan, args=(cfg, hours, end, self.path == '/api/reanalyze'), daemon=True).start()
                return self.respond({'started': True})
            if self.path == '/api/status':
                Store().set_status(cfg['account'], data['id'], data['status'], data.get('note', ''))
                REVISION += 1
                return self.respond({'ok': True})
            if self.path == '/api/export':
                if JOB['running']:
                    raise ValueError('请等待本次读取完成后导出')
                return self.respond(build_report(Store(), cfg['account'], include_raw=data.get('include_raw') is True))
            self.respond({'error': '未找到'}, 404)
        except Exception as exc:
            self.respond({'error': str(exc)[:1000]}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'微信晚间复盘：http://127.0.0.1:{args.port}', flush=True)
    if not args.no_browser:
        webbrowser.open(f'http://127.0.0.1:{args.port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
