"""包内共享原子:生成器层通用失败异常 + 异常 stage 分类(http/parse/run)。

与具体生成器无关(不识别任何 SDK 类型/协议);各生成器协议错误类在各自文件派生
(见 dsh.py _dsh_stage / codex.py _codex_stage:沿 _stage_of,协议错误归 parse)。
"""

import json
from typing import Any

import httpx


class RequestFailedError(Exception):
    """SDK 层原始失败(detail 为调用方可见的失败原因;文案组合由 dispatch 包装)。"""

    def __init__(self, detail: Any):
        super().__init__(detail)
        self.detail = str(detail)


# ---------------------------------------------------------------------------
# 通用事件辅助:异常 → error 事件 stage 分类(纯函数,零 SDK 依赖)
# ---------------------------------------------------------------------------


def _stage_of(exc: Exception) -> str:
    """error 事件 stage 分类:http(网络/状态码)/ parse(响应结构)/ run(其余)。"""
    if isinstance(exc, httpx.HTTPError):
        return "http"
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)):
        return "parse"
    return "run"
