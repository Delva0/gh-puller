"""DeepWiki 兼容后端 HTTP 服务(FastAPI 端点层)。

契约与 deepwiki-open 一致(前端见 apps/deepwiki-webui/web/);引擎(Repo/graphify/agent)、
chat/codemap 生成器与 wiki 任务管理全部由 gh_puller.deepwiki 提供,本模块只负责
HTTP/WS 端点适配(SSE 心跳、错误语义、模型配置契约)。

启动:`cd apps/deepwiki-webui/server && uv run uvicorn app:app --port 8001`(或 python app.py)。
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Literal

from dotenv import load_dotenv

# 必须先于任何 gh_puller 导入:envs.py 在导入时单点快照环境变量。
# load_dotenv() 自 cwd 向上找 .env(仓库根,整树仅此一份);override=False,不覆盖已设变量。
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.websockets import WebSocketState
from gh_puller import envs
from gh_puller.deepwiki import (
    _LANGUAGE_NAMES,
    AuthorizationConfig,
    ChatCompletionRequest,
    CodeMapRequest,
    Model,
    ModelConfig,
    ProcessedProjectEntry,
    Provider,
    RepoNotIndexedError,
    RepoPrepareRequest,
    RepoRequestBase,
    WikiCacheData,
    WikiExportRequest,
    WikiTask,
    WikiTaskRequest,
    WikiTaskStatus,
    WikiTaskSubmitResult,
    WikiTaskSummary,
    _index_ready,
    _log,
    _require_indexed,
    _request_digest,
    _run_extract,
    chat_stream,
    delete_wiki_cache,
    export_wiki,
    generate_codemap,
    list_processed_projects,
    list_wiki_cache,
    read_wiki_cache,
    registry,
)
from gh_puller.utils import (
    Repo,
    TaskStatus,
    _event,
    read_repo_file,
    read_repo_file_tree,
)

# 语言与模型契约(仅 HTTP 层展示用;_LANGUAGE_NAMES 为引擎侧映射)
_LANG_CONFIG = {"supported_languages": dict(_LANGUAGE_NAMES), "default": "en"}

# target 契约:唯一真源 = agent.GENERATORS(极简 id → 生成器类实例映射;类型信息在
# 各生成器类属性)。generator → generator_config(dict,按 generator 包装):file 类
# (cc/dsh/codex)= {"config_path"};object 类(llm)= {"provider","model","base_url","api_key"}。
# /models/config 为 deepwiki-open 旧契约的投影(标注 deprecated);/generators/config
# 为当前契约(前端唯一真源)。
from gh_puller.agent import GENERATORS as AGENT_GENERATORS


def _models_config() -> ModelConfig:
    """旧 /models/config 契约(object-only 投影,标注 deprecated):file 类的
    provider 由所选配置文件决定(请求无此轴),故只出 object 类(openai)入口。

    defaultProvider = 默认 generator 的"provider"展示值(cc 无此轴 → 空串)。
    """
    return ModelConfig(
        providers=[
            Provider(
                id=gen.provider,
                name=gen.provider.title(),
                supportsCustomModel=getattr(gen, "supports_custom_model", False),
                models=[Model(id=mid, name=mid) for mid in getattr(gen, "models", ())],
            )
            for gen in AGENT_GENERATORS.values()
            if gen.config_kind == "object"
        ],
        defaultProvider=getattr(AGENT_GENERATORS[envs.DEEPWIKI_GENERATOR], "provider", ""),
    )


def _generators_config() -> dict:
    """GET /generators/config:生成器类属性直出(前端唯一真源)。

    configKind = "file"(generator_config 只填 config_path;占位取 configPathEnv/
    configDefault)或 "object"(providers 列表 + provider/model 字段)。
    """
    default_gen = AGENT_GENERATORS[envs.DEEPWIKI_GENERATOR]
    if default_gen.config_kind == "file":
        default_config: dict = {"config_path": default_gen.config_default or ""}
    else:
        default_config = {"provider": default_gen.provider, "model": ""}
    return {
        "generators": [
            {"id": gen.id, "name": gen.name, "configKind": gen.config_kind,
             "capability": gen.capability,
             "defaultProvider": gen.provider if gen.config_kind == "object" else "",
             "providers": [gen.provider] if gen.config_kind == "object" else [],
             "defaultModelEnv": getattr(gen, "model_env", None),
             "configPathEnv": getattr(gen, "config_path_env", None),
             "configDefault": getattr(gen, "config_default", None)}
            for gen in AGENT_GENERATORS.values()
        ],
        "providers": [
            {"id": gen.provider, "name": gen.provider.title(),
             "apiKeyEnv": getattr(gen, "api_key_env", None),
             "baseUrlEnv": getattr(gen, "base_url_env", None),
             "baseUrlDefault": getattr(gen, "base_url_default", None),
             "models": list(getattr(gen, "models", ())),
             "supportsCustomModel": getattr(gen, "supports_custom_model", False)}
            for gen in AGENT_GENERATORS.values() if gen.config_kind == "object"
        ],
        "defaultGenerator": envs.DEEPWIKI_GENERATOR,
        "defaultTarget": {"generator": envs.DEEPWIKI_GENERATOR,
                          "generator_config": default_config},
    }


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


@app.get("/generators/config")
async def get_generators_config():
    """统一 target 契约配置(注册表直出):前端 Generator→Provider→Model 选择器唯一真源。"""
    return _generators_config()


@app.get("/auth/status")
async def get_auth_status():
    """wiki 删除是否需要授权(原语义)。"""
    return {"auth_required": envs.WIKI_AUTH_MODE}


@app.post("/auth/validate")
async def validate_auth_code(request: AuthorizationConfig):
    return {"success": request.code == envs.WIKI_AUTH_CODE}


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
            except TimeoutError:
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


def _target_digest_query(generator: str, config_path: str, provider: str, model: str) -> str:
    """target 查询参数(generator + file:config_path / object:provider|model)→ 缓存摘要。"""
    gc: dict = {}
    if config_path:
        gc["config_path"] = config_path
    if provider:
        gc["provider"] = provider
    if model:
        gc["model"] = model
    return _request_digest({"generator": generator or "", "generator_config": gc})


@app.get("/api/wiki_cache", response_model=WikiCacheData | None)
async def read_wiki(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g. github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
    generator: str = Query("", description="Generator id (cc/dsh/codex/llm) — public target"),
    config_path: str = Query("", description="Config file path (file kind) — public target"),
    provider: str = Query("", description="Provider id (object kind) — public target"),
    model: str = Query("", description="Model id (object kind) — public target"),
):
    if language not in _LANG_CONFIG["supported_languages"]:
        language = _LANG_CONFIG["default"]
    return await read_wiki_cache(owner, repo, repo_type, language,
                                 digest=_target_digest_query(generator, config_path, provider, model))


@app.delete("/api/wiki_cache")
async def delete_wiki(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g. github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
    authorization_code: str | None = Query(None, description="Authorization code"),
    generator: str = Query("", description="Generator id (cc/dsh/codex/llm) — public target"),
    config_path: str = Query("", description="Config file path (file kind) — public target"),
    provider: str = Query("", description="Provider id (object kind) — public target"),
    model: str = Query("", description="Model id (object kind) — public target"),
    digest: str = Query("", description="公开 target 摘要(列尾 digest8;缺省=摘要缺省/旧格式)"),
):
    if language not in _LANG_CONFIG["supported_languages"]:
        raise HTTPException(status_code=400, detail="Language is not supported")
    if envs.WIKI_AUTH_MODE:
        if not authorization_code or authorization_code != envs.WIKI_AUTH_CODE:
            raise HTTPException(status_code=401, detail="Authorization code is invalid")
    try:
        target_digest = (
            digest
            or (_target_digest_query(generator, config_path, provider, model)
                if (generator or config_path or provider or model) else "")
        )
        deleted = await delete_wiki_cache(
            owner, repo, repo_type, language,
            digest=target_digest,
        )
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
    """提交任务(get-or-create):created / joined / from_cache 三态(同原)。

    target 校验(dict 键集白名单/非法组合/配置文件不存在)在 resolve_target
    运行前即抛 ValueError → 400(带具体消息,不是 500)。
    """
    try:
        return await registry.submit(WikiTask.from_wiki_request(request))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    """uvicorn 启动入口:`cd apps/deepwiki-webui/server && uv run uvicorn app:app` 或模块直跑。"""
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=envs.PORT)


if __name__ == "__main__":
    main()
