# 固定源码与兼容范围

本技能携带实际使用的源码子集，不需要旧项目目录，也不在每次运行时下载上游代码：

- `vendor/wechatauto-replica/wechatauto/db.py`、`media.py`：fanyuantaier/wechatauto-replica，提交 `04ef8cbde3862cff90b5f6b42c9ebfcea44ef48d`。
- `vendor/wechat-chat-export/exporter_core.py`：zhuzhangxue/wechat-chat-export，提交 `5b56e51cd9368bc7e4c563761057b561501809d9`。
- 保留各自 Apache-2.0 LICENSE，以及导出器的 THIRD_PARTY_NOTICES.md。仅加载解析所需定义，不调用媒体下载、UI、消息发送或远程下载函数。

已在本机 Windows 微信 4.1.13.65 真实读取。4.x 内部结构随版本可能变化；不声称通用支持 macOS 或所有微信版本。读取使用 Config.Cipher 内存配置获取候选库密钥，经首页 HMAC-SHA512 校验，按 SQLCipher 4 页面格式使用 AES-256-CBC 解密并合并 WAL；缺少可用密钥时底层另有主密钥派生回退。首页验证与 SQLite quick_check 并不等于逐页 HMAC 认证或手机消息同步完整性证明。

读取器覆写 `_save_keys`，密钥不主动保存到文件；解密副本使用独立 TemporaryDirectory，正常退出自动清理。进程强杀可能遗留本次临时目录。不要自动删除无法确认归属的缓存。

依赖版本写在 `scripts/requirements.txt`，优先用已有 Python。确有缺失时，可在本地任务工作目录创建虚拟环境安装该文件；不必重复安装。换电脑后先配置 `settings.local.json` 的数据库父目录与账号目录，避免沿用其他机器的身份。
