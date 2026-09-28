"""Execute search-agent conversations using canonical agent events and caller-owned tools."""

import asyncio
import copy
import json
import threading
from contextlib import AsyncExitStack
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

import httpx
from jsonschema import ValidationError

from ..agent.adapters.openai import ChatCompletion
from ..agent.base import BaseAgent, RequestFailedError
from ..agent.events import CONTEXT_APPEND_TYPES, EVENT_TYPES, fold_state, is_event_type
from ..configuration import option, positive_integer
from ..tools.registry import ToolInputError, ToolRegistry, input_error
from ..tools.storage import ToolStorage
from ..tools.tool_early_answer import EarlyAnswerTool
from ..tools.tool_offload import OffloadPolicy, ToolOutput, ToolResultStore
from ..tools.tool_ptc import PTCTool
from ..tools.tool_web import WebTools
from .context import ContextMirror, complete_calls
from .options import model_id, reasoning_effort, tool_result_policy


class CommonAgent(BaseAgent):
    """Run independent tool calls concurrently, committing their results in model call order."""

    name = ""
    defaults: ClassVar[dict] = {}
    runtime_defaults: ClassVar[dict] = {
        "concurrency": option(8, description="Per-agent concurrency budget supplied to each tool provider.",
                              validator=positive_integer),
        "max_steps": option(0, internal=True),
    }
    tool_configs: ClassVar[tuple] = ()
    backends: ClassVar[tuple] = ()
    credential_names: ClassVar[dict] = {}
    data_boundary = ""

    @classmethod
    def configuration_tools(cls, options=None):
        """Discover configured tools without opening connections or allocating tool storage.

        Args:
            options: Effective native options, including the selected backend. None
                discovers the complete tool catalog independently of active options.
        """
        return []

    def __init__(self, config: dict, storage: ToolStorage | None = None, *, api_key: str,
                 model_transport: httpx.AsyncBaseTransport | None = None,
                 web_transport: httpx.AsyncBaseTransport | None = None, on_early_answer=None,
                 brave_api_key: str = ""):
        """Construct a conversation without enabling recording or reading application configuration.

        Args:
            config: Model and base_url plus optional native parameters, concurrency,
                max_steps (zero means unlimited), environment context, and agent_options.
                Subclasses define their supported agent_options and tool credentials.
            storage: Caller-owned tool files; omission creates temporary storage cleaned
                when the session closes. No experiment archive is created.
            api_key: Model credential passed only to the model HTTP client.
            model_transport: Optional model transport, including caller-supplied recording.
            web_transport: Optional transport for public web tools.
            on_early_answer: Optional callback receiving published early-answer text.
            brave_api_key: Credential used only for Brave web search.
        """
        config = {**self.runtime_defaults, "parameters": {}, "environment": {},
                  **copy.deepcopy(config)}
        self.normalize_runtime(config)
        options = self.normalize_options({**self.defaults, **{k: config[k] for k in self.defaults if k in config},
                                          **config.get("agent_options", {})})
        if config.get("agent", self.name) != self.name:
            raise ValueError("Agent identity does not match configuration")
        super().__init__({**{k: v for k, v in config.items() if k not in self.defaults},
                          "agent": self.name, "agent_options": options})
        self.options = self.config["agent_options"]
        self.agent = "search-" + self.name
        self.resources = AsyncExitStack()
        if storage is None:
            directory = self.resources.enter_context(TemporaryDirectory(prefix="gh-puller-tools-"))
            storage = ToolStorage(Path(directory))
        self.storage, self.api_key = storage, api_key
        self.brave_api_key = brave_api_key
        self.model_transport, self.web_transport = model_transport, web_transport
        self.on_early_answer = on_early_answer
        self.tool_definitions = []
        self.tool_results = ToolResultStore(storage)
        self.messages: list[dict] = []
        self.completed_steps: set[tuple[int, int]] = set()
        self.web_tools = self.web_client = None

    @classmethod
    def normalize_runtime(cls, config):
        if type(config["concurrency"]) is not int or config["concurrency"] < 1:
            raise ValueError("concurrency must be a positive integer")
        if type(config["max_steps"]) is not int or config["max_steps"] < 0:
            raise ValueError("max_steps must be a non-negative integer; zero means unlimited")
        return config

    @classmethod
    def normalize_options(cls, options):
        # Image support is negotiated by inference; older callers may still pass this flag.
        options.pop("is_llm_multi_modal", None)
        unknown = options.keys() - cls.defaults.keys()
        if unknown:
            raise ValueError(f"Unsupported {cls.name} options: {', '.join(sorted(unknown))}")
        return options

    async def _enter(self):
        try:
            self.model_client = self.own(httpx.AsyncClient(
                transport=self.model_transport, timeout=httpx.Timeout(300, connect=20)))
            self.context = ContextMirror(self._require_event_recorder(), [])
            self.early_answers = EarlyAnswerTool(self.context, self.storage, self.on_early_answer)
            observer = self.storage.observer
            self.resources.callback(setattr, self.storage, "observer", observer)
            loop, thread = asyncio.get_running_loop(), threading.get_ident()

            def record(kind, data):
                if ((kind.startswith("tool/") and kind not in EVENT_TYPES and is_event_type(kind))
                        or kind in {"artifact/allocated", "artifact/saved"}):
                    self._require_event_recorder().event(kind, **data)

            async def record_async(kind, data):
                record(kind, data)

            def observe(kind, **data):
                if observer is not None:
                    observer(kind, **data)
                if threading.get_ident() == thread:
                    record(kind, data)
                else:
                    # Synchronous search providers report from worker threads; preserve ordering and errors.
                    asyncio.run_coroutine_threadsafe(record_async(kind, data), loop).result()

            self.storage.observer = observe
            await self.initialize_tools()
            self.storage.event("tool_result/policies", tools={name: asdict(policy)
                                                        for name, policy in self.offload_policies.items()})
            self.messages = [{"role": "system", "content": self.instructions()}]
            self.context.definitions = self.tool_definitions
            self.context.append(self.messages[0])
        except BaseException:
            await self.resources.aclose()
            raise

    def own(self, resource):
        self.resources.push_async_callback(resource.aclose)
        return resource

    def http_client(self, transport=None):
        return self.own(httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(30, connect=15), follow_redirects=False,
            limits=httpx.Limits(max_connections=self.config["concurrency"], max_keepalive_connections=8)))

    def add_web(self):
        self.web_client = self.http_client(self.web_transport)
        self.web_tools = WebTools(
            self.web_client, self.storage, concurrency=self.config["concurrency"],
            search_concurrency=self.options["web_search_concurrency"],
            search_interval=self.options["web_search_interval"],
            search_backend=self.options["web_search_backend"], brave_api_key=self.brave_api_key)
        return self.web_tools

    def install_tools(self, *providers, ptc=False, early_answer=False, offload: OffloadPolicy | None = None,
                      offload_overrides: dict[str, OffloadPolicy | None] | None = None):
        """Install providers or (provider, name) pairs, then apply retention to public names."""
        registry = ToolRegistry(*providers)
        if early_answer:
            registry.install(self.early_answers, None)
        self.tool_registry = registry
        self.tool_definitions, self.tool_handlers = registry.definitions, registry.handlers
        if ptc:
            self.ptc_definitions = self.tool_definitions
            registry.install(PTCTool(self.storage, registry, self._call, concurrency=self.config["concurrency"],
                                     variant=ptc, immediate_tools=("early_answer",)), None)
            self.tool_definitions = [registry.specs["run_code"].definition]
        overrides = offload_overrides or {}
        if unknown := overrides.keys() - registry.specs.keys():
            raise ValueError(f"Offload policy names unregistered tools: {', '.join(sorted(unknown))}")
        if any(policy is not None and not isinstance(policy, OffloadPolicy)
               for policy in (offload, *overrides.values())):
            raise TypeError("Offload settings must be OffloadPolicy or None")
        self.offload_policies = {}
        for definition in self.tool_definitions:
            name = definition["function"]["name"]
            if (policy := overrides.get(name, offload)) is not None:
                self.offload_policies[name] = tool_result_policy(policy, self.options, registry.registered_names[name])

    async def initialize_tools(self):
        raise NotImplementedError

    @classmethod
    async def prepare(cls, config, tasks):
        """Prepare shared dependencies without attaching them to one conversation.

        Args:
            config: Resolved native configuration for the next conversation.
            tasks: Caller-owned preparation tasks, awaited on service shutdown.
                Cancelling a conversation must not cancel shared preparation.
        """

    def set_credentials(self, credentials):
        """Replace in-memory credentials between turns without rebuilding context.

        Args:
            credentials: Model and tool credential values supplied by the caller.
        """
        self.api_key = credentials["api_key"]
        self.brave_api_key = credentials.get("brave_api_key", "")
        if self.web_tools:
            self.web_tools.brave_api_key = self.brave_api_key

    def instructions(self):
        raise NotImplementedError

    def begin_query(self):
        pass

    def clear_resources(self):
        self.tools.clear_context()
        if self.web_tools:
            self.web_tools.clear_context()

    def cancelled_output(self, call_id):
        return {"error": {"type": "CancelledError", "message": "Tool call cancelled"}}

    def fatal_error(self):
        return RequestFailedError("Tool provider unavailable")

    async def _exit(self, exc):
        await self.resources.aclose()

    async def _call(self, call: dict, *, parent_call_id: str | None = None) -> ToolOutput:
        recorder = self._require_event_recorder()
        call_id, function = call["id"], call["function"]
        name, raw = function["name"], function["arguments"]
        if parent_call_id:
            recorder.event("tool/start", callId=call_id, name=name, arguments=json.loads(raw),
                           parentCallId=parent_call_id)
        else:
            recorder.tool_call(call_id, name, raw)
        self.storage.event("tool/start", call_id=call_id, name=name, arguments=raw, parent_call_id=parent_call_id)
        try:
            available = self.ptc_definitions if parent_call_id else self.tool_definitions
            definitions = {item["function"]["name"]: item["function"]["parameters"] for item in available}
            if name not in definitions:
                raise ToolInputError(f"Unknown tool: {name}", code="unknown_tool", choices=list(definitions))
            args = json.loads(raw)
            result = await self.tool_registry.call(name, call_id, args)
        except (ValueError, ValidationError) as exc:
            result = {"error": input_error(exc)}
        except BaseException as exc:
            recorder.tool_end(call_id, error={"type": type(exc).__name__, "message": str(exc)})
            self.storage.event("tool/end", call_id=call_id, status="failed", error=type(exc).__name__)
            raise
        if isinstance(result, ToolOutput):
            output = result
            result = output.result if output.result is not None else json.loads(output.content)
        else:
            images, observations = (self.web_tools.image_parts(result, call_id)
                                    if self.web_tools and self.tool_registry.providers.get(name) is self.web_tools
                                    and self.tool_registry.registered_names[name] == "web_fetch" else ([], []))
            output = ToolOutput(json.dumps(result, ensure_ascii=False),
                                fatal=any(item.get("fatal") for item in result.get("results", [])),
                                images=images, observations=observations)
        content = output.content
        failed = "error" in result or bool(result.get("errors")) or any(
            "error" in item for item in result.get("results", []))
        suffix = "ptc-value" if parent_call_id else "model-visible"
        self.storage.record(f"{self.storage.allocate('tool')}.{suffix}.json", {
            "call_id": call_id, "content": content, "image_attachments": output.observations,
            "parent_call_id": parent_call_id,
        }, call_id=call_id, parent_call_id=parent_call_id)
        if failed:
            recorder.tool_end(call_id, error={"type": "ToolError", "message": content})
        else:
            recorder.tool_end(call_id, result=content)
        self.storage.event("tool/end", call_id=call_id, status="failed" if failed else "completed")
        return output

    async def result(self, prompt: str) -> str:
        async for _ in self.stream(prompt):
            pass
        return self.final_answer

    async def take_notifications(self) -> list[str]:
        return []

    async def wait_notifications(self):
        await asyncio.Future()

    async def pending_background_tasks(self) -> bool:
        return False

    async def append_notifications(self) -> bool:
        notifications = await self.take_notifications()
        for content in notifications:
            message = {"role": "user", "content": content}
            self.messages.append(message)
            self.context.append(message, role="tool")
        return bool(notifications)

    def clear_context(self) -> None:
        """Explicitly clear conversation memory while retaining the session's logs."""
        self.messages = self.messages[:1]
        self.final_answer = ""
        self.completed_steps.clear()
        self.tool_results.clear_context()
        self.early_answers.clear_context()
        self.clear_resources()
        self.context.synchronize(self.messages, force=True)
        self.storage.event("context/cleared", query=self.context.query)

    @classmethod
    def own_events(cls, events):
        """Select private facts from the latest concrete Agent instance.

        Args:
            events: Ordered event prefix. Re-observing the same identity/configuration
                does not start a new instance; a changed ``instance`` does.
        """
        start, identity, instance = 0, None, None
        for index, event in enumerate(events):
            if event["type"] == "agent/set":
                data = event["data"]
                current = data.get("instance", instance)
                if data["agent"] != identity or current != instance:
                    start, identity, instance = index + 1, data["agent"], current
        return events[start:] if identity == "search-" + cls.name else []

    @classmethod
    def validate_events(cls, events, *, max_bytes=64 * 1024 * 1024):
        """Bound file observations before allocation; tools validate their references on load.

        Args:
            events: An ordered prefix of canonical and Agent-owned events.
            max_bytes: Maximum total decoded evidence size accepted by the caller.
        """
        ToolStorage.event_files(cls.own_events(events), max_bytes=max_bytes)

    def load_events(self, events, *, resolve_artifact=None, max_bytes=64 * 1024 * 1024):
        """Restore own memory or a foreign Agent's Context in a fresh session.

        Args:
            events: Any ordered event prefix. Own search events restore private data;
                foreign events contribute only context. Connections and in-flight work
                are not process snapshots; the target keeps its current configuration.
            max_bytes: Maximum total decoded evidence size accepted by the caller.
            resolve_artifact: Caller-owned reader of content-addressed evidence. Omission
                resolves only artifacts already available in this ToolStorage.
        """
        own = self.own_events(events)
        self.storage.load_events(own, resolve_artifact=resolve_artifact, max_bytes=max_bytes)
        self.storage.reserve_context_ids(fold_state(events)["context"])
        self.tool_registry.load_events(own)
        self.early_answers.load_events(own)
        self._restore_images = bool(own)
        super().load_events(events)
        self.tool_results.bind_messages(self.messages, images=self.context.tool_images())
        query = step = 0
        for event in own:
            kind, data = event["type"], event["data"]
            if kind == "turn/start":
                query, step = query + 1, 0
            elif kind == "step/start":
                step += 1
            elif kind == "step/end" and data["outcome"] == "completed":
                self.completed_steps.add((query, step))
            elif kind in CONTEXT_APPEND_TYPES | {"context/set"}:
                positions = [(item.get("metadata", {}).get("query", 0), item.get("metadata", {}).get("step", 0))
                             for item in data["items"]]
                query, step = max([(query, step), *positions])
                if kind == "context/set" and all(item.get("role") == "system" for item in data["items"]):
                    self.completed_steps.clear()
        if own:
            self.context.query, self.context.step = query, step

    def _load_context(self, items):
        self.messages = self.context.load(items, self.messages[0],
                                          storage=self.storage if self._restore_images else None)
        self.final_answer = next((m["content"] for m in reversed(self.messages)
                                  if m["role"] == "assistant" and not m.get("tool_calls")), "")
        self.completed_steps.clear()

    def update_config(self, changes):
        """Apply between-turn model controls and observe their effective values.

        Args:
            changes: Public native configuration values. Construction-time tool options
                require a new Agent; credentials use ``set_credentials`` separately.
        """
        recorder = self._require_event_recorder()
        config = self.normalize_runtime({**self.config, **copy.deepcopy(changes)})
        config["model"] = model_id(config["model"])
        if "reasoning_effort" in config["parameters"]:
            config["parameters"]["reasoning_effort"] = reasoning_effort(config["parameters"]["reasoning_effort"])
        changed = {key: value for key, value in config.items() if value != self.config.get(key)}
        if changed.keys() - {"model", "base_url", "parameters", "environment", "max_steps"}:
            raise ValueError("Tool or Agent configuration changed; construct a new Agent")
        self.config.update(changed)
        for key, value in changed.items():
            recorder.set_agent_facet(key, value)
        if "environment" in changed:
            self.messages[0] = {"role": "system", "content": self.instructions()}
            self.context.synchronize(self.messages)

    def set_model(self, model: str) -> None:
        """Change the model between turns, preserving context and request parameters."""
        model = model_id(model)
        previous = self.config["model"]
        self.update_config({"model": model})
        self.storage.event("model/changed", previous=previous, model=model, parameters=self.config["parameters"])

    def set_reasoning_effort(self, effort: str) -> None:
        """Change effort between turns without altering the prompt or retained context."""
        effort = reasoning_effort(effort)
        previous = self.config["parameters"].get("reasoning_effort")
        self.update_config({"parameters": {**self.config["parameters"], "reasoning_effort": effort}})
        self.storage.event("model/reasoning_effort_changed", previous=previous, reasoning_effort=effort,
                       parameters=self.config["parameters"])

    async def stream(self, prompt: str | None):
        """Keep completed turns and close failed turns so the session can continue."""
        recorder = self._require_event_recorder()
        recorder.begin_turn()
        try:
            async for part in self._stream_turn(prompt):
                yield part
        except BaseException as exc:
            recorder.end_turn(outcome="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                              reason=str(exc) or type(exc).__name__)
            raise

    async def _stream_turn(self, prompt: str | None):
        recorder = self._require_event_recorder()
        if prompt is not None:
            self.messages = complete_calls(self.messages)
            if self.offload_policies:
                self.tool_results.begin_user_query()
            self.begin_query()
        self.context.query += 1
        self.context.step = 0
        if prompt is not None:
            self.messages.append({"role": "user", "content": prompt})
            self.context.append(self.messages[-1])
        self.final_answer = ""
        while not self.config["max_steps"] or self.context.step < self.config["max_steps"]:
            await self.append_notifications()
            self.context.step += 1
            recorder.begin_step()
            self.messages = self.tool_results.prepare_messages(self.messages)
            self.context.synchronize(self.messages)
            completion = ChatCompletion(
                self.model_client, recorder, base_url=self.config["base_url"],
                headers={"Authorization": f"Bearer {self.api_key}"},
                body={"model": self.config["model"], "messages": list(self.messages),
                      "tools": self.tool_definitions, **self.config["parameters"]},
            )
            async for part in completion.stream():
                yield part
            message = completion.message
            self.storage.event("model/assistant", request_id=f"r{recorder.request_n}", message=message)
            self.messages.append(message)
            self.context.append(message, role="assistant")
            if not (calls := message.get("tool_calls")):
                if not message.get("content"):
                    raise RequestFailedError("Model returned an empty final answer")
                self.final_answer = message["content"]
                recorder.end_step()
                self.completed_steps.add((self.context.query, self.context.step))
                if await self.append_notifications():
                    continue
                recorder.end_turn(reason="final_response")
                return
            fatal = False
            tasks = []
            tool_queries = (self.tool_results.begin_tool_batch(len(calls))
                            if self.offload_policies else range(len(calls)))
            try:
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(self._call(call)) for call in calls]
            finally:
                # Keep successful siblings; pair interrupted calls with explicit errors.
                # A subsequent prompt must never carry unmatched assistant tool calls.
                image_messages = []
                for call, task, tool_query in zip(calls, tasks, tool_queries, strict=True):
                    if task.cancelled():
                        output = ToolOutput(json.dumps(self.cancelled_output(call["id"])))
                    elif error := task.exception():
                        output = ToolOutput(json.dumps({
                            "error": {"type": type(error).__name__, "message": str(error)},
                        }))
                    else:
                        output = task.result()
                        fatal |= output.fatal
                    tool_message = {"role": "tool", "tool_call_id": call["id"], "content": output.content}
                    image_message = {"role": "user", "content": output.images} if output.images else None
                    name = call["function"]["name"]
                    if policy := self.offload_policies.get(name):
                        self.tool_results.bind(output, tool_message, image_message, name=name,
                                               tool_query=tool_query, policy=policy)
                    self.messages.append(tool_message)
                    self.context.append(tool_message, role="tool")
                    if image_message is not None:
                        image_messages.append((image_message, output.observations, call["id"]))
                # Keep tool/image ownership separate, with images after ALL paired tool results.
                for image_message, observations, call_id in image_messages:
                    self.messages.append(image_message)
                    self.context.append(image_message, role="tool", image_observation={
                        "type": "message", "role": "user", "content": observations, "metadata": {"call_id": call_id},
                    })
            if fatal:
                raise self.fatal_error()
            recorder.end_step()
            self.completed_steps.add((self.context.query, self.context.step))
        raise RequestFailedError("Model step limit reached without a final answer", reason_code="budget_exhausted")
