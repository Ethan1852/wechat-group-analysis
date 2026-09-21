"""Resolve direct mentions and quoted authors without guessing identities."""
import re
import xml.etree.ElementTree as ET


def reference_metadata(row, parser, self_id):
    result = {'mentioned_user_ids': [], 'quoted_sender_id': '', 'quoted_server_id': '', 'quoted_name': ''}
    for key in ('source', 'message_content', 'compress_content'):
        value = row.get(key)
        try:
            text = parser.decode_blob(value) or ''
            if not isinstance(text, str):
                continue
            start = text.find('<')
            if start < 0:
                continue
            root = ET.fromstring(text[start:])
        except (ET.ParseError, ValueError, TypeError):
            continue
        for node in root.iter('atuserlist'):
            result['mentioned_user_ids'].extend(x.strip() for x in (node.text or '').split(',') if x.strip())
        refer = root if root.tag == 'refermsg' else root.find('.//refermsg')
        if refer is not None:
            result.update(quoted_sender_id=refer.findtext('chatusr') or refer.findtext('fromusr') or '',
                          quoted_server_id=refer.findtext('svrid') or '',
                          quoted_name=refer.findtext('displayname') or '')
    result['mentioned_user_ids'] = list(dict.fromkeys(result['mentioned_user_ids']))
    result['mentions_self'] = self_id in result['mentioned_user_ids']
    result['quotes_self'] = bool(result['quoted_sender_id'] and result['quoted_sender_id'] == self_id)
    return result


def relevance(message, cfg):
    if message.get('is_self') is True:
        return 'commitment' if re.search(r'我(?:来|会|负责|明天|今天|稍后|晚点)', message.get('content', '')) else 'context'
    if message.get('is_self') is not False or message.get('sender_status') != 'resolved':
        return 'possible'
    if not message.get('is_group', message.get('chat_id', '').endswith('@chatroom')):
        return 'private'
    if message.get('mentions_self') or (cfg.get('self_id') and cfg['self_id'] in message.get('mentioned_user_ids', [])):
        return 'mention'
    if message.get('quotes_self') or (cfg.get('self_id') and cfg['self_id'] == message.get('quoted_sender_id')):
        return 'quote'
    aliases = [x.strip() for x in re.split(r'[,，\n]', (cfg.get('self_aliases') or '') + ',' + (cfg.get('self_name') or '')) if x.strip()]
    content = message.get('content', '')
    if any(re.search('@' + re.escape(a) + r'(?=[\s\u2005\u2006，,:：]|$)', content) for a in aliases):
        return 'mention'
    # Names alone can collide. Keep old exports that lack structured quote data
    # visible as possible instead of asserting that the quoted author is self.
    if any(re.search(r'↳\s*引用\s*' + re.escape(a) + r'[：:]', content) for a in aliases):
        return 'possible_quote'
    return 'possible'


RELATION_LABELS = {'private': '私聊', 'mention': '群里 @我', 'quote': '引用我的消息',
                   'commitment': '我的承诺', 'context': '上下文与我相关',
                   'possible_quote': '可能引用了我', 'possible': '可能相关'}
