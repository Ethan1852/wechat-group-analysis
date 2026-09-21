import base64
import ctypes
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'local-data'
SETTINGS = ROOT / 'settings.local.json'
DEFAULTS = {'db_dir': str(Path.home() / 'Documents' / 'xwechat_files'), 'account': '', 'self_name': '',
            'self_aliases': '', 'model_url': '', 'model': '', 'external_enabled': False}


def read_config():
    return DEFAULTS | (json.loads(SETTINGS.read_text('utf-8')) if SETTINGS.exists() else {})


def write_config(values):
    cfg = read_config()
    for key in DEFAULTS:
        if key in values:
            cfg[key] = values[key] if key == 'external_enabled' else str(values[key]).strip()
    if type(cfg['external_enabled']) is not bool:
        raise ValueError('模型启用选项格式错误')
    if cfg['external_enabled'] and (not cfg['model_url'] or not cfg['model']):
        raise ValueError('启用模型前请填写 Base URL 和模型名称')
    account = cfg['account']
    if account and (Path(account).name != account or '/' in account or '\\' in account or account in ('.', '..')):
        raise ValueError('账号必须为数据目录下的文件夹名称')
    DATA.mkdir(exist_ok=True)
    if values.get('api_key'):
        (DATA / 'credential.dpapi').write_bytes(protect(str(values['api_key']).encode()))
    if values.get('clear_api_key'):
        (DATA / 'credential.dpapi').unlink(missing_ok=True)
    temp = SETTINGS.with_suffix('.tmp')
    temp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
    temp.replace(SETTINGS)
    return cfg


class Blob(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_char))]


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('凭据安全保存需要 Windows DPAPI')
    buffer = ctypes.create_string_buffer(data)
    src, dst = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), Blob()
    api = ctypes.windll.crypt32
    if decrypt:
        ok = api.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst))
    else:
        ok = api.CryptProtectData(ctypes.byref(src), 'WeChat workbench', None, None, None, 1, ctypes.byref(dst))
    if not ok:
        raise RuntimeError('Windows 凭据加密/解密失败')
    try:
        return ctypes.string_at(dst.data, dst.length)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(dst.data, ctypes.c_void_p))


def api_key():
    path = DATA / 'credential.dpapi'
    return protect(path.read_bytes(), True).decode() if path.exists() else os.environ.get('WECHAT_MODEL_API_KEY', '')


def public_config():
    return read_config() | {'has_api_key': bool(api_key())}


def accounts(root=None):
    parent = Path(root or read_config()['db_dir'])
    if not parent.is_dir():
        return []
    return sorted(p.name for p in parent.iterdir() if p.is_dir() and (p / 'db_storage').is_dir())
