"""server 测试级默认:与根 conftest 同式(cc env 缺省钉 tmp),且 DEEPWIKI_ROOT 强制临时目录。

envs.py 在导入时单点快照 os.environ —— 必须在任何 gh_puller 导入前:
- 写 DEEPWIKI_CC_CONFIG(tmp 配置文件):空 target(默认 cc)不依赖真实
  ~/.claude/settings.json 是否存在;
- 强制 DEEPWIKI_ROOT = tmp(而非 setdefault:外层环境已设也隔离);并 pop + 清除包属性
  强刷 gh_puller.envs(全量套件下其它测试可能已先导入并快照真实根)。
- 引擎导入已零副作用(不再自动建目录):deepwiki 根即 mkdtemp 已存在,状态 IO
  原语测试无需额外前置(项目子目录在写路径时创建)。
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

_cc_dir = Path(tempfile.mkdtemp(prefix="deepwiki-webui-cc-config-"))
_cc_file = _cc_dir / "claude-settings.json"
_cc_file.write_text("{}", encoding="utf-8")
os.environ["DEEPWIKI_CC_CONFIG"] = str(_cc_file)
os.environ["DEEPWIKI_ROOT"] = tempfile.mkdtemp(prefix="deepwiki-webui-test-")
sys.modules.pop("gh_puller.envs", None)
with contextlib.suppress(AttributeError, KeyError):
    delattr(sys.modules["gh_puller"], "envs")
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _cc_config_default():
    """cc 的 env 缺省配置路径(= conftest 设置的 tmp 文件;见模块 docstring)。"""
    return str(_cc_file)
