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
from pathlib import Path
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
    chat_stream,
    delete_wiki_cache,
    ensure_index,
    export_wiki,
    generate_codemap,
    list_processed_projects,
    list_wiki_cache,
    read_wiki_cache,
)
from gh_puller.deepwiki.utils import generator_digest, index_ready, log

# 语言契约(引擎层移出:仅 HTTP 层展示用;提示词语言名在 utils.language_name)
_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "zh": "Mandarin Chinese (中文)",
}
from gh_puller.utils import (
    Repo,
    TaskStatus,
    _event,
    read_repo_file,
    read_repo_file_tree,
)

# HTTP 边界请求/响应模型(wire 契约唯一验证面;引擎零 pydantic)
from schemas import (
    AuthorizationConfig,
    ChatCompletionRequest,
    CodeMapRequest,
    Model,
    ModelConfig,
    ProcessedProjectEntry,
    Provider,
    RepoPrepareRequest,
    RepoRequestBase,
    WikiExportRequest,
    WikiTaskRequest,
    WikiTaskStatus,
    WikiTaskSummary,
)

# 任务 runtime 包装(注册表/任务模型/提交响应):server 侧专属(引擎零任务调度状态)
from tasks import WikiTask, WikiTaskSubmitResult, registry  # noqa: E402

# 语言与模型契约(仅 HTTP 层展示用;_LANGUAGE_NAMES 为引擎侧映射)
_LANG_CONFIG = {"supported_languages": dict(_LANGUAGE_NAMES), "default": "en"}

# target 契约:唯一真源 = agent.GENERATORS(极简 id → 生成器类映射;生成器类零 config
# 元数据 —— configKind 经引擎 utils.config_kind 判别,展示/缺省元数据由本层自持(同
# _LANGUAGE_NAMES 的「仅 HTTP 层展示用」哲学;值沿旧生成器类属性契约,见 test_app.py)。
# generator → generator_config(dict,按 generator 包装):file 类(cc/dsh/codex)
# = {"config_path"};object 类(llm)= {"provider","model","base_url","api_key"}。
# /models/config 为 deepwiki-open 旧契约的投影(标注 deprecated);/generators/config
# 为当前契约(前端唯一真源)。
from gh_puller.agent import GENERATORS as AGENT_GENERATORS
from gh_puller.deepwiki.utils import config_kind, resolve_generator

# /generators/config 前端元数据表(键 = GENERATORS id;file 类无 provider 键,object 类
# 无 configPath 键 —— 键集互斥即类别;configDefault = 配置路径 UI 占位/缺省展示)。
_GENERATOR_META: dict[str, dict] = {
    "cc": {"name": "Claude Code", "capability": "anthropic-agent-api",
           "configPathEnv": "DEEPWIKI_CC_CONFIG",
           "configDefault": str(Path.home() / ".claude" / "settings.json")},
    "dsh": {"name": "DeepSeek Harness", "capability": "deepseek-route",
            "configPathEnv": "DEEPWIKI_DSH_CORDIS", "configDefault": None},
    "codex": {"name": "Codex", "capability": "responses",
              "configPathEnv": "DEEPWIKI_CODEX_CONFIG", "configDefault": None},
    "llm": {"name": "LLM", "capability": "chat-completions", "provider": "openai",
            "modelEnv": "LLM_MODEL", "apiKeyEnv": "OPENAI_API_KEY",
            "baseUrlEnv": "OPENAI_BASE_URL", "baseUrlDefault": "https://api.openai.com/v1",
            "models": ("gpt-5.6-luna", "gpt-5.3-codex", "gpt-5.1-codex"),
            "supportsCustomModel": True},
}


def _models_config() -> ModelConfig:
    """旧 /models/config 契约(object-only 投影,标注 deprecated):file 类的
    provider 由所选配置文件决定(请求无此轴),故只出 object 类(openai)入口。

    defaultProvider = 默认 generator 的"provider"展示值(cc 无此轴 → 空串)。
    """
    providers = []
    for gid in AGENT_GENERATORS:
        meta = _GENERATOR_META[gid]
        if "provider" not in meta:  # file 类:provider 随所选配置文件,不设请求轴
            continue
        providers.append(Provider(
            id=meta["provider"], name=meta["provider"].title(),
            supportsCustomModel=meta["supportsCustomModel"],
            models=[Model(id=mid, name=mid) for mid in meta["models"]],
        ))
    return ModelConfig(
        providers=providers,
        defaultProvider=_GENERATOR_META[envs.DEEPWIKI_GENERATOR].get("provider", ""),
    )


def _generators_config() -> dict:
    """GET /generators/config:注册表直出(前端唯一真源)。

    configKind = "file"(generator_config 只填 config_path;占位取 configPathEnv/
    configDefault)或 "object"(providers 列表 + provider/model 字段)。
    """
    default_gid, default_gc = resolve_generator()  # 空选型 → env 缺省(与运行期解析一致)
    if config_kind(default_gid) == "file":
        default_config: dict = {"config_path": default_gc.get("config_path", "")}
    else:
        default_config = {"provider": _GENERATOR_META[default_gid].get("provider", ""),
                          "model": ""}
    generators, providers = [], []
    for gid in AGENT_GENERATORS:
        meta = _GENERATOR_META[gid]
        kind = config_kind(gid)
        generators.append({
            "id": gid, "name": meta["name"], "configKind": kind,
            "capability": meta["capability"],
            "defaultProvider": meta.get("provider", "") if kind == "object" else "",
            "providers": [meta["provider"]] if kind == "object" else [],
            "defaultModelEnv": meta.get("modelEnv"),
            "configPathEnv": meta.get("configPathEnv"),
            "configDefault": meta.get("configDefault"),
        })
        if "provider" in meta:  # object 类入口(providers 注册表)
            providers.append({
                "id": meta["provider"], "name": meta["provider"].title(),
                "apiKeyEnv": meta.get("apiKeyEnv"),
                "baseUrlEnv": meta.get("baseUrlEnv"),
                "baseUrlDefault": meta.get("baseUrlDefault"),
                "models": list(meta.get("models", ())),
                "supportsCustomModel": meta.get("supportsCustomModel", False),
            })
    return {
        "generators": generators,
        "providers": providers,
        "defaultGenerator": default_gid,
        "defaultTarget": {"generator": default_gid, "generator_config": default_config},
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
        if index_ready(repo):
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
            log(f"仓库索引失败: {exc}")
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
        else:
            yield "event: done\ndata: ok\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def _prepare_index(request: RepoRequestBase) -> dict:
    """索引保障(克隆 + 建图,与 wiki 任务流共用 ensure_index);失败抛异常供 SSE 上报。"""
    repo = Repo(request.repo_url, request.type, access_token=request.token)
    await ensure_index(repo, extra_excludes=_extra_excludes(request))
    return {}


@app.get("/repo/index/status")
async def repo_index_status(
    repo_url: str = Query(..., description="Repository URL or local path"),
    type: str = Query("github", description="Repository type"),
):
    """廉价就绪探针(前端轮询 /repo/index/status,不占用 prepare 流)。"""
    return {"ready": index_ready(Repo(repo_url, type))}


class RepoNotIndexedError(ValueError):
    """chat/codemap 到达时仓库尚未建图(未索引前置校验属端点守卫;建图服务在引擎)。"""


def _require_indexed(repo: Repo) -> None:
    """chat 前置校验:仓库必须已建图,失败在进生成器前即抛
    (WS 发错误文本、HTTP 映射 425,见两处调用点)。"""
    if not index_ready(repo):
        raise RepoNotIndexedError(
            f"仓库尚未索引: {repo.name}。请先通过 /repo/prepare 建立代码图谱。"
        )


# -- chat --------------------------------------------------------------------------------------


def _send_if_connect(websocket: WebSocket, msg: str) -> None:
    if websocket.application_state == WebSocketState.CONNECTED:
        asyncio.create_task(websocket.send_text(msg))


# 经调研:query 失败时 chat 的 WebSocket 端点会向客户端多发送一条错误文本;原后端也是直接发文本 chunk
def _extra_excludes(request: RepoRequestBase) -> list[str] | None:
    """请求的排除目录/文件 → graphify extra_excludes(散装参数形态的边界拼装)。"""
    if request.excluded_dirs or request.excluded_files:
        return [*request.excluded_dirs, *request.excluded_files]
    return None


@app.websocket("/ws/chat")
async def handle_websocket_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        request = ChatCompletionRequest(**await websocket.receive_json())
        repo = Repo(request.repo_url, request.type, access_token=request.token)
        try:
            _require_indexed(repo)
        except RepoNotIndexedError as e:
            await websocket.send_text(str(e))
            return
        async for chunk in chat_stream(
            generator=request.target.get("generator"), generator_config=request.target.get("generator_config"), repo=repo,
            messages=[m.model_dump() for m in request.messages],
            language=request.language, research_iteration=request.research_iteration,
        ):
            if websocket.application_state != WebSocketState.CONNECTED:
                break
            await websocket.send_text(chunk)
    except WebSocketDisconnect:
        log("chat WebSocket 断开")
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
    repo = Repo(request.repo_url, request.type, access_token=request.token)
    try:
        _require_indexed(repo)
    except RepoNotIndexedError as e:
        raise HTTPException(status_code=425, detail=str(e))
    try:
        stream = chat_stream(
            generator=request.target.get("generator"), generator_config=request.target.get("generator_config"), repo=repo,
            messages=[m.model_dump() for m in request.messages],
            language=request.language, research_iteration=request.research_iteration,
        )
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
        repo = Repo(request.repo_url, request.type, access_token=request.token)
        async for event in generate_codemap(
            generator=request.target.get("generator"), generator_config=request.target.get("generator_config"), repo=repo, question=request.question, language=request.language,
        ):
            if websocket.application_state != WebSocketState.CONNECTED:
                break
            await websocket.send_text(event)
    except WebSocketDisconnect:
        log("codemap WebSocket 断开")
    except Exception as e:  # noqa: BLE001
        log(f"codemap 生成异常: {e}")
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_text(_event(type="error", message=str(e)))
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close()


@app.post("/codemap/stream")
async def codemap_stream(request: CodeMapRequest):
    try:
        repo = Repo(request.repo_url, request.type, access_token=request.token)
        stream = generate_codemap(
            generator=request.target.get("generator"), generator_config=request.target.get("generator_config"), repo=repo, question=request.question, language=request.language,
        )
        return StreamingResponse(stream, media_type="application/x-ndjson")
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
    content = export_wiki(
        request.repo_url, pages=[p.model_dump() for p in request.pages],
        format=request.format, timestamp=timestamp,
    )
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
        log(f"local_repo/structure 异常: {e}")
        return JSONResponse(status_code=500, content={"error": f"Error processing local repository: {e}"})


def _query_choice_digest(generator: str, config_path: str, provider: str, model: str) -> str:
    """查询参数(generator + file:config_path / object:provider|model)→ 选型摘要。"""
    gc: dict = {}
    if config_path:
        gc["config_path"] = config_path
    if provider:
        gc["provider"] = provider
    if model:
        gc["model"] = model
    return generator_digest(generator or "", gc)


@app.get("/api/wiki_cache")
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
                                 digest=_query_choice_digest(generator, config_path, provider, model))


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
            or (_query_choice_digest(generator, config_path, provider, model)
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
        return await registry.submit(WikiTask.from_wiki_request(request.model_dump()))
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
            payload = json.dumps(task.to_status())
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
