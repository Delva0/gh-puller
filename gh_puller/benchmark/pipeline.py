"""Run one question bank against one endpoint and persist its opaque verdict."""

import argparse
import asyncio
import importlib.util
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import jsonschema
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gh_puller.benchmark.protocol import ASK_PATH, OPENAPI_PATH, RESPONSE_SCHEMA
from gh_puller.benchmark.types import Answer
from gh_puller.envs import TIMEOUT

RETRY_ATTEMPTS = 3

# --- Results ---


@dataclass
class EligibilityResult:
    valid: bool
    detail: str


@dataclass
class BenchResult:
    name: str
    url: str
    valid: bool
    invalid_reason: str = ""
    judgment: Any = None
    judge_error: str = ""


# --- Question-bank loading ---


def load_bank(path: str | Path) -> ModuleType:
    """Load a question-bank module from any path and validate its ``JUDGE`` export."""
    path = Path(path).resolve()
    if not path.is_file():
        raise SystemExit(f"题库文件不存在：{path}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # Preserve the bank failure as a concise CLI error.
        raise SystemExit(f"题库模块执行出错：{type(e).__name__}: {e}") from e
    if not callable(getattr(module, "JUDGE", None)):
        raise SystemExit(f"题库文件必须导出 JUDGE（async 可调用对象）：{path}")
    return module


# --- Endpoint qualification ---


async def _discover_route(client: httpx.AsyncClient, base_url: str) -> tuple[str, str]:
    """Return route status as ``ok``, ``no_route``, or ``unreachable``."""
    # Prefer an explicit OpenAPI route declaration.
    try:
        r = await client.get(f"{base_url}{OPENAPI_PATH}")
        if r.status_code == 200 and r.json().get("paths", {}).get(ASK_PATH, {}).get("post"):
            return "ok", f"openapi.json 声明了 {ASK_PATH}"
    except (httpx.HTTPError, ValueError):
        pass
    # A non-404 response proves the fallback probe reached an ``/ask`` handler.
    try:
        r = await client.post(f"{base_url}{ASK_PATH}", json={"question": "ping"})
    except httpx.HTTPError:
        return "unreachable", f"无法连接 {base_url}"
    if r.status_code == 404:
        return "no_route", f"{base_url}{ASK_PATH} 返回 404，路由缺失"
    return "ok", f"探测到 {ASK_PATH}（HTTP {r.status_code}）"


async def check_eligibility(client: httpx.AsyncClient, base_url: str) -> EligibilityResult:
    """Qualify an endpoint through route discovery and a schema-checked smoke test."""
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


# --- Participant endpoint ---


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=0.5, max=3),
    reraise=True,
)
async def _post_ask(client: httpx.AsyncClient, base_url: str, question: str) -> dict:
    """Post one question, retrying transport failures but not HTTP errors."""
    r = await client.post(f"{base_url}{ASK_PATH}", json={"question": question}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
    return r.json()


def make_ask_fn(client: httpx.AsyncClient, base_url: str):
    """Bind a participant endpoint to the ``async ask(question)`` bank contract."""

    async def ask(question: str) -> Answer:
        body = await _post_ask(client, base_url, question)
        jsonschema.validate(body, RESPONSE_SCHEMA)
        return Answer(text=body["answer"])

    return ask


# --- Evaluation ---


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
        except Exception as e:  # Preserve judge failures in the result archive.
            print(f"  → 裁判异常：{type(e).__name__}: {e}", flush=True)
            return BenchResult(name, url, True, judge_error=f"{type(e).__name__}: {e}")


# --- Persistence ---


def write_result(result: BenchResult, out_dir: Path) -> Path:
    """Write one result, using ``repr`` for opaque non-JSON verdict values."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "result.json"
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=repr))
    return path


# --- CLI ---


def main() -> None:
    ap = argparse.ArgumentParser(description="单点评测：一个题库文件 + 一个参赛方 endpoint")
    ap.add_argument("bank", type=Path, help="题库文件（导出 JUDGE 的 Python 文件，任意路径，位置参数）")
    ap.add_argument("--url", required=True, help="参赛方 base_url")
    ap.add_argument("--name", help="参赛方名，默认用 url")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("outputs") / datetime.now(UTC).strftime("%Y%m%d_%H%M%S"),
    )
    args = ap.parse_args()

    module = load_bank(args.bank)
    result = asyncio.run(run_benchmark(module, args.url, args.name or args.url))
    path = write_result(result, args.out_dir)
    print(f"结果已写入 {path}", flush=True)


if __name__ == "__main__":
    main()
