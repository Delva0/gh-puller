"""评测管线（单点评测）：导入题库模块 → 资格检查 → 注入 ask → 收集 judgment → 单文件存档。

一次运行 = 一个题库文件（--bank）+ 一个参赛方 endpoint（--url）。
框架只认识三样东西：ask 接口签名、题库导出的 JUDGE、judge 返回的 judgment（原样存档）。
题目形态、参考答案、评判逻辑、输出结构——全部由题库（出题人）自拟，框架零认知。
"""

import argparse
import asyncio
import importlib.util
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import jsonschema
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gh_puller.benchmark.types import Answer

# ---------- 协议（改协议只改这一节） ----------
ASK_PATH = "/ask"  # 唯一被测试的路由
OPENAPI_PATH = "/openapi.json"  # 路由声明的读取入口
TIMEOUT = 3600.0  # 单题超时（秒）
RETRY_ATTEMPTS = 3  # 连接类错误的重试次数
RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "minLength": 1}},
    "additionalProperties": True,  # 容忍未知字段，保证协议前向兼容
}

# ---------- 数据模型 ----------


@dataclass
class EligibilityResult:
    valid: bool  # 端口是否合法（通过资格检查）
    detail: str  # 探测诊断信息（终端展示用）


@dataclass
class BenchResult:
    name: str
    url: str
    valid: bool
    invalid_reason: str = ""  # 非法原因（出局理由）；合法时为空
    judgment: Any = None  # judge 原样输出；裁判异常时为空
    judge_error: str = ""


# ---------- 题库模块导入（插件式） ----------


def load_bank(path: str | Path) -> ModuleType:
    """导入用户题库文件（任意路径），仅取 JUDGE，不碰任何题目数据。"""
    path = Path(path).resolve()
    if not path.is_file():
        raise SystemExit(f"题库文件不存在：{path}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # 出题人代码出错，给出清晰报错
        raise SystemExit(f"题库模块执行出错：{type(e).__name__}: {e}") from e
    if not callable(getattr(module, "JUDGE", None)):
        raise SystemExit(f"题库文件必须导出 JUDGE（async 可调用对象）：{path}")
    return module


# ---------- 路由探测与资格检查 ----------


async def _discover_route(client: httpx.AsyncClient, base_url: str) -> tuple[str, str]:
    """探测 /ask 是否存在，返回 (status, detail)，status ∈ ok | no_route | unreachable。"""
    # 优先读 openapi.json 声明
    try:
        r = await client.get(f"{base_url}{OPENAPI_PATH}")
        if r.status_code == 200 and r.json().get("paths", {}).get(ASK_PATH, {}).get("post"):
            return "ok", f"openapi.json 声明了 {ASK_PATH}"
    except (httpx.HTTPError, ValueError):
        pass
    # fallback：向 /ask 发探测请求；404 视为路由缺失，其余 4xx 视为路由存在（拒收探测请求）
    try:
        r = await client.post(f"{base_url}{ASK_PATH}", json={"question": "ping"})
    except httpx.HTTPError:
        return "unreachable", f"无法连接 {base_url}"
    if r.status_code == 404:
        return "no_route", f"{base_url}{ASK_PATH} 返回 404，路由缺失"
    return "ok", f"探测到 {ASK_PATH}（HTTP {r.status_code}）"


async def check_eligibility(client: httpx.AsyncClient, base_url: str) -> EligibilityResult:
    """两步资格检查：路由探测 + 冒烟测试。任一失败即取消参赛资格。"""
    status, detail = await _discover_route(client, base_url)
    if status != "ok":
        return EligibilityResult(False, detail)
    try:
        r = await client.post(f"{base_url}{ASK_PATH}", json={"question": "ping"}, timeout=TIMEOUT)
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return EligibilityResult(False, f"冒烟测试失败：{type(e).__name__}: {e}")
    if r.status_code != 200:
        return EligibilityResult(False, f"冒烟测试返回 HTTP {r.status_code}")
    try:
        jsonschema.validate(body, RESPONSE_SCHEMA)
    except jsonschema.ValidationError as e:
        return EligibilityResult(False, f"冒烟测试响应不合规：{e.message}")
    return EligibilityResult(True, detail)


# ---------- ask 封装（参赛方接口注入） ----------


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=0.5, max=3),
    reraise=True,
)
async def _post_ask(client: httpx.AsyncClient, base_url: str, question: str) -> dict:
    """发单题请求；仅连接类错误（含超时）重试，HTTP 错误不重试。"""
    r = await client.post(f"{base_url}{ASK_PATH}", json={"question": question}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
    return r.json()


def make_ask_fn(client: httpx.AsyncClient, base_url: str):
    """参赛方接口封装：async ask(question) -> Answer；异常向上抛，由 judge 自行处理。"""

    async def ask(question: str) -> Answer:
        body = await _post_ask(client, base_url, question)
        jsonschema.validate(body, RESPONSE_SCHEMA)
        return Answer(text=body["answer"])

    return ask


# ---------- 评测 ----------


async def run_benchmark(module: ModuleType, url: str, name: str) -> BenchResult:
    judge = module.JUDGE
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        print(f"[资格检查] {name}（{url}）", flush=True)
        elig = await check_eligibility(client, url)
        if not elig.valid:
            print(f"  → 非法端口，取消参赛资格：{elig.detail}", flush=True)
            return BenchResult(name, url, False, invalid_reason=elig.detail)
        print(f"  → 通过（{elig.detail}）", flush=True)
        print(f"[评测] 注入 ask 接口，交由 {module.__name__}.JUDGE", flush=True)
        try:
            judgment = await judge(make_ask_fn(client, url))
            return BenchResult(name, url, True, judgment=judgment)
        except Exception as e:  # 裁判异常：评测仍完成并出存档
            print(f"  → 裁判异常：{type(e).__name__}: {e}", flush=True)
            return BenchResult(name, url, True, judge_error=f"{type(e).__name__}: {e}")


# ---------- 单文件存档 ----------


def write_result(result: BenchResult, out_dir: Path) -> Path:
    """judge 完成后单对象序列化存档；judgment 不可序列化时兜底转 repr。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"result_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=repr))
    return path


# ---------- CLI ----------


def main() -> None:
    ap = argparse.ArgumentParser(description="单点评测：一个题库文件 + 一个参赛方 endpoint")
    ap.add_argument("bank", type=Path, help="题库文件（导出 JUDGE 的 Python 文件，任意路径，位置参数）")
    ap.add_argument("--url", required=True, help="参赛方 base_url")
    ap.add_argument("--name", help="参赛方名，默认用 url")
    ap.add_argument("--out", type=Path, default=Path("."))  # 默认输出到当前目录
    args = ap.parse_args()

    module = load_bank(args.bank)
    result = asyncio.run(run_benchmark(module, args.url, args.name or args.url))
    path = write_result(result, args.out)
    print(f"结果已写入 {path}", flush=True)


if __name__ == "__main__":
    main()
