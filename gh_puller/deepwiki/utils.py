"""generator 选型/判等/凭证规则簇 + deepwiki 域内日志 + repo 键(引擎共用 helper)。

共用 helper:`_log`(deepwiki 域内统一进度日志单点)、`repo_key_of`
(repo 键,type_owner_repo,任务注册键/交付件目录前缀同式)、generator 选型簇
(见下「generator 选型」段,唯一知识源)。
页面内容渲染、模型产出 XML 解析与索引保障服务属生成协议(pipeline 内);
图/成品/续跑产物的 dir/path 布局在 cache。

叶子模块:仅 stdlib + 根包 ..utils(**非**本包 deepwiki/utils —— `..utils`
从包外解析)+ envs/agent 单向依赖,零导入副作用;被 pipeline/cache 白名单直连,
deepwiki 主干白名单 re-export。术语:引擎内部一律说 generator(选型 dict =
{generator, generator_config});"target" 只存在于一个冻结边界 —— wire 字段
(apps/schemas)。
"""

import os
from functools import partial

from .. import envs
from ..agent import GENERATORS
from ..utils import _log as _utils_log

# 进度日志走 stderr(同 graphify.py 约定);prefix 固定 [deepwiki]
_log = partial(_utils_log, prefix="deepwiki")


# ---------------------------------------------------------------------------
# generator 选型(解析 / 判等身份 / 凭证落盘规则;唯一知识源)
# ---------------------------------------------------------------------------


def _default_get_env(key: str) -> str:
    """env 缺省桥接(envs 模块对象 getattr 调用时取 —— 测试 monkeypatch/pop+delattr 强刷生效)。"""
    return getattr(envs, key, "") or ""


# file 类生成器(它们的 config 是一条配置文件路径)→ config_path 的 env 缺省键;
# 这是本层的契约知识(agent 包不提供任何缺省/元数据假设,见 configs.py 上层自验哲学)。
_FILE_CONFIG_PATH_ENV = {"cc": "DEEPWIKI_CC_CONFIG", "dsh": "DEEPWIKI_DSH_CORDIS",
                         "codex": "DEEPWIKI_CODEX_CONFIG"}


def _resolve_generator(choice: dict | None = None, get_env=None) -> tuple[str, dict]:
    """generator 选型 dict({generator, generator_config})→ (generator id, 规范化配置);
    空选型走 env 缺省(与运行期一致)。

    本层自称:未知 id 报错;file 类(cc/dsh/codex)config_path = 显式 > env 缺省
    (> 空),~ 展开并绝对化 —— 对象类(llm)透传(api_key/base_url 等由调用方自管)。
    envs 走模块对象 getattr(测试 monkeypatch 与 pop+delattr 强刷均生效)。
    """
    if get_env is None:
        get_env = _default_get_env
    gen_id = (choice or {}).get("generator") or get_env("DEEPWIKI_GENERATOR") or "cc"
    if gen_id not in GENERATORS:
        raise ValueError(f"未知 generator: {gen_id!r}(可选 {sorted(GENERATORS)})")
    resolved = dict((choice or {}).get("generator_config") or {})
    env_key = _FILE_CONFIG_PATH_ENV.get(gen_id)
    if env_key:
        config_path = resolved.get("config_path") or get_env(env_key) or ""
        if config_path:
            config_path = os.path.abspath(os.path.expanduser(config_path))
        resolved["config_path"] = config_path
    return gen_id, resolved


def _config_kind(generator_id: str) -> str:
    """file/object 配置类别(cache 落盘/凭证处置决策用)。"""
    return "file" if generator_id in _FILE_CONFIG_PATH_ENV else "object"


def _generator_identity(generator_id: str, resolved: dict) -> str:
    """判等身份(不含凭证):file 类 = config_path;object 类 = "provider|model"。"""
    if _config_kind(generator_id) == "file":
        return resolved.get("config_path", "") or ""
    return f"{resolved.get('provider', '')}|{resolved.get('model', '')}"


def _strip_creds(config: dict) -> dict:
    """落盘形态:拷贝并剥离 generator_config 内 api_key/base_url(config_path 非凭证,保留)。"""
    out = dict(config)
    gc = dict(out.get("generator_config") or {})
    gc.pop("api_key", None)
    gc.pop("base_url", None)
    out["generator_config"] = gc
    return out


def _merge_creds(base: dict, other: dict | None) -> dict:
    """落盘形态保持自身;object 类凭证(api_key/base_url 于 generator_config 内)取 other。"""
    out = dict(base)
    oc = dict((other or {}).get("generator_config") or {})
    merged = dict(out.get("generator_config") or {})
    for key in ("base_url", "api_key"):
        if oc.get(key):
            merged[key] = oc[key]
    out["generator_config"] = merged
    return out


def repo_key_of(repo_type: str, owner: str, repo: str) -> str:
    """repo 键(type_owner_repo;与任务注册键/交付件目录前缀同式)。"""
    return f"{repo_type}_{owner}_{repo}"
