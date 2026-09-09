"""Implement the one-process-per-call CLI transport for the CBM client.

The transport owns only CLI child processes and normalizes their responses to
the shared transport contract. CBM service lifecycle remains outside this package.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING, Any

from ._transport import (
    _DELTA_ARGUMENTS,
    CBMTransportError,
    ResourceMonitorLike,
    _ProjectTransport,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .binary import CBMBinary


class CLITransport(_ProjectTransport):
    """Launch an independent CBM CLI process for every operation."""

    name = "cli"

    def __init__(
        self,
        binary: CBMBinary,
        cache_root: Path,
        timeout: float,
        monitor: ResourceMonitorLike,
        environment: Mapping[str, str],
    ):
        super().__init__(binary)
        self.cache_root = cache_root
        self.timeout = timeout
        self.monitor = monitor
        self.environment = dict(environment)

    def _run(self, arguments: list[str]) -> dict[str, Any]:
        process = subprocess.Popen(
            [str(self._binary.path), "cli", "--json", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env={
                **os.environ,
                **self.environment,
                "CBM_CACHE_DIR": str(self.cache_root),
            },
        )
        self.monitor.add_child(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise CBMTransportError(f"CBM CLI request timed out after {self.timeout:g}s") from None
        finally:
            self.monitor.remove_child(process.pid)
        self.monitor.sample()
        if self.monitor.exceeded:
            raise CBMTransportError("memory limit exceeded while running CBM")
        if process.returncode:
            raise CBMTransportError(f"CBM CLI exited {process.returncode}: {stderr[-2000:]}")
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise CBMTransportError(f"CBM CLI returned invalid JSON: {exc}") from exc
        if not isinstance(envelope, dict):
            raise CBMTransportError("CBM CLI returned a non-object JSON envelope")
        nested_result = envelope.get("result")
        if envelope.get("isError") or (
            isinstance(nested_result, dict) and nested_result.get("isError")
        ):
            raise CBMTransportError(f"CBM CLI request failed: {stdout[-2000:]}")
        return envelope

    def _call_tool(self, name: str, arguments: Mapping[str, object]) -> dict[str, Any]:
        return self._run(
            [name, json.dumps(arguments, separators=(",", ":"), ensure_ascii=False)],
        )

    def capabilities(self) -> frozenset[str]:
        """Inspect capabilities through CLI-generated tool help."""
        try:
            result = subprocess.run(
                [str(self._binary.path), "cli", "index_repository", "--help"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=min(self.timeout, 30),
                check=False,
                env={
                    **os.environ,
                    **self.environment,
                    "CBM_CACHE_DIR": str(self.cache_root),
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CBMTransportError(f"cannot inspect CBM CLI capabilities: {exc}") from exc
        if result.returncode:
            raise CBMTransportError(
                f"CBM CLI capability inspection exited {result.returncode}: {result.stderr[-2000:]}",
            )
        capabilities = {"repository-index"}
        if "--force-full" in result.stdout:
            capabilities.add("force-full-route")
        delta_flags = {f"--{name.replace('_', '-')}" for name in _DELTA_ARGUMENTS}
        if all(flag in result.stdout for flag in delta_flags):
            capabilities.add("granular-delta-controls")
        return frozenset(capabilities)

    def close(self) -> None:
        """Release no resources because every CLI process is request-scoped."""
