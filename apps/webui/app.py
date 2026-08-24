"""DeepWiki 兼容后端 HTTP 服务(FastAPI 端点层)。

契约与 deepwiki-open 一致(前端见 apps/webui/);引擎(Repo/graphify/agent)、
chat/codemap 生成器与 wiki 任务管理全部由 gh_puller.deepwiki 提供,本模块只负责
HTTP/WS 端点适配(SSE 心跳、错误语义、模型配置契约)。

启动:`uv --directory apps/webui/api run uvicorn app:app --port 8001`(或 python app.py)。
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.websockets import WebSocketState

from gh_puller import envs
from gh_puller.deepwiki import (
    AuthorizationConfig,
    ChatCompletionRequest,
    CodeMapRequest,
    Model,
    ModelConfig,
    ProcessedProjectEntry,
    Provider,
    Repo,
    RepoNotIndexedError,
    RepoPrepareRequest,
    RepoRequestBase,
    TaskStatus,
    WikiCacheData,
    WikiExportRequest,
    WikiTask,
    WikiTaskRequest,
    WikiTaskStatus,
    WikiTaskSubmitResult,
    WikiTaskSummary,
    _LANGUAGE_NAMES,
    _event,
    _index_ready,
    _log,
    _require_indexed,
    _run_extract,
    chat_stream,
    delete_wiki_cache,
    export_wiki,
    generate_codemap,
    list_processed_projects,
    list_wiki_cache,
    read_repo_file,
    read_repo_file_tree,
    read_wiki_cache,
    registry,
)

# 语言与模型契约(仅 HTTP 层展示用;_LANGUAGE_NAMES 为引擎侧映射)
_LANG_CONFIG = {"supported_languages": dict(_LANGUAGE_NAMES), "default": "en"}

# 模型配置:仅 claude 单一 provider(与 tools 缺省模型同为 SDK 缺省)
_CLAUDE_MODELS = [
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-opus-5", "Claude Opus 5"),
]


def _models_config() -> ModelConfig:
    return ModelConfig(
        providers=[
            Provider(
                id="claude",
                name="Claude",
                supportsCustomModel=False,
                models=[Model(id=mid, name=name) for mid, name in _CLAUDE_MODELS],
            )
        ],
        defaultProvider="claude",
    )


app = FastAPI(title="Streaming API", description="DeepWiki 兼容后端 (gh-puller)", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """根端点:确认服务在跑。"""
    return {"message": "Welcome to Streaming API", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat(), "service": "deepwiki-api"}


@app.get("/lang/config")
async def lang_config():
    return _LANG_CONFIG


@app.get("/models/config", response_model=ModelConfig)
async def get_model_config():
    return _models_config()


@app.get("/auth/status")
async def get_auth_status():
    """wiki 删除是否需要授权(原语义)。"""
    return {"auth_required": envs.WIKI_AUTH_MODE}


@app.post("/auth/validate")
async def validate_auth_code(request: AuthorizationConfig):
    return {"success": envs.WIKI_AUTH_CODE == request.code}


# -- repo 索引(SSE 心跳,同原 repo.py) ----------------------------------------------------------

_HEARTBEAT_INTERVAL_SEC = 10  # 前端代理/undici bodyTimeout(300s)以内保持连接


@app.post("/repo/prepare")
async def prepare_repo_index(request: RepoPrepareRequest):
    async def event_stream():
        repo = Repo(request.repo_url, request.type, access_token=request.token)
        if _index_ready(repo):
            yield "event: ready\ndata: already indexed\n\n"
            yield "event: done\ndata: ok\n\n"
            return
        yield ": indexing-start\n\n"
        task = asyncio.create_task(_prepare_index(request))
        elapsed = 0
        while not task.done():
            try:
                # shield:心跳超时绝不能取消索引任务
                await asyncio.wait_for(asyncio.shield(task), timeout=_HEARTBEAT_INTERVAL_SEC)
            except asyncio.TimeoutError:
                elapsed += _HEARTBEAT_INTERVAL_SEC
                yield f"event: progress\ndata: {json.dumps({'elapsed_sec': elapsed})}\n\n"
            except Exception:
                break
        exc = task.exception()
        if exc is not None:
            _log(f"仓库索引失败: {exc}")
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
        else:
            yield "event: done\ndata: ok\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def _prepare_index(request: RepoRequestBase) -> dict:
    """克隆(如需) + graphify.extract 建图;失败抛异常供 SSE 上报。"""
    repo = Repo(request.repo_url, request.type, access_token=request.token)
    if not repo.downloaded and not repo.is_local:
        await asyncio.to_thread(repo.download)
    result = await _run_extract(repo, request)
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result


@app.get("/repo/index/status")
async def repo_index_status(
    repo_url: str = Query(..., description="Repository URL or local path"),
    type: str = Query("github", description="Repository type"),
):
    """廉价就绪探针(前端轮询 /repo/index/status,不占用 prepare 流)。"""
    return {"ready": _index_ready(Repo(repo_url, type))}


# -- chat --------------------------------------------------------------------------------------


def _send_if_connect(websocket: WebSocket, msg: str) -> None:
    if websocket.application_state == WebSocketState.CONNECTED:
        asyncio.create_task(websocket.send_text(msg))


# 经调研:query 失败时 chat 的 WebSocket 端点会向客户端多发送一条错误文本;原后端也是直接发文本 chunk
@app.websocket("/ws/chat")
async def handle_websocket_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        request = ChatCompletionRequest(**await websocket.receive_json())
        try:
            _require_indexed(request)
        except RepoNotIndexedError as e:
            await websocket.send_text(str(e))
            return
        async for chunk in chat_stream(request):
            if websocket.application_state != WebSocketState.CONNECTED:
                break
            await websocket.send_text(chunk)
    except WebSocketDisconnect:
        _log("chat WebSocket 断开")
    except ValueError as e:
        _send_if_connect(websocket, f"Error preparing retriever: {e}")
    except Exception as e:  # noqa: BLE001
        _send_if_connect(websocket, f"Error preparing retriever: {e}")
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close()


@app.post("/chat/completions/stream")
async def chat_completions_stream(request: ChatCompletionRequest):
    if not request.messages:
        raise HTTPException(status_code=400, detail="No messages provided")
    if request.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="Last message must be from the user")
    try:
        _require_indexed(request)
    except RepoNotIndexedError as e:
        raise HTTPException(status_code=425, detail=str(e))
    try:
        stream = chat_stream(request)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Error preparing retriever: {str(e)}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Error preparing retriever: {str(e)}")
    return StreamingResponse(stream, media_type="text/event-stream")


# -- codemap -----------------------------------------------------------------------------------

@app.websocket("/ws/codemap")
async def handle_websocket_codemap(websocket: WebSocket):
    await websocket.accept()
    try:
        request = CodeMapRequest(**await websocket.receive_json())
        async for event in generate_codemap(request):
            if websocket.application_state != WebSocketState.CONNECTED:
                break
            await websocket.send_text(event)
    except WebSocketDisconnect:
        _log("codemap WebSocket 断开")
    except Exception as e:  # noqa: BLE001
        _log(f"codemap 生成异常: {e}")
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_text(_event(type="error", message=str(e)))
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close()


@app.post("/codemap/stream")
async def codemap_stream(request: CodeMapRequest):
    try:
        return StreamingResponse(generate_codemap(request), media_type="application/x-ndjson")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/codemap/file")
async def codemap_file(
    repo_url: str = Query(..., description="Repository URL or local path"),
    file_path: str = Query(..., description="Repository-relative file path"),
    type: str = Query("github", description="Repository type"),
):
    try:
        return {"file_path": file_path, "content": read_repo_file(repo_url, type, file_path)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


# -- wiki 任务与缓存 -----------------------------------------------------------------------------


@app.post("/export/wiki")
async def post_export_wiki(request: WikiExportRequest):
    repo_parts = request.repo_url.rstrip("/").split("/")
    repo_name = repo_parts[-1] if repo_parts else "wiki"
    timestamp = datetime.now()
    content = export_wiki(request.repo_url, pages=request.pages, format=request.format, timestamp=timestamp)
    filename = f"{repo_name}_wiki_{timestamp.strftime('%Y%m%d_%H%M%S')}"
    if request.format == "markdown":
        filename += ".md"
        media_type = "text/markdown"
    else:
        filename += ".json"
        media_type = "application/json"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/local_repo/structure")
async def get_local_repo_structure(path: str | None = Query(None, description="Path to local repository")):
    if not path:
        return JSONResponse(status_code=400, content={"error": "No path provided. Please provide a 'path' query parameter."})
    if not os.path.isdir(path):
        return JSONResponse(status_code=404, content={"error": f"Directory not found: {path}"})
    try:
        file_tree_lines, readme_content = read_repo_file_tree(path)
        return {"file_tree": "\n".join(sorted(file_tree_lines)), "readme": readme_content}
    except Exception as e:  # noqa: BLE001
        _log(f"local_repo/structure 异常: {e}")
        return JSONResponse(status_code=500, content={"error": f"Error processing local repository: {e}"})


@app.get("/api/wiki_cache", response_model=WikiCacheData | None)
async def read_wiki(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g. github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
):
    if language not in _LANG_CONFIG["supported_languages"]:
        language = _LANG_CONFIG["default"]
    return await read_wiki_cache(owner, repo, repo_type, language)


@app.delete("/api/wiki_cache")
async def delete_wiki(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g. github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
    authorization_code: str | None = Query(None, description="Authorization code"),
):
    if language not in _LANG_CONFIG["supported_languages"]:
        raise HTTPException(status_code=400, detail="Language is not supported")
    if envs.WIKI_AUTH_MODE:
        if not authorization_code or envs.WIKI_AUTH_CODE != authorization_code:
            raise HTTPException(status_code=401, detail="Authorization code is invalid")
    try:
        deleted = await delete_wiki_cache(owner, repo, repo_type, language)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to delete wiki cache: {e}")
    if deleted:
        return {"message": f"Wiki cache for {owner}/{repo} ({language}) deleted successfully"}
    raise HTTPException(status_code=404, detail="Wiki cache not found")


@app.get("/api/processed_projects", response_model=list[ProcessedProjectEntry])
async def get_processed_projects():
    try:
        return await list_processed_projects()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="Failed to list processed projects from server cache.")


@app.post("/wiki/tasks", response_model=WikiTaskSubmitResult)
async def submit_wiki_task(request: WikiTaskRequest):
    """提交任务(get-or-create):created / joined / from_cache 三态(同原)。"""
    return await registry.submit(WikiTask.from_wiki_request(request))


@app.get("/wiki/tasks", response_model=list[WikiTaskSummary])
async def list_wiki_tasks(
    status: Literal["active", "completed", None] = Query(None),
):
    active = sorted(registry.active(), key=lambda t: t.submitted_at)
    active_summaries = [t.to_summary() for t in active]
    if status == "active":
        return active_summaries
    completed = await list_wiki_cache()
    if status == "completed":
        return completed
    return completed + active_summaries


@app.get("/wiki/tasks/{task_id}", response_model=WikiTaskStatus)
async def get_wiki_task(task_id: str):
    task = registry.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.to_status()


@app.get("/wiki/tasks/{task_id}/stream")
async def stream_wiki_task(task_id: str):
    if registry.get(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_stream():
        while True:
            task = registry.get(task_id)
            if task is None:
                yield 'event: error\ndata: {"error": "task no longer available"}\n\n'
                return
            payload = task.to_status().model_dump_json()
            if task.status == TaskStatus.COMPLETED:
                yield f"event: done\ndata: {payload}\n\n"
                return
            if task.status == TaskStatus.FAILED:
                yield f"event: error\ndata: {payload}\n\n"
                return
            yield f"event: progress\ndata: {payload}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


def main() -> None:
    """uvicorn 启动入口:`uv --directory apps/webui/api run uvicorn app:app` 或模块直跑。"""
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=envs.PORT)


if __name__ == "__main__":
    main()
