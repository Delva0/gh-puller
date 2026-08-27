"""测试级默认:file 类(cc)的 env 缺省配置路径钉到 tmp,产物根隔离到临时目录。

envs.py 在导入时单点快照 os.environ —— 因此必须在 conftest 导入时(先于任何
测试模块导入)写入:
- DEEPWIKI_CC_CONFIG(tmp 配置文件):空 target(默认 cc)不依赖真实
  ~/.claude/settings.json 是否存在;
- DEEPWIKI_ROOT(tmp,强制赋值而非 setdefault):即使外层环境已设,本套件也不落
  用户真实目录;且全量套件下其它测试可能已先 import gh_puller.agent → 模块级
  `from .. import envs` 已把真实根快照进 sys.modules —— 必须 pop + 清除包属性
  (仅 pop 时 `from pkg import mod` 会命中包上的缓存属性,仍需 delattr),
  让 envs 以临时根重新加载。
- AGENT_MONITOR_DIR(tmp,强制赋值):agent 监控文件 sink 恒开(configure 已无 file
  开关),测试默认落盘必须重定向到临时目录;同上述强刷,任何模块重新导入都命中
  tmp 而非真实 ~/.gh-puller。
- 引擎导入零副作用(不再自动建 wikicache):显式建好,状态 IO 原语测试的前置。
"""

import os
import sys
import tempfile
from pathlib import Path

_cc_dir = Path(tempfile.mkdtemp(prefix="gh-puller-cc-config-"))
_cc_file = _cc_dir / "claude-settings.json"
_cc_file.write_text("{}", encoding="utf-8")
os.environ["DEEPWIKI_CC_CONFIG"] = str(_cc_file)
os.environ["DEEPWIKI_ROOT"] = tempfile.mkdtemp(prefix="deepwiki-test-")
os.environ["AGENT_MONITOR_DIR"] = tempfile.mkdtemp(prefix="gh-puller-agent-monitor-test-")
sys.modules.pop("gh_puller.envs", None)
try:
    delattr(sys.modules["gh_puller"], "envs")
except (AttributeError, KeyError):
    pass
os.makedirs(os.path.join(os.environ["DEEPWIKI_ROOT"], "wikicache"), exist_ok=True)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _cc_config_default():
    """cc 的 env 缺省配置路径(= conftest 设置的 tmp 文件;见模块 docstring)。"""
    return str(_cc_file)
