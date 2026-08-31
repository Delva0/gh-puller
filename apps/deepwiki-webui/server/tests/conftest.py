"""server 测试级默认:与根 conftest 同式,且 DEEPWIKI_ROOT 强制临时目录。

envs.py 在导入时单点快照 os.environ —— 必须在任何 gh_puller 导入前:
- 强制 DEEPWIKI_ROOT = tmp(而非 setdefault:外层环境已设也隔离);并 pop + 清除包属性
  强刷 gh_puller.envs(全量套件下其它测试可能已先导入并快照真实根)。
- 引擎导入已零副作用(不再自动建目录):deepwiki 根即 mkdtemp 已存在,状态 IO
  原语测试无需额外前置(项目子目录在写路径时创建)。
"""

import contextlib
import os
import sys
import tempfile

os.environ["DEEPWIKI_ROOT"] = tempfile.mkdtemp(prefix="deepwiki-webui-test-")
os.environ["CBM_CACHE_DIR"] = tempfile.mkdtemp(prefix="cbm-cache-test-")
os.environ["CBM_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="cbm-runtime-test-")
sys.modules.pop("gh_puller.envs", None)
with contextlib.suppress(AttributeError, KeyError):
    delattr(sys.modules["gh_puller"], "envs")
