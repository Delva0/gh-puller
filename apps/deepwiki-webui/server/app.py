"""Serve the DeepWiki-compatible FastAPI boundary.

The engine supplies chat, codemap, and wiki operations. This module adapts HTTP,
WebSocket, SSE, errors, and generator metadata; ``generators`` owns index and MCP
runtime assembly.
"""

import asyncio
import contextlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv

# Load the repository .env before ``gh_puller.envs`` captures its import-time snapshot.
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect  # noqa: E402 - Load env first.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402 - Load env first.
from fastapi.responses import JSONResponse, Response, StreamingResponse  # noqa: E402 - Load env first.
from fastapi.websockets import WebSocketState  # noqa: E402 - Load env first.
from gh_puller.deepwiki import (  # noqa: E402 - Load env first.
    chat_stream,
    delete_wiki_cache,
    export_wiki,
    generate_codemap,
    list_processed_projects,
    list_wiki_cache,
    read_wiki_cache,
)
from gh_puller.deepwiki.utils import generator_digest, log  # noqa: E402 - Load env first.

from generators import ensure_index, index_ready, runtime_config  # noqa: E402 - Load env first.

# --- Application configuration ---

_WIKI_AUTH_MODE = os.environ.get("DEEPWIKI_AUTH_MODE", "False").lower() in ["true", "1", "t"]
_WIKI_AUTH_CODE = os.environ.get("DEEPWIKI_AUTH_CODE", "")
_PORT = int(os.environ.get("PORT", "8001"))
_DEEPWIKI_GENERATOR = os.environ.get("DEEPWIKI_GENERATOR", "cc")

_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "zh": "Mandarin Chinese (中文)",
}
from gh_puller.utils import (  # noqa: E402 - Load env first.
    Repo,
    TaskStatus,
    _event,
    read_repo_file,
    read_repo_file_tree,
)

from schemas import (  # noqa: E402 - Load env first.
    AuthorizationConfig,
    ChatCompletionRequest,
    CodeMapRequest,
    ProcessedProjectEntry,
    RepoPrepareRequest,
    RepoRequestBase,
    WikiExportRequest,
    WikiTaskRequest,
    WikiTaskStatus,
    WikiTaskSummary,
)
from tasks import WikiTask, WikiTaskSubmitResult, registry  # noqa: E402 - Load env first.

_LANG_CONFIG = {"supported_languages": dict(_LANGUAGE_NAMES), "default": "en"}

from gh_puller.agent import AGENTS  # noqa: E402 - Load env first.
from gh_puller.deepwiki.utils import resolve_generator  # noqa: E402 - Load env first.

# File-backed generators expose ``configDefault``; object-backed generators expose a provider.
_GENERATOR_META: dict[str, dict] = {
    "cc": {"name": "Claude Code", "capability": "anthropic-agent-api",
           "configDefault": str(Path.home() / ".claude" / "settings.json")},
    "dsh": {"name": "DeepSeek Harness", "capability": "deepseek-route",
            "configDefault": None},
    "codex": {"name": "Codex", "capability": "responses",
              "configDefault": None},
    "opencode": {"name": "OpenCode", "capability": "opencode-cli",
                 "configDefault": None},
}


def _generators_config() -> dict:
    """Project the Agent registry into the frontend's generator metadata contract."""
    default_gid, default_gc = resolve_generator(_DEEPWIKI_GENERATOR)
    if "configDefault" in _GENERATOR_META[default_gid]:
        default_config: dict = {"config_path": default_gc.get("config_path", "")}
    else:
        default_config = {"provider": _GENERATOR_META[default_gid].get("provider", ""),
                          "model": ""}
    generators, providers = [], []
    for gid in AGENTS:
        meta = _GENERATOR_META.get(gid)
        if meta is None:
            continue
        kind = "file" if "configDefault" in meta else "object"
        generators.append({
            "id": gid, "name": meta["name"], "configKind": kind,
            "capability": meta["capability"],
            "defaultProvider": meta.get("provider", "") if kind == "object" else "",
            "providers": [meta["provider"]] if kind == "object" else [],
            "defaultModelEnv": meta.get("modelEnv"),
            "configDefault": meta.get("configDefault"),
        })
        if "provider" in meta:
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


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        yield
    finally:
        await registry.shutdown()


app = FastAPI(
    title="Streaming API",
    description="DeepWiki-compatible backend (gh-puller)",
    version="0.1.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Confirm that the service is running."""
    return {"message": "Welcome to Streaming API", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat(), "service": "deepwiki-api"}  # noqa: DTZ005 - Health displays local wall time.


@app.get("/lang/config")
async def lang_config():
    return _LANG_CONFIG


@app.get("/generators/config")
async def get_generators_config():
    """Return the canonical generator, provider, and model selector configuration."""
    return _generators_config()


@app.get("/auth/status")
async def get_auth_status():
    """Return whether wiki deletion requires authorization."""
    return {"auth_required": _WIKI_AUTH_MODE}


@app.post("/auth/validate")
async def validate_auth_code(request: AuthorizationConfig):
    return {"success": request.code == _WIKI_AUTH_CODE}


# --- Repository indexing ---

_HEARTBEAT_INTERVAL_SEC = 10


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
                # A heartbeat timeout must not cancel the underlying index build.
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
    """Ensure checkout and index exist, propagating failures to the SSE endpoint."""
    repo = Repo(request.repo_url, request.type, access_token=request.token)
    await ensure_index(repo)
    return {}


@app.get("/repo/index/status")
async def repo_index_status(
    repo_url: Annotated[str, Query(description="Repository URL or local path")],
    type: Annotated[str, Query(description="Repository type")] = "github",  # noqa: A002 - Public API name.
):
    """Return index readiness without occupying the preparation stream."""
    return {"ready": index_ready(Repo(repo_url, type))}


class RepoNotIndexedError(ValueError):
    """Report a chat or codemap request made before indexing."""


def _require_indexed(repo: Repo) -> None:
    """Reject generation before it can enter an Agent without an index."""
    if not index_ready(repo):
        raise RepoNotIndexedError(
            f"仓库尚未索引: {repo.name}。请先通过 /repo/prepare 建立代码图谱。",
        )


# --- Chat ---


# Retain fire-and-forget sends until their completion callbacks release them.
_pending_sends: set[asyncio.Task] = set()


def _send_if_connect(websocket: WebSocket, msg: str) -> None:
    if websocket.application_state == WebSocketState.CONNECTED:
        task = asyncio.create_task(websocket.send_text(msg))
        _pending_sends.add(task)
        task.add_done_callback(_pending_sends.discard)


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
            generator=request.target.get("generator") or _DEEPWIKI_GENERATOR,
            generator_config=runtime_config(
                request.target.get("generator") or _DEEPWIKI_GENERATOR,
                request.target.get("generator_config"), repo=repo,
            ), repo=repo,
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
    except Exception as e:
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
        raise HTTPException(status_code=425, detail=str(e)) from e
    try:
        stream = chat_stream(
            generator=request.target.get("generator") or _DEEPWIKI_GENERATOR,
            generator_config=runtime_config(
                request.target.get("generator") or _DEEPWIKI_GENERATOR,
                request.target.get("generator_config"), repo=repo,
            ), repo=repo,
            messages=[m.model_dump() for m in request.messages],
            language=request.language, research_iteration=request.research_iteration,
        )
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Error preparing retriever: {e!s}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error preparing retriever: {e!s}") from e
    return StreamingResponse(stream, media_type="text/event-stream")


# --- Codemap ---

@app.websocket("/ws/codemap")
async def handle_websocket_codemap(websocket: WebSocket):
    await websocket.accept()
    try:
        request = CodeMapRequest(**await websocket.receive_json())
        repo = Repo(request.repo_url, request.type, access_token=request.token)
        try:
            _require_indexed(repo)
        except RepoNotIndexedError as e:
            await websocket.send_text(_event(type="error", stage="analyzing", message=str(e)))
            return
        async for event in generate_codemap(
            generator=request.target.get("generator") or _DEEPWIKI_GENERATOR,
            generator_config=runtime_config(
                request.target.get("generator") or _DEEPWIKI_GENERATOR,
                request.target.get("generator_config"), repo=repo,
            ), repo=repo,
            question=request.question, language=request.language,
        ):
            if websocket.application_state != WebSocketState.CONNECTED:
                break
            await websocket.send_text(event)
    except WebSocketDisconnect:
        log("codemap WebSocket 断开")
    except Exception as e:
        log(f"codemap 生成异常: {e}")
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_text(_event(type="error", message=str(e)))
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close()


@app.post("/codemap/stream")
async def codemap_stream(request: CodeMapRequest):
    repo = Repo(request.repo_url, request.type, access_token=request.token)
    try:
        _require_indexed(repo)
    except RepoNotIndexedError as e:
        raise HTTPException(status_code=425, detail=str(e)) from e
    try:
        stream = generate_codemap(
            generator=request.target.get("generator") or _DEEPWIKI_GENERATOR,
            generator_config=runtime_config(
                request.target.get("generator") or _DEEPWIKI_GENERATOR,
                request.target.get("generator_config"), repo=repo,
            ), repo=repo,
            question=request.question, language=request.language,
        )
        return StreamingResponse(stream, media_type="application/x-ndjson")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/codemap/file")
async def codemap_file(
    repo_url: Annotated[str, Query(description="Repository URL or local path")],
    file_path: Annotated[str, Query(description="Repository-relative file path")],
    type: Annotated[str, Query(description="Repository type")] = "github",  # noqa: A002 - Public API name.
):
    try:
        return {"file_path": file_path, "content": read_repo_file(repo_url, type, file_path)}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# --- Wiki tasks and cache ---


@app.post("/export/wiki")
async def post_export_wiki(request: WikiExportRequest):
    repo_parts = request.repo_url.rstrip("/").split("/")
    repo_name = repo_parts[-1] if repo_parts else "wiki"
    timestamp = datetime.now()  # noqa: DTZ005 - Export names and content use local wall time.
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
async def get_local_repo_structure(path: Annotated[str | None, Query(description="Path to local repository")] = None):
    if not path:
        return JSONResponse(
            status_code=400,
            content={"error": "No path provided. Please provide a 'path' query parameter."},
        )
    if not os.path.isdir(path):  # noqa: ASYNC240 - A local stat does not warrant async indirection.
        return JSONResponse(status_code=404, content={"error": f"Directory not found: {path}"})
    try:
        file_tree_lines, readme_content = read_repo_file_tree(path)
        return {"file_tree": "\n".join(sorted(file_tree_lines)), "readme": readme_content}
    except Exception as e:
        log(f"local_repo/structure 异常: {e}")
        return JSONResponse(status_code=500, content={"error": f"Error processing local repository: {e}"})


def _query_choice_digest(generator: str, config_path: str, provider: str, model: str) -> str:
    """Derive the same target digest for query and submission representations."""
    gc: dict = {}
    if config_path:
        gc["config_path"] = config_path
    if provider:
        gc["provider"] = provider
    if model:
        gc["model"] = model
    return generator_digest(generator or _DEEPWIKI_GENERATOR, gc)


@app.get("/api/wiki_cache")
async def read_wiki(
    owner: Annotated[str, Query(description="Repository owner")],
    repo: Annotated[str, Query(description="Repository name")],
    repo_type: Annotated[str, Query(description="Repository type (e.g. github, gitlab)")],
    language: Annotated[str, Query(description="Language of the wiki content")],
    generator: Annotated[str, Query(description="Generator id (cc/dsh/codex/opencode) — public target")] = "",
    config_path: Annotated[str, Query(description="Config file path (file kind) — public target")] = "",
    provider: Annotated[str, Query(description="Provider id (object kind) — public target")] = "",
    model: Annotated[str, Query(description="Model id (object kind) — public target")] = "",
):
    if language not in _LANG_CONFIG["supported_languages"]:
        language = _LANG_CONFIG["default"]
    return await read_wiki_cache(owner, repo, repo_type, language,
                                 digest=_query_choice_digest(generator, config_path, provider, model))


@app.delete("/api/wiki_cache")
async def delete_wiki(
    owner: Annotated[str, Query(description="Repository owner")],
    repo: Annotated[str, Query(description="Repository name")],
    repo_type: Annotated[str, Query(description="Repository type (e.g. github, gitlab)")],
    language: Annotated[str, Query(description="Language of the wiki content")],
    authorization_code: Annotated[str | None, Query(description="Authorization code")] = None,
    generator: Annotated[str, Query(description="Generator id (cc/dsh/codex/opencode) — public target")] = "",
    config_path: Annotated[str, Query(description="Config file path (file kind) — public target")] = "",
    provider: Annotated[str, Query(description="Provider id (object kind) — public target")] = "",
    model: Annotated[str, Query(description="Model id (object kind) — public target")] = "",
    digest: Annotated[str, Query(description="Public trailing eight-character target digest")] = "",
):
    if language not in _LANG_CONFIG["supported_languages"]:
        raise HTTPException(status_code=400, detail="Language is not supported")
    if _WIKI_AUTH_MODE and (not authorization_code or authorization_code != _WIKI_AUTH_CODE):
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete wiki cache: {e}") from e
    if deleted:
        return {"message": f"Wiki cache for {owner}/{repo} ({language}) deleted successfully"}
    raise HTTPException(status_code=404, detail="Wiki cache not found")


@app.get("/api/processed_projects", response_model=list[ProcessedProjectEntry])
async def get_processed_projects():
    try:
        return await list_processed_projects()
    except Exception:
        # Do not expose internal cache paths through error details.
        raise HTTPException(status_code=500, detail="Failed to list processed projects from server cache.") from None


@app.post("/wiki/tasks", response_model=WikiTaskSubmitResult)
async def submit_wiki_task(request: WikiTaskRequest):
    """Submit or join a task, returning created, joined, or cached status."""
    try:
        payload = request.model_dump()
        target = payload.get("target") or {}
        if not target.get("generator"):
            payload["target"] = {**target, "generator": _DEEPWIKI_GENERATOR}
        return await registry.submit(WikiTask.from_wiki_request(payload))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/wiki/tasks", response_model=list[WikiTaskSummary])
async def list_wiki_tasks(
    status: Annotated[Literal["active", "completed"] | None, Query()] = None,
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
    """Run the development Uvicorn server."""
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=_PORT)  # noqa: S104 - Development server binds all interfaces.


if __name__ == "__main__":
    main()
