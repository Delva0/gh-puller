"""测试级默认:file 类(cc)的 env 缺省配置路径钉到 tmp,机器无关。

envs.py 在导入时单点快照 os.environ,且 tests/test_deepwiki.py 会 pop+delattr
强制 envs 重新加载(见其文件头注释)—— 因此必须在 conftest 导入时(先于任何
测试模块导入)把 DEEPWIKI_CC_CONFIG 写入 os.environ:无论 envs 何时(重)加载,
快照都命中 tmp 文件,任何测试里的空 target(默认 cc)都不依赖真实
~/.claude/settings.json 是否存在。
"""

import os
import tempfile
from pathlib import Path

_cc_dir = Path(tempfile.mkdtemp(prefix="gh-puller-cc-config-"))
_cc_file = _cc_dir / "claude-settings.json"
_cc_file.write_text("{}", encoding="utf-8")
os.environ["DEEPWIKI_CC_CONFIG"] = str(_cc_file)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _cc_config_default():
    """cc 的 env 缺省配置路径(= conftest 设置的 tmp 文件;见模块 docstring)。"""
    return str(_cc_file)
