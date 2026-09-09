"""Provide contracts and small utilities shared by CBM transports.

The public client depends only on this module. Concrete MCP, CLI, and native
implementations own their process protocols and expose graph bindings as opaque
tokens, so transport selection never leaks into tool routing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from ...utils import ResourceMonitorLike
    from ..binary import CBMBinary
    from .native import NativeHelper

TransportName = Literal["native", "mcp", "cli"]
TRANSPORT_NAMES = frozenset({"native", "mcp", "cli"})
_DELTA_ARGUMENTS = frozenset(
    {
        "delta_closure_overflow",
        "delta_closure_cost_percent",
        "delta_dependent_scope",
        "delta_new_surface",
        "delta_reference_fanout_cap",
        "delta_pair_outputs",
        "delta_pair_refresh_budget",
        "delta_pair_input_missing",
    },
)


class CBMTransportError(RuntimeError):
    """Report a failed or incompatible CBM transport operation."""


@dataclass(frozen=True, slots=True)
class OpenedGraph:
    """Carry transport-neutral graph facts plus its opaque route binding."""

    project: str
    binding: object
    source_root: Path | None = None
    nodes: int | None = None
    edges: int | None = None


class Transport(Protocol):
    """Expose route identity, capability discovery, and owned lifecycle only."""

    name: TransportName

    def supports(self, operation: str) -> bool: ...

    def close(self) -> None: ...


def parse_transport_name(value: str) -> TransportName:
    """Validate one transport selector.

    Args:
        value: Public transport name.

    Returns:
        The selector narrowed to the supported transport-name contract.

    Raises:
        ValueError: The selector does not name a CBM transport.
    """
    if value not in TRANSPORT_NAMES:
        raise ValueError(f"unknown CBM transport: {value}")
    return cast("TransportName", value)


def response_object(envelope: object, key: str) -> dict | None:
    """Find a named JSON object in a frontend response envelope.

    Args:
        envelope: MCP or CLI response value.
        key: Object key to locate recursively.

    Returns:
        The first matching object, if one is present.
    """
    if isinstance(envelope, dict):
        value = envelope.get(key)
        if isinstance(value, dict):
            return value
        for value in envelope.values():
            if found := response_object(value, key):
                return found
    elif isinstance(envelope, list):
        for value in envelope:
            if found := response_object(value, key):
                return found
    elif isinstance(envelope, str) and envelope.lstrip().startswith(("{", "[")):
        try:
            return response_object(json.loads(envelope), key)
        except json.JSONDecodeError:
            return None
    return None


def logical_result(name: str, envelope: Mapping[str, object]) -> dict[str, Any]:
    """Normalize one frontend envelope to the SDK's logical JSON result.

    Args:
        name: Tool name used for diagnostics.
        envelope: MCP or CLI tool result envelope.

    Returns:
        The logical JSON object returned by the CBM engine.

    Raises:
        CBMTransportError: The frontend returned only non-JSON content.
    """
    structured = envelope.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    if structured is not None:
        raise CBMTransportError(f"CBM tool {name} returned non-object structured content")
    content = envelope.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            try:
                decoded = json.loads(block["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    raise CBMTransportError(f"CBM tool {name} did not return a JSON object")


def index_arguments(
    tree: Path,
    project: str,
    mode: str,
    force_full: bool,
    incremental_controls: Mapping[str, str | int] | None,
    target_projects: Sequence[str] | None,
) -> dict[str, object]:
    """Build the frontend form of one repository-index request."""
    arguments: dict[str, object] = {
        "repo_path": str(tree),
        "name": project,
        "mode": mode,
        "persistence": False,
        "force_full": force_full,
    }
    if incremental_controls is not None:
        arguments.update({f"delta_{key}": value for key, value in incremental_controls.items()})
    if target_projects is not None:
        arguments["target_projects"] = list(target_projects)
    return arguments


def checked_index_execution(
    envelope: object,
    force_full: bool,
    incremental_controls: Mapping[str, str | int] | None,
) -> dict[str, Any]:
    """Validate route evidence returned for a requested build policy."""
    execution = index_execution_from_envelope(envelope)
    if force_full and (execution is None or execution.get("route") != "full"):
        raise CBMTransportError("CBM did not confirm the requested force_full build")
    if incremental_controls is not None:
        reported = response_object(envelope, "incremental_controls")
        if reported != dict(incremental_controls):
            raise CBMTransportError("CBM did not confirm the requested incremental controls")
    return execution or {"route": "unreported"}


def index_execution_from_envelope(envelope: object) -> dict | None:
    """Find CBM's machine-readable route evidence in a frontend envelope.

    Args:
        envelope: Logical, MCP, or CLI response value.

    Returns:
        Valid route evidence when CBM reported it.
    """
    execution = response_object(envelope, "index_execution")
    return execution if execution is not None and isinstance(execution.get("route"), str) else None


@dataclass(frozen=True, slots=True)
class _ProjectBinding:
    transport: _ProjectTransport
    project: str
    source_root: Path | None


class _ProjectTransport:
    """Share project-addressed SDK semantics between frontend protocols."""

    name: TransportName

    def __init__(self, binary: CBMBinary):
        self._binary = binary

    @property
    def engine(self) -> CBMBinary:
        return self._binary

    def open_project(self, project: str, source_root: Path | None) -> OpenedGraph:
        binding = _ProjectBinding(self, project, source_root)
        return OpenedGraph(project, binding, source_root)

    def call_tool(
        self,
        binding: object | None,
        name: str,
        arguments: Mapping[str, object],
    ) -> dict[str, Any]:
        values = dict(arguments)
        if binding is not None:
            if not isinstance(binding, _ProjectBinding) or binding.transport is not self:
                raise CBMTransportError("graph handle belongs to another CBM transport")
            supplied = values.get("project")
            if supplied is not None and supplied != binding.project:
                raise CBMTransportError("CBM graph handle disagrees with the tool project")
            values["project"] = binding.project
        source_root = binding.source_root if isinstance(binding, _ProjectBinding) else None
        return logical_result(name, self._call_tool(name, values, source_root))

    def index_repository(
        self,
        tree: Path,
        database_path: Path,
        project: str,
        mode: str,
        *,
        force_full: bool = False,
        incremental_controls: Mapping[str, str | int] | None = None,
        target_projects: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        del database_path
        envelope = self._call_tool(
            "index_repository",
            index_arguments(
                tree,
                project,
                mode,
                force_full,
                incremental_controls,
                target_projects,
            ),
            tree,
        )
        return checked_index_execution(envelope, force_full, incremental_controls)

    def list_projects(self, options: Mapping[str, object]) -> dict[str, Any]:
        return self.call_tool(None, "list_projects", options)

    def delete_project(self, database_path: Path, project: str) -> tuple[bool, str]:
        del database_path
        try:
            result = self.call_tool(None, "delete_project", {"project": project})
        except CBMTransportError as exc:
            return False, str(exc)[-1000:]
        return True, json.dumps(result, ensure_ascii=False)[-1000:]

    def compare_graphs(
        self,
        base: object,
        target: object,
        *,
        limit: int,
        scan_limit: int,
    ) -> dict[str, Any]:
        if (
            not isinstance(base, _ProjectBinding)
            or not isinstance(target, _ProjectBinding)
            or base.transport is not self
            or target.transport is not self
        ):
            raise CBMTransportError("graph handles belong to different CBM transports")
        return logical_result(
            "compare_graphs",
            self._call_tool(
                "compare_graphs",
                {
                    "base_project": base.project,
                    "target_project": target.project,
                    "limit": limit,
                    "scan_limit": scan_limit,
                },
                target.source_root,
            ),
        )

    def _call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        source_root: Path | None,
    ) -> dict[str, Any]:
        raise NotImplementedError


def create_transport(
    name: TransportName,
    *,
    resolve_binary: Callable[[], CBMBinary],
    cache_root: Path,
    timeout: float,
    environment: Mapping[str, str],
    monitor: ResourceMonitorLike,
    native_helper: NativeHelper | str | Path | None,
    native_index_helper: NativeHelper | str | Path | None,
) -> Transport:
    """Construct one concrete transport behind the shared protocol.

    Args:
        name: Access path selected for this transport instance.
        resolve_binary: Lazy authenticated CBM executable resolver used only by
            MCP and CLI.
        cache_root: CBM database directory.
        timeout: Maximum seconds for an operation.
        environment: Child-process environment overrides.
        monitor: Resource observer shared with the owning client.
        native_helper: Compact native query helper override.
        native_index_helper: Full native indexing helper override.

    Returns:
        A transport whose concrete implementation remains private.
    """
    if name == "native":
        from .native import NativeTransport

        return NativeTransport(
            native_helper,
            native_index_helper,
            cache_root,
            timeout,
            environment,
            monitor,
        )

    if name == "mcp":
        from .mcp import MCPTransport

        transport_type = MCPTransport
    else:
        from .cli import CLITransport

        transport_type = CLITransport
    binary = resolve_binary()
    return transport_type(binary, cache_root, timeout, monitor, environment)
