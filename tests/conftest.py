"""测试级默认:产物根与监控目录隔离到临时目录。

envs.py 在导入时单点快照 os.environ —— 因此必须在 conftest 导入时(先于任何
测试模块导入)写入:
- DEEPWIKI_ROOT(tmp,强制赋值而非 setdefault):即使外层环境已设,本套件也不落
  用户真实目录;且全量套件下其它测试可能已先 import gh_puller.agent → 模块级
  `from .. import envs` 已把真实根快照进 sys.modules —— 必须 pop + 清除包属性
  (仅 pop 时 `from pkg import mod` 会命中包上的缓存属性,仍需 delattr),
  让 envs 以临时根重新加载。
- AGENT_MONITOR_DIR(tmp,强制赋值):agent 监控文件 sink 恒开(configure 已无 file
  开关),测试默认落盘必须重定向到临时目录;同上述强刷,任何模块重新导入都命中
  tmp 而非真实 ~/.gh-puller。
- 引擎导入零副作用(不再自动建目录):deepwiki 根即 mkdtemp 已存在(App 进程侧
  建根与项目子目录),状态 IO 原语测试无需额外前置。
"""

import os
import sys
import tempfile
from contextlib import suppress

os.environ["DEEPWIKI_ROOT"] = tempfile.mkdtemp(prefix="deepwiki-test-")
os.environ["AGENT_MONITOR_DIR"] = tempfile.mkdtemp(prefix="gh-puller-agent-monitor-test-")
sys.modules.pop("gh_puller.envs", None)
# 包属性可能未被 from-import 缓存(AttributeError),包条目也可能不在 sys.modules(KeyError)
with suppress(AttributeError, KeyError):
    delattr(sys.modules["gh_puller"], "envs")
