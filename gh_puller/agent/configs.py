"""config 世界:每生成器一种 dict schema(TypedDict)+ config 派生纯函数。

层级纪律(本文件 = config 层;generators 顶层依赖本层,本层零生成器依赖):
- 只依赖标准库;SDK 类型仅在函数内懒导入接触(测试经 sys.modules 假模块注入;
  import 面保持零 SDK/零事件/零 generators)—— 输入均为普通 dict(键名见各
  TypedDict),本层负责 **config → SDK 对象/派生字段的全部转换**,generators
  只消费最终装配件、不关心转换过程;
- 类型良定义:每生成器一个 TypedDict(下方 *Config,键可省略,键集即白名单
  语义,上层校验按此查混键);
- 唯一模块态:内容哈希键控的 cordis 文件缓存(_DSH_CORDIS_FILE,见
  dsh_cordis_path)。

config 概念契约(与 generators.py 上层 API 契约互补,人类开发者正式定义):
- 键名跨生成器收敛:config_path/system_prompt/model/api_key/base_url/cwd/
  mcp_servers/allowed_tools/env;SDK 专属名映射收在下方各映射函数 —— cc
  config_path → ClaudeAgentOptions.settings;dsh config_path → cordis、
  system_prompt → env.DSH_SYSTEM_PROMPT;codex system_prompt → base_instructions;
  llm 的 config 只有 OpenAIConfig{model, base_url, api_key},请求体(payload
  = OpenAI 兼容 chat/completions 请求体)独立于 config 运行时传入。
- 隔离组合装配属于 config 层(组合即隔离边界):dsh 的内置 cordis 组合
  (dsh_cordis_path,镜像官方 minimal.cordis.yml,零用户级补丁)、codex 的
  隔离 home(codex_home_path / codex_home_setup,零用户级配置)。
"""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import TypedDict


# ---------------------------------------------------------------------------
# config 类型(每生成器一种 dict schema;键可省略,键集即解析层白名单语义)
# ---------------------------------------------------------------------------


class ClaudeConfig(TypedDict, total=False):
    """cc 运行时 config:整体作为 ClaudeAgentOptions(**config);config_path → settings。"""

    model: str
    system_prompt: str
    allowed_tools: list[str]
    mcp_servers: dict
    cwd: str
    config_path: str
    add_dirs: list[str]
    permission_mode: str
    setting_sources: list[str]
    include_partial_messages: bool
    max_turns: int


class DshConfig(TypedDict, total=False):
    """dsh 运行时 config:映射 DeepSeekHarness kwargs;config_path → cordis,
    system_prompt → env.DSH_SYSTEM_PROMPT。"""

    provider: str
    model: str
    max_tokens: int
    cwd: str
    runtime_cwd: str
    session_root: str
    env: dict
    config_path: str
    mcp_servers: list[dict]
    base_url: str
    api_key: str
    runtime_bin: str
    launch_args_override: list[str]
    request_timeout_seconds: float
    shutdown_timeout_seconds: float


class CodexConfig(TypedDict, total=False):
    """codex 运行时 config:映射 CodexConfig/thread/turn kwargs;system_prompt →
    base_instructions。"""

    model: str
    system_prompt: str
    cwd: str
    codex_bin: str
    codex_home: str
    config_path: str
    sandbox: str
    approval_mode: str
    token: str
    env: dict
    timeout_seconds: float
    mcp_servers: list[dict]
    allowed_tools: list[str]
    effort: str
    output_schema: dict
    config_overrides: dict
    launch_args_override: list[str]
    base_instructions: str
    service_tier: str
    summary: dict
    web_search: bool


class OpenAIConfig(TypedDict, total=False):
    """llm 运行时 config(model/base_url/api_key);请求体(payload)独立传入。"""

    model: str
    base_url: str
    api_key: str
    provider: str


# ---------------------------------------------------------------------------
# SDK 字段映射(config dict → SDK 构造 kwargs;None 跳过 → 走 SDK 缺省)
# ---------------------------------------------------------------------------


def dsh_fields(config: dict) -> dict:
    """DshConfig → DeepSeekHarness 构造 kwargs(概念键映射 + 缺省隔离组合)。

    DeepSeekHarnessConfig 是 dataclass 而非 pydantic(无 model_dump),且
    DeepSeekHarness.__init__(config=None, **kwargs) 同时传 config 与 kwargs 会
    TypeError —— 恒走 kwargs;调用方自组装 config(键集见 DshConfig)。
    config_path → SDK cordis;system_prompt → env.DSH_SYSTEM_PROMPT(已有 env 键
    优先);未提供 cordis 时经 mcp_servers 描述回退内置隔离组合(见 dsh_cordis_path)。
    """
    names = ("provider", "model", "max_tokens", "cwd", "runtime_cwd", "session_root",
             "env", "runtime_bin", "launch_args_override",
             "request_timeout_seconds", "shutdown_timeout_seconds", "base_url", "api_key")
    fields = {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}
    if config.get("config_path"):
        fields["cordis"] = config["config_path"]
    if config.get("system_prompt"):
        env = dict(fields.get("env") or {})
        env.setdefault("DSH_SYSTEM_PROMPT", config["system_prompt"])
        fields["env"] = env
    fields.setdefault("cordis", dsh_cordis_path(config.get("mcp_servers")))
    return fields


def codex_config_fields(config: dict) -> dict:
    """CodexConfig → CodexConfig(SDK)构造 kwargs(None 跳过 → 走 SDK 缺省)。

    与 dsh_fields 同法:调用方自组装 config(键集见 CodexConfig);env 由调用流
    合并 CODEX_HOME 后整传入 —— SDK 从不自设 CODEX_HOME,隔离点见
    codex_home_path / codex_home_setup。
    """
    names = ("cwd", "codex_bin", "config_overrides", "launch_args_override", "env")
    return {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}


def codex_thread_fields(config: dict) -> dict:
    """CodexConfig → AsyncCodex.thread_start 透传 kwargs(sandbox/approval_mode 由
    调用方经 codex_lookup 转换后塞入,此处只保留其余自由字段)。

    system_prompt 缺省映射 base_instructions(与 cc 的 system_prompt 同位语义)。
    """
    names = ("base_instructions", "developer_instructions", "personality", "ephemeral",
             "model", "model_provider", "service_tier", "config", "cwd",
             "session_start_source", "thread_source")
    fields = {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}
    if config.get("system_prompt") and "base_instructions" not in fields:
        fields["base_instructions"] = config["system_prompt"]
    return fields


def codex_turn_fields(config: dict) -> dict:
    """CodexConfig → AsyncThread.turn 透传 kwargs。"""
    names = ("cwd", "effort", "output_schema", "personality", "service_tier", "summary")
    return {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}


# ---------------------------------------------------------------------------
# config → SDK 对象装配(dict 到 SDK 类型/单次运行件;懒导入,测试喂假模块)
# ---------------------------------------------------------------------------


def claude_options(config: dict):
    """ClaudeConfig → ClaudeAgentOptions 实例(config_path → settings 装载)。

    本层只做键映射(config 原样透传 + config_path → settings),不关心 SDK 运行细节。
    """
    from claude_agent_sdk import ClaudeAgentOptions  # lazy:测试可喂假模块

    sdk_options = {k: v for k, v in config.items() if k != "config_path"}  # 概念键不得透传
    if config.get("config_path"):  # 统一概念键 → SDK settings(--settings 装载)
        sdk_options["settings"] = config["config_path"]
    return ClaudeAgentOptions(**sdk_options)


def dsh_harness(config: dict):
    """DshConfig → DeepSeekHarness 实例(懒导入;kwargs 映射见 dsh_fields)。"""
    from deepseek_harness import DeepSeekHarness  # lazy:测试可喂假模块

    return DeepSeekHarness(**dsh_fields(config))


def codex_config(config: dict):
    """CodexConfig → SDK CodexConfig 实例(懒导入)。

    codex_config_fields 的 env 合并 CODEX_HOME(SDK 从不自设;隔离凭它生效,见
    codex_home_path)。
    """
    from openai_codex import CodexConfig  # lazy:测试可喂假模块

    fields = codex_config_fields(config)
    env = dict(config.get("env") or {})
    env["CODEX_HOME"] = codex_home(config)
    fields["env"] = env
    return CodexConfig(**fields)


def codex_thread(config: dict) -> dict:
    """CodexConfig → AsyncCodex.thread_start kwargs(sandbox/approval_mode 经
    codex_lookup 转枚举;其余见 codex_thread_fields)。"""
    from openai_codex import ApprovalMode, Sandbox  # lazy:测试可喂假模块

    fields = codex_thread_fields(config)
    if (sandbox := config.get("sandbox")) is not None:
        fields["sandbox"] = codex_lookup(sandbox, Sandbox, "Sandbox")
    if (approval := config.get("approval_mode")) is not None:
        fields["approval_mode"] = codex_lookup(approval, ApprovalMode, "approval_mode")
    return fields


def codex_turn(config: dict) -> dict:
    """CodexConfig → AsyncThread.turn kwargs(见 codex_turn_fields)。

    summary 缺省 detailed:CLI 侧 summary=auto 经模型元数据 default_reasoning_summary
    "none" 解析(全模型默认不产可见推理摘要)→ 事件流无 thinking。观测者原则是
    agent 发生什么就落盘什么——显式打开可见推理摘要流;显式 "none"/"auto"/"concise"
    仍尊重。
    """
    fields = codex_turn_fields(config)
    fields.setdefault("summary", "detailed")
    return fields


def codex_home(config: dict) -> str:
    """CodexConfig → 隔离 home(codex_home_setup:config.toml + auth 引导)。"""
    return codex_home_setup(config.get("codex_home") or codex_home_path(),
                            config_path=config.get("config_path"),
                            mcp_servers=config.get("mcp_servers"),
                            web_search=config.get("web_search") or False)


# ---------------------------------------------------------------------------
# 枚举归一(config 的 Sandbox/ApprovalMode 名/值/枚举成员 → SDK 枚举)
# ---------------------------------------------------------------------------


def codex_val(x):
    """枚举成员 → 值(pydantic/codex 枚举 .value;普通值原样)—— 状态比较统一走值。"""
    return getattr(x, "value", x)


def codex_lookup(v, enum_cls, label):
    """config 的 Sandbox/ApprovalMode(名字/值/枚举成员)→ SDK 枚举;None → None。

    调用方常传字符串("full_access" / "auto_review");高级用户可传 SDK 枚举。
    名字与值双匹配 —— 防 str 子类枚举当 str 用后按名匹配失真。
    """
    if v is None:
        return None
    if isinstance(v, enum_cls):
        return v
    raw = codex_val(v)
    for member in enum_cls:
        if raw in (member.name, member.value):
            return member
    raise ValueError(f"codex {label} 取值非法(可选 {[m.name for m in enum_cls]}): {v!r}")


# ---------------------------------------------------------------------------
# codex 隔离 home 装配(CODEX_HOME 目录即隔离边界;零用户级 config.toml/凭证读绕过)
# ---------------------------------------------------------------------------


def codex_home_path() -> str:
    """codex 隔离 home 路径(缺省 ~/.gh-puller/codex-home;显式经 config.codex_home)。

    与 dsh_cordis_path 同位语义:app-server 只读本目录(CODEX_HOME 下 config.toml /
    auth.json / sessions),不读用户 ~/.codex —— 目录即隔离边界;目录稳定(会话持久,
    thread_resume 可用),区别于 dsh_cordis 的 temp+内容哈希(文件名固定 config.toml,
    无法按内容换名,见 codex_home_setup 的内容比对改写)。
    """
    return str(Path.home() / ".gh-puller" / "codex-home")


def _codex_config_toml_content(*, mcp_servers: list[dict] | None = None,
                               web_search: bool = False) -> str:
    """隔离 config.toml 内容:按调用方注入的通用 mcp 服务器描述渲染(组合即隔离边界,
    镜像 dsh 的 _DSH_CORDIS_YAML —— 无用户 model_provider/keys/mcp/hook/高级设置)。

    每条 spec 应带 id/command/args/env_vars(值不内联,每 run 经 CodexConfig.env 注入
    app-server 进程环境,再由 rmcp stdio 启动器按白名单取走)。mcp_servers 空 → 空
    config(无附加工具桌);本层不识别任何具体工具名。
    web_search = True → [features] web_search_request(Codex 内置网络搜索工具;CLI
    默认出于安全关闭,须显式启用 —— 产物隔离边界内的用户级开关)。
    """
    sections = []
    for spec in mcp_servers or []:
        name = spec.get("id", "mcp-server")
        cmd = json.dumps(spec.get("command", ""))
        args = json.dumps(spec.get("args", []))
        env_vars = json.dumps(spec.get("env_vars", []))
        sections.append(
            f"[mcp_servers.{name}]\n"
            f"command = {cmd}\n"
            f"args = {args}\n"
            f"env_vars = {env_vars}\n"
            "startup_timeout_sec = 30\n"
            "required = true\n"
        )
    if web_search:
        sections.append("[features]\nweb_search_request = true\n")
    return "\n".join(sections)


def codex_home_setup(home, *, auth_src: str | Path | None = None,
                     config_path: str | Path | None = None,
                     mcp_servers: list[dict] | None = None,
                     web_search: bool = False) -> str:
    """确保 codex 隔离 home 就绪:config.toml + auth 引导;内容不变不重写。

    config_path(file 类契约,纯透传):home/config.toml 改为指向该文件的**符号链接**
    (与 auth.json 引导同模式:零读取零解析,SDK 经 CODEX_HOME 原生装载所选文件,
    用户全责)。符号链接不可用(windows/文件系统限制)时回落复制。
    缺省(无 config_path):按 mcp_servers(通用描述)渲染 config.toml(空 → 无附加
    工具桌;web_search=True 追加 [features] web_search_request —— user 级开关,
    与 config_path 模式互斥:自定义配置由该文件自定)。
    auth 引导(cc 同形:凭证通道不隔离,隔离只管设置面 —— 见 cc setting_sources=[]):
    home/auth.json 缺失且 auth_src(缺省 ~/.codex/auth.json)存在 → **符号链接**——
    与 cc 的 CLI 自持凭证一样实时引用(重新登录/revoke 即跟随,无副本陈旧);旧副本
    (非符号链接)存在时替换为链接。符号链接不可用(windows/文件系统限制)时回落复制。
    auth_src 传 False 可关闭引导(纯隔离无凭证态);显式 token 走 login_api_key 时
    app-server 自写 home/auth.json(先删符号链接再真写,不冲突)。
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.toml"
    if config_path is not None:
        p = Path(config_path)
        if not p.is_file():  # resolve_target 已校验;防御双检
            raise FileNotFoundError(f"codex 配置文件不存在: {p}")
        if cfg_path.is_symlink() or cfg_path.exists():
            cfg_path.unlink()  # 覆盖写穿悬空链接文件(符号链接须先 unlink)
        try:
            cfg_path.symlink_to(p)
        except OSError:  # windows/不可符号链接 → 复制兜底
            shutil.copyfile(p, cfg_path)
        return str(home)
    content = _codex_config_toml_content(mcp_servers=mcp_servers, web_search=web_search)
    if not cfg_path.exists() or cfg_path.is_symlink() \
            or cfg_path.read_text(encoding="utf-8") != content:
        if cfg_path.is_symlink():  # 上一 config_path 世代残留:必须 unlink 防写穿
            cfg_path.unlink()
        cfg_path.write_text(content, encoding="utf-8")
    auth = home / "auth.json"
    if auth_src is not False and not auth.is_symlink():
        src = Path(auth_src) if auth_src else Path.home() / ".codex" / "auth.json"
        if src.exists():
            if auth.exists() or auth.is_symlink():
                auth.unlink()  # 旧副本(含悬空链接)→ 替换为链接或复制
            try:
                auth.symlink_to(src)
            except OSError:  # windows/不支持符号链接 → 复制兜底(陈旧风险可忽略:覆写即重置)
                shutil.copyfile(src, auth)
    return str(home)


# ---------------------------------------------------------------------------
# dsh 内置隔离组合装配(cordis 文件;SDK/JSON-RPC 只读这一个文件,无用户级 patch 层)
# ---------------------------------------------------------------------------

_DSH_CORDIS_FILE: str | None = None  # 内容哈希键控缓存(每次调用少写盘;内容变了自然换文件名)


def dsh_cordis_path(mcp_servers: list[dict] | None = None) -> str:
    """dsh 内置隔离组合文件路径(DSH_CORDIS_CONFIG 只认文件路径;按内容哈希落盘一次)。

    组合蓝图见下方 `_DSH_CORDIS_YAML`(镜像 dsh 官方 minimal.cordis.yml;SDK/
    JSON-RPC 路径只读这一个文件,无用户级 patch 层/无 XDG 配置 —— 组合即隔离
    边界)。语义同 cc `setting_sources=[]` 的默认位:上层不传 cordis(None)时,
    回退本组合(默认隔离);上层显式提供 cordis(如组合文件缺省路径)则全权交给
    上层(隔离与否由上层组合保证)。
    mcp_servers = 调用方经 config 注入的通用工具桌描述(附加 mcp 服务器段)。
    """
    global _DSH_CORDIS_FILE
    if mcp_servers is None:
        if _DSH_CORDIS_FILE is None:
            _DSH_CORDIS_FILE = _dsh_cordis_write(None)
        return _DSH_CORDIS_FILE
    return _dsh_cordis_write(mcp_servers)  # 带工具桌:按内容哈希直写(不占默认缓存)


def _dsh_cordis_write(mcp_servers: list[dict] | None) -> str:
    """最小隔离组合 + 可选附加 mcp 服务器段(通用描述,内容哈希落盘一次)。

    调用方(上层装配层)以 mcp_servers=[{"id","serverName","command","args",...}]
    注入引擎工具桌 —— 本层只按通用结构渲染,不认识任何具体工具名。
    """
    digest = hashlib.sha1(_DSH_CORDIS_YAML.encode()).hexdigest()[:8]
    text = _DSH_CORDIS_YAML
    for spec in mcp_servers or []:
        text = _dsh_mcp_section(spec) + text
        digest = hashlib.sha1((digest + repr(sorted(spec.items()))).encode()).hexdigest()[:8]
    path = Path(tempfile.gettempdir()) / "gh-puller" / f"dsh-cordis-{digest}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return str(path)


def _dsh_mcp_section(spec: dict) -> str:
    """通用 mcp 服务器描述 → 组合 YAML 段(dsh-mcp-client 行,零具体工具名)。"""
    args = " ".join(f"'{a}'" for a in spec.get("args") or [])
    return (
        f"- id: {spec.get('id', 'mcp-server')}\n"
        f"  name: '@deepseek-ai/dsh-mcp-client'\n"
        f"  config:\n"
        f"    serverName: {spec.get('serverName', '')}\n"
        f"    transport: stdio\n"
        f"    command: {spec.get('command', 'python3')}\n"
        f"    args: [{args}]\n"
        f"    cwd: !!js process.env.DSH_CWD ?? process.cwd()\n"
        f"    failOnStartupError: true\n"
        f"    reconnect:\n      enabled: false\n"
    )


# 组合层隔离逐项(cordis.yml 之外再无配置注入面;余下环境残留 DSH_HOME/之类
# 无任何已装载组件读取 —— SDK 路径无 home patch/settings/credentials 装载):
# workspaceContext(禁 $DSH_HOME/AGENTS.md 与 checkout 的 AGENTS.md/CLAUDE.md 链)、
# skills(禁用户 $DSH_HOME/skills、$DSH_AGENTS_HOME/skills、项目 .dsh/skills 与
# 捆绑技能目录)、includeHarnessIdentity/includeRuntimeContext(提示词注入)、
# toolBash/toolJobs/goals(面模型工具域)。附加工具桌(引擎 MCP 服务器)由调用方经
# mcp_servers 通用描述注入(_dsh_mcp_section 渲染;无 .mcp.json/全局发现)。
# hooks 未装载(配置文件型,须显式挂)。
_DSH_CORDIS_YAML = """- id: sdk-jsonrpc-server
  name: '@deepseek-ai/dsh-sdk-jsonrpc-server'
  config:
    maxTokensAsSuccess: false
- id: llm-deepseek
  name: '@deepseek-ai/dsh-llm-deepseek'
- id: sandbox
  name: '@deepseek-ai/dsh-sandbox-local'
- id: sandbox-policy
  name: '@deepseek-ai/dsh-sandbox-policy'
  config:
    mode: danger-full-access
    workspaceRoot: !!js process.env.DSH_CWD ?? process.cwd()
- id: subprocess
  name: '@deepseek-ai/dsh-subprocess-local'
- id: pty
  name: '@deepseek-ai/dsh-terminal'
- id: terminal-bash
  name: '@deepseek-ai/dsh-terminal-bash'
  config:
    timeoutMs: 300000
- id: fs-local
  name: '@deepseek-ai/dsh-fs-local'
  config:
    cwd: !!js process.env.DSH_CWD ?? process.cwd()
- id: agent-spine
  name: '@deepseek-ai/dsh-agent-spine-demo'
  config:
    includeHarnessIdentity: false
    includeRuntimeContext: false
    persona: !!js process.env.DSH_SYSTEM_PROMPT ?? 'You are a helpful software engineer assistant.'
    workspaceContext: false
    skills:
      enabled: false
    toolBash: false
    toolJobs: false
    goals: false
- id: persistent-bash
  name: '@deepseek-ai/dsh-tool-bash-persistent'
  config:
    timeoutMs: 300000
- id: str-replace-editor
  name: '@deepseek-ai/dsh-tool-str-replace-editor'
  config:
    maxOutputChars: 16000
- id: sessions
  name: '@deepseek-ai/dsh-session-persistence-jsonl'
  config:
    root: !!js process.env.DSH_SESSION_ROOT ?? './.sessions'
    compression: none
"""
