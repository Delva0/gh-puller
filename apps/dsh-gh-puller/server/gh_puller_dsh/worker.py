"""NDJSON stdio 常驻 worker:dsh 插件经 uv run 拉起,保持图查询进程在线(摊薄 Python import)。

协议:stdin 逐行一个请求 JSON {id, action: "query"|"index", ...args};stdout 逐行
一个响应 {id, ok: true, text} / {id, ok: false, error}。stdout 只走协议帧,一切
日志与库的 print 均被 redirect 到 stderr(worker 不向外写其他字节)。

请求按序串行处理(v1;extract/query 均为 CPU/盘绑定,图加载成本已由常驻摊薄)。
"""

import asyncio
import contextlib
import json
import sys

from dotenv import load_dotenv

load_dotenv()  # 必须在任何 gh_puller 导入之前(envs.py 导入时快照)

from gh_puller_dsh import core  # noqa: E402


def handle_request(req: dict) -> dict:
    """单个请求(同步执行,可被测试直接调用);未知 action → 错误响应。"""
    rid = req.get("id")
    action = req.get("action")
    try:
        if action == "query":
            text = core.query_text(
                req.get("question", ""), req.get("repo"), req.get("repo_type"), req.get("default_graph")
            )
        elif action == "index":
            text = core.index_text(req.get("path", ""), req.get("repo_type"))
        else:
            return {"id": rid, "ok": False, "error": f"unknown action: {action!r}"}
        return {"id": rid, "ok": True, "text": text}
    except Exception as exc:  # noqa: BLE001 - 协议层兜底:任何异常都转错误帧而非杀进程
        return {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def handle_line(line: str) -> str | None:
    """一行请求 → 一行响应;空行/坏 JSON 忽略(无 id 可关联,协议层容错)。"""
    line = line.strip()
    if not line:
        return None
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        return None
    return json.dumps(handle_request(req), ensure_ascii=False)


async def _loop(readline, write) -> None:
    """协议主循环:读 stdin 行 → 线程池执行处理 → 写响应行;EOF 退出。"""
    while True:
        line = await asyncio.to_thread(readline)
        if not line:
            return
        resp = await asyncio.to_thread(handle_line, line)
        if resp is not None:
            await asyncio.to_thread(write, resp)


def main() -> int:
    """console entry:stdout 通道先行捕获再重定向,协议帧与库输出严格隔离。"""
    out = sys.stdout
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    out.reconfigure(encoding="utf-8", errors="replace")

    def write(resp: str) -> None:
        out.write(resp + "\n")
        out.flush()

    async def _run() -> None:
        await _loop(sys.stdin.readline, write)

    with contextlib.redirect_stdout(sys.stderr), contextlib.redirect_stderr(sys.stderr):
        asyncio.run(_run())
    return 0
