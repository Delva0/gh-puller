"""Search agents for GitHub, GitCode, the web and code in a persistent container."""

import asyncio
import json
from pathlib import Path, PurePosixPath
from typing import ClassVar

from gh_puller.agent.adapters.openai import ChatCompletion
from gh_puller.agent.base import RequestFailedError

from ..configuration import option
from ..tools.githost_api_utils import APIProvider
from ..tools.github_api import BACKEND as GITHUB_BACKEND
from ..tools.github_api import GitHubUnavailableError
from ..tools.registry import tool_configuration, tool_definitions
from ..tools.shell_contract import shell_settings
from ..tools.tool_bash import (
    CONTRACT,
    LEGACY_CONTRACT,
    PREVIOUS_CONTRACT,
    BashTool,
    DockerBashTools,
    DockerSandbox,
    SandboxBashTool,
)
from ..tools.tool_early_answer import INSTRUCTIONS as EARLY_ANSWER_INSTRUCTIONS
from ..tools.tool_early_answer import EarlyAnswerTool
from ..tools.tool_fastcode import FastCodeTool
from ..tools.tool_gitcode import BACKEND as GITCODE_BACKEND
from ..tools.tool_gitcode import DSL_LANGUAGE as GITCODE_DSL_LANGUAGE
from ..tools.tool_gitcode import GITCODE_CONFIG, GitCodeDSLTool, GitCodeTool
from ..tools.tool_github import DSL_LANGUAGE as GITHUB_DSL_LANGUAGE
from ..tools.tool_github import (
    GITHUB_CONFIG,
    SCHEMA_SOURCE,
    GitHubDSLTool,
    GitHubGraphQLTool,
    GitHubRESTTool,
    query_schema,
)
from ..tools.tool_mcp import MCPTools, load_connection
from ..tools.tool_offload import TOOL_RESULT_CONFIG, OffloadPolicy, ToolResultStore, tool_result_config
from ..tools.tool_ptc import PTCTool
from ..tools.tool_web import WEB_CONFIG, WebTools
from .common import CommonAgent
from .options import (
    SEARCH_DEFAULTS,
    normalize_search,
    normalize_web,
    tool_result_policy,
)
from .registry import register

EARLY_ANSWER_ENABLED = False

SEARCH_INSTRUCTIONS = (
    "Always think in Chinese.\n\n"
    + (EARLY_ANSWER_INSTRUCTIONS if EARLY_ANSWER_ENABLED else "") +
    "Let the user's requested depth guide exploration, especially for open-ended questions.\n\n"
    "Make full use of the available search tools of different types. Whenever possible, call general-purpose and "
    "specialized search tools in parallel to gather more complete information from complementary perspectives. "
    "Issue independent queries together in the same round of tool calls.\n\n"
    "Use exact search for precise terms; use semantic search for meaning or uncertain wording.\n\n"
    "As you search and read, learn terminology, aliases and relationships between concepts from the material. "
    "Use what you learn to refine subsequent search terms and perspectives, and check that the evidence applies "
    "to the current goal and constraints."
)


def search_instructions(environment):
    return SEARCH_INSTRUCTIONS + "\n\n" + json.dumps(environment, ensure_ascii=False)


GITHUB_BACKENDS = ("gh-cli", "rest", "graphql", "dsl", "gh-mcp", "split")
GITHUB_API_PROVIDERS = {"rest": GitHubRESTTool, "graphql": GitHubGraphQLTool, "dsl": GitHubDSLTool}


def search_tool_configuration(options, *providers):
    result = tool_configuration(*providers, WebTools, ToolResultStore)
    if options.get("ptc"):
        result.extend(tool_configuration(PTCTool, shared=tuple(dict.fromkeys(
            key for item in result for key in item["configuration"]))))
    for item in result:
        item["configuration"].extend(tool_result_config(item["id"]).defaults)
    if EARLY_ANSWER_ENABLED:
        result.extend(tool_configuration(EarlyAnswerTool))
    return result


def search_tool_configs(*providers):
    return (TOOL_RESULT_CONFIG, *(tool_result_config(item["id"]) for item in
                                 search_tool_configuration({"ptc": True}, *providers)))


@register
class GitHubAgent(CommonAgent):
    name = "github"
    defaults: ClassVar[dict] = {
        **SEARCH_DEFAULTS,
        "backend": option("rest", choices=GITHUB_BACKENDS,
                          requires={"gh-cli": ("host_commands",), "gh-mcp": ("mcp_config",)}),
        "mcp_mode": option(None, binding="mcp_mode"), "mcp_config": option(None, binding="mcp_config"),
    }
    tool_configs = (GITHUB_CONFIG, WEB_CONFIG, *search_tool_configs(*GITHUB_API_PROVIDERS.values(), BashTool))
    backends = GITHUB_BACKENDS
    data_boundary = "Live GitHub resources, public web search and HTTP(S) downloads."

    @classmethod
    def configuration_tools(cls, options=None):
        if options is None:
            result = search_tool_configuration({"ptc": True}, *GITHUB_API_PROVIDERS.values(), BashTool)
            next(item for item in result if item["id"] == "bash")["configuration"].extend(GITHUB_CONFIG.credentials)
            return result
        backend = options["backend"]
        if backend == "gh-cli":
            result = search_tool_configuration(options, BashTool)
            result[0]["configuration"].extend(GITHUB_CONFIG.credentials)
            return result
        if backend == "gh-mcp":
            return search_tool_configuration(options)
        provider = GITHUB_API_PROVIDERS["rest" if backend == "split" else backend]
        return search_tool_configuration(options, (provider, "github"),
                                         *((GitHubGraphQLTool, "github_graphql"),) if backend == "split" else ())

    @classmethod
    def normalize_options(cls, options):
        options = normalize_search(super().normalize_options(options))
        if options["backend"] not in cls.backends:
            raise ValueError(f"backend must be one of {', '.join(cls.backends)}")
        if options["backend"] == "gh-mcp":
            options["mcp_mode"] = options["mcp_mode"] or "r"
            if options["mcp_mode"] not in {"r", "rw"}:
                raise ValueError("--mcp-mode must be r or rw")
            if not options["mcp_config"]:
                raise ValueError("mcp_config is required for the gh-mcp backend")
            options["mcp_config"] = str(Path(options["mcp_config"]).expanduser().resolve())
        elif options["mcp_mode"] is not None:
            raise ValueError("--mcp-mode requires --backend gh-mcp")
        elif options["mcp_config"] is not None:
            raise ValueError("--mcp-config requires --backend gh-mcp")
        return options

    def __init__(self, config, storage=None, *, github_token="", github_transport=None, **kwargs):
        super().__init__(config, storage, **kwargs)
        self.github_token, self.github_transport = github_token, github_transport
        self.github_client = self.mcp_tools = self.graphql_tool = None

    @classmethod
    async def prepare(cls, config, tasks):
        if config["agent_options"]["backend"] == "dsl":
            if "github_schema" not in tasks:
                tasks["github_schema"] = asyncio.create_task(asyncio.to_thread(query_schema))
            await asyncio.shield(tasks["github_schema"])

    def set_credentials(self, credentials):
        super().set_credentials(credentials)
        self.github_token = credentials.get("github_token", "")
        for provider in (self.tools, self.graphql_tool):
            if hasattr(provider, "api"):
                provider.api.token = self.github_token

    async def initialize_tools(self):
        name = None
        if self.options["backend"] == "gh-cli":
            if not self.github_token:
                raise GitHubUnavailableError("Set GH_TOKEN or GITHUB_TOKEN for the GitHub gh-cli backend")
            self.data_boundary += " Host scratch files and commands through bash."
            self.tools = BashTool(self.storage, token=self.github_token, concurrency=self.config["concurrency"])
            self.config["environment"]["github_access"] = (
                "Use only online GitHub sources; search and read them with authenticated gh commands.")
        elif self.options["backend"] == "gh-mcp":
            entry = "github" if self.options["mcp_mode"] == "r" else "github-rw"
            connection = load_connection(self.options["mcp_config"], entry,
                                         variables={"GITHUB_TOKEN": self.github_token} if self.github_token else {})
            stdio = connection.get("stdio")
            self.storage.record(f"{self.storage.allocate('github-mcp')}.launch.json", {
                "config": self.options["mcp_config"], "entry": entry,
                **({"command": stdio.command, "args": stdio.args, "cwd": str(stdio.cwd) if stdio.cwd else None,
                    "environment_names": list(stdio.env or {})} if stdio else
                   {"url": connection["url"], "header_names": list(connection.get("headers", {}))}),
            })
            self.mcp_tools = self.own(MCPTools(
                self.storage, server="github", **connection, concurrency=self.config["concurrency"]))
            self.tools = await self.mcp_tools.connect()
        else:
            self.github_client = self.http_client(self.github_transport)
            provider = GITHUB_API_PROVIDERS["rest" if self.options["backend"] == "split" else self.options["backend"]]
            self.tools = provider(self.github_client, self.storage, token=self.github_token,
                                  concurrency=self.config["concurrency"])
            name = "github"
        registrations = [(self.tools, name)]
        if self.options["backend"] == "split":
            self.graphql_tool = GitHubGraphQLTool(
                self.github_client, self.storage, token=self.github_token, concurrency=self.config["concurrency"])
            # Share saved evidence only; each protocol owns its request queue and state.
            self.graphql_tool.api.responses = self.tools.responses
            registrations.append((self.graphql_tool, "github_graphql"))
        self.backend_tool_names = [t["function"]["name"] for t in tool_definitions(*registrations)]
        self.install_tools(*registrations, (self.add_web(), None), (self.tool_results, None), ptc=self.options["ptc"],
                           early_answer=EARLY_ANSWER_ENABLED,
                           offload=tool_result_policy(OffloadPolicy(), self.options),
                           offload_overrides={"early_answer": None} if EARLY_ANSWER_ENABLED else None)

    def instructions(self):
        return search_instructions(self.config["environment"])

    def begin_query(self):
        if isinstance(self.tools, APIProvider):
            self.tools.begin_query()
        if self.graphql_tool:
            self.graphql_tool.begin_query()

    def clear_resources(self):
        super().clear_resources()
        if self.graphql_tool:
            self.graphql_tool.clear_context()

    def fatal_error(self):
        return GitHubUnavailableError(self.tools.unavailable
                                      or (self.graphql_tool.unavailable if self.graphql_tool else None)
                                      or "GitHub authentication unavailable")

    async def backend_metadata(self):
        backend = {**GITHUB_BACKEND, "authenticated": bool(self.github_token), "tool_backend": self.options["backend"],
                   "transport": "httpx"}
        if self.options["backend"] == "gh-mcp":
            backend = {"provider": "github/github-mcp-server",
                       "tool_backend": "gh-mcp", "mcp_mode": self.options["mcp_mode"],
                       "mcp_config": self.options["mcp_config"],
                       "transport": "stdio" if self.mcp_tools.stdio else "streamable-http"}
        elif self.options["backend"] == "gh-cli":
            version = await asyncio.create_subprocess_exec("gh", "--version", stdout=asyncio.subprocess.PIPE)
            output, _ = await version.communicate()
            backend = {"name": "GitHub", "provider": "host-gh-cli", "tool_backend": "gh-cli",
                       "authenticated": bool(self.github_token), "transport": "native gh CLI (REST or GraphQL)",
                       "gh_version": output.decode().splitlines()[0]}
        elif self.options["backend"] == "rest":
            backend["content_scope"] = "GitHub REST on api.github.com"
            del backend["graphql"]
        elif self.options["backend"] == "graphql":
            backend.update(content_scope="Read-only GitHub GraphQL on api.github.com", methods=["POST"])
        elif self.options["backend"] == "dsl":
            backend.update(language=GITHUB_DSL_LANGUAGE, graphql_schema=SCHEMA_SOURCE,
                           reuse_scope="user_query", refresh="explicit tool argument")
        return {**backend, "tools": self.backend_tool_names,
                "web": self.web_tools.metadata()}


@register
class GitCodeAgent(CommonAgent):
    name = "gitcode"
    backends = ("rest", "dsl")
    defaults: ClassVar[dict] = {**SEARCH_DEFAULTS, "backend": option("rest", choices=backends)}
    tool_configs = (GITCODE_CONFIG, WEB_CONFIG, *search_tool_configs(GitCodeTool, GitCodeDSLTool))
    data_boundary = "Live GitCode resources, public web search and HTTP(S) downloads."

    @classmethod
    def configuration_tools(cls, options=None):
        if options is None:
            return search_tool_configuration({"ptc": True}, GitCodeTool, GitCodeDSLTool)
        provider = GitCodeDSLTool if options["backend"] == "dsl" else GitCodeTool
        return search_tool_configuration(options, (provider, "gitcode"))

    @classmethod
    def normalize_options(cls, options):
        options = normalize_search(super().normalize_options(options))
        if options["backend"] not in cls.backends:
            raise ValueError("GitCode backend must be rest or dsl")
        return options

    def __init__(self, config, storage=None, *, gitcode_token="", gitcode_transport=None, **kwargs):
        super().__init__(config, storage, **kwargs)
        self.gitcode_token, self.gitcode_transport = gitcode_token, gitcode_transport

    def set_credentials(self, credentials):
        super().set_credentials(credentials)
        self.gitcode_token = self.tools.api.token = credentials.get("gitcode_token", "")

    async def initialize_tools(self):
        self.gitcode_client = self.http_client(self.gitcode_transport)
        provider = GitCodeDSLTool if self.options["backend"] == "dsl" else GitCodeTool
        self.tools = provider(self.gitcode_client, self.storage, token=self.gitcode_token,
                              concurrency=self.config["concurrency"])
        self.install_tools((self.tools, "gitcode"), (self.add_web(), None), (self.tool_results, None),
                           ptc=self.options["ptc"],
                           early_answer=EARLY_ANSWER_ENABLED,
                           offload=tool_result_policy(OffloadPolicy(), self.options),
                           offload_overrides={"early_answer": None} if EARLY_ANSWER_ENABLED else None)

    def instructions(self):
        return search_instructions(self.config["environment"])

    def begin_query(self):
        self.tools.begin_query()

    async def backend_metadata(self):
        return {**GITCODE_BACKEND, "authenticated": bool(self.gitcode_token), "transport": "httpx",
                "web": self.web_tools.metadata(), "backend": self.options["backend"], "tool": "gitcode",
                **({"language": GITCODE_DSL_LANGUAGE} if self.options["backend"] == "dsl" else {})}


@register
class WebAgent(CommonAgent):
    name = "web"
    tool_configs = (WEB_CONFIG,)
    data_boundary = "Public web search and HTTP(S) downloads; no repository tools or prior knowledge store."

    @classmethod
    def configuration_tools(cls, options=None):
        return tool_configuration(WebTools)

    @classmethod
    def normalize_options(cls, options):
        return normalize_web(super().normalize_options(options))

    async def initialize_tools(self):
        self.tools = self.add_web()
        # Two tools only: keep results in context without an offload/retrieval tool.
        self.install_tools((self.tools, None), offload=None)

    def instructions(self):
        return "Always think in Chinese.\n\n" + json.dumps(self.config["environment"], ensure_ascii=False)

    async def backend_metadata(self):
        return {"name": "Web", "web": self.web_tools.metadata()}


CODE_INSTRUCTIONS = (
    "Use GitHub material (issues, pull requests and discussions) and Git history "
    "to understand developers' intent, reasoning and problem context as you investigate code. "
    "Build your understanding from inspected evidence. Gather related code and context together, "
    "then let gaps or contradictions guide further investigation. When an explanation depends on "
    "unseen code or history, inspect it before treating it as established. Answer once the core "
    "question is supported, distinguishing observed behavior, documented intent, and inference.\n"
    "Inspect the container working directory for prepared repositories and reuse them. "
    "Read as much relevant code and surrounding context as possible in each round. "
    "Parallelize tool calls whenever you can, especially file reads and searches."
)


class FastCodeRecorder:
    """Correlate internal inference without appending it to the outer conversation."""

    def __init__(self, recorder, call_id, phase, storage):
        self.recorder, self.call_id, self.phase = recorder, call_id, phase
        self.storage = storage
        self.request_id = None

    def model_request(self, **data):
        self.request_id = self.recorder.model_request(parentCallId=self.call_id,
                                                     fastcodePhase=self.phase, **data)
        self.storage.event("model/child_request", request_id=self.request_id,
                       parent_call_id=self.call_id, phase=self.phase)
        return self.request_id

    def __getattr__(self, name):
        return getattr(self.recorder, name)


@register
class CodeAgent(CommonAgent):
    """Search and understand code through a persistent container and GitHub evidence."""

    name = "code"
    defaults: ClassVar[dict] = {"container": option(None, binding="container", required=True),
                              "workdir": option("/workspace", binding="workdir")}
    tool_configs = (GITHUB_CONFIG,)
    data_boundary = "The connected persistent container filesystem, its operator-configured network and GitHub REST."

    @classmethod
    def configuration_tools(cls, options=None):
        return [*tool_configuration(DockerBashTools, shared=("container", "workdir")),
                *tool_configuration((GitHubRESTTool, "github"))]

    def __init__(self, config, *args, github_token="", github_transport=None, **kwargs):
        config = {"bash_contract": CONTRACT, **config}
        config.pop("read_settings", None)
        if config["bash_contract"] in {CONTRACT, PREVIOUS_CONTRACT, LEGACY_CONTRACT}:
            config.setdefault("shell_settings", shell_settings())
        super().__init__(config, *args, **kwargs)
        self.github_token, self.github_transport = github_token, github_transport

    @classmethod
    def normalize_options(cls, options):
        options = super().normalize_options(options)
        if not options["container"]:
            raise ValueError("--container is required for the code agent")
        if not PurePosixPath(options["workdir"]).is_absolute():
            raise ValueError("--workdir must be an absolute container path")
        return options

    async def initialize_tools(self):
        self.sandbox = await DockerSandbox(self.options["container"], self.options["workdir"],
                                          identity=self.config.get("connection")).connect()
        self.config["connection"] = self.sandbox.identity
        self.storage.event("tool/bash/connected", connection=self.sandbox.identity)
        self._require_event_recorder().set_agent_facet("connection", self.sandbox.identity)
        self.github_client = self.http_client(self.github_transport)
        self.github_tools = GitHubRESTTool(self.github_client, self.storage, token=self.github_token,
                                          concurrency=self.config["concurrency"])
        retrieval = []
        if self.config.get("fastcode"):
            self.fastcode_tool = FastCodeTool(self.sandbox, self.config["fastcode"],
                                             complete=self.fastcode_complete, storage=self.storage,
                                             model=self.config["model"])
            self.storage.event("fastcode/connected", **await self.fastcode_tool.connect())
            retrieval.append((self.fastcode_tool, "codebase"))
        contract = self.config.setdefault("bash_contract", CONTRACT)
        if contract not in {CONTRACT, PREVIOUS_CONTRACT, LEGACY_CONTRACT, "legacy"}:
            raise ValueError(f"Unsupported saved Bash contract: {contract}")
        if contract == "legacy":
            self.tools = SandboxBashTool(self.storage, self.sandbox, concurrency=self.config["concurrency"])
            self.install_tools((self.tools, None), (self.github_tools, "github"), *retrieval, offload=None)
        else:
            legacy_names = contract == LEGACY_CONTRACT
            self.tools = self.own(DockerBashTools(self.storage, self.sandbox, concurrency=self.config["concurrency"],
                                                settings=self.config.get("shell_settings"), legacy_names=legacy_names))
            self.config["shell_settings"] = self.tools.settings
            self.config["shell_runtime"] = await self.tools.request({"action": "shell_capabilities", "wait": 0})
            names = {"bash": "Bash" if legacy_names else None,
                     "task_output": "TaskOutput" if contract != CONTRACT else None,
                     "task_stop": "TaskStop" if contract != CONTRACT else None}
            self.install_tools((self.tools, names), (self.github_tools, "github"), *retrieval, offload=None)
            if legacy_names:
                # Retain the historical Bash task names and catalog order.
                self.tool_definitions = [self.tool_registry.specs[name].definition
                                         for name in ("Bash", "TaskOutput", "TaskStop",
                                                      "github", *(name for _, name in retrieval))]

    def begin_query(self):
        self.github_tools.begin_query()

    def set_credentials(self, credentials):
        super().set_credentials(credentials)
        self.github_token = self.github_tools.api.token = credentials.get("github_token", "")

    def clear_resources(self):
        super().clear_resources()
        self.github_tools.clear_context()

    def fatal_error(self):
        return GitHubUnavailableError(self.github_tools.unavailable or "GitHub authentication unavailable")

    def instructions(self):
        return "\n".join(part for part in (
            CODE_INSTRUCTIONS, f"Container working directory: {self.options['workdir']}",
            self.config.get("task_instructions", ""),
        ) if part)

    async def fastcode_complete(self, call_id, request):
        recorder = FastCodeRecorder(self._require_event_recorder(), call_id, request["phase"], self.storage)
        # Keep the run's model/reasoning/output configuration. The untouched
        # upstream proposal is archived alongside the actual native request.
        body = {**request["body"], **self.config["parameters"], "model": self.config["model"]}
        if "max_completion_tokens" in self.config["parameters"]:
            body.pop("max_tokens", None)
        completion = ChatCompletion(
            self.model_client, recorder, base_url=self.config["base_url"],
            headers={"Authorization": f"Bearer {self.api_key}"},
            body=body,
        )
        message = await completion.result()
        self.storage.event("model/assistant", request_id=recorder.request_id,
                           message=message, fastcode_request=request)
        self.storage.event("fastcode/model_response", parent_call_id=call_id,
                       request_id=recorder.request_id, phase=request["phase"])
        if not message.get("content") or message.get("tool_calls"):
            raise RequestFailedError("FastCode internal model request requires a text response")
        return message["content"]

    async def take_notifications(self):
        return await self.tools.take_notifications() if isinstance(self.tools, DockerBashTools) else []

    async def wait_notifications(self):
        if isinstance(self.tools, DockerBashTools):
            await self.tools.wait_notifications()
        else:
            await super().wait_notifications()

    async def pending_background_tasks(self):
        return isinstance(self.tools, DockerBashTools) and await self.tools.pending()

    def cancelled_output(self, call_id):
        return (self.tools.cancelled_output(call_id) if call_id in self.tools.calls
                else super().cancelled_output(call_id))

    async def backend_metadata(self):
        return {"provider": "docker", "transport": "docker exec", "connection": self.sandbox.identity,
                **({"fastcode": self.config["fastcode"]} if self.config.get("fastcode") else {}),
                "github": {"tool_backend": "rest", "transport": "httpx", "authenticated": bool(self.github_token)},
                "bash_contract": self.config["bash_contract"],
                "helper_sha256": self.sandbox.identity["helper"].rsplit("worker-", 1)[-1].removesuffix(".py"),
                "read_helper_sha256": self.sandbox.identity["read_helper"].rsplit("read-", 1)[-1].removesuffix(".py")}
