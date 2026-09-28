"""Validate search-agent runtime options independently of application configuration."""

import math
from dataclasses import asdict, replace

from ..configuration import option
from ..tools.tool_offload import OffloadPolicy
from ..tools.tool_ptc import normalize_ptc
from ..tools.tool_web import SEARCH_BACKENDS

TOOL_RESULT_OPTIONS = {f"tool_result_{name}": name for name in asdict(OffloadPolicy())}


SEARCH_DEFAULTS = {"ptc": option(False, choices=(False, "A", "B"), requires={"A": ("node",), "B": ("node",)})}


def tool_result_policy(policy, options):
    """Resolve session limits before the agent registers its tools."""
    return replace(policy, **{field: options[option] for option, field in TOOL_RESULT_OPTIONS.items()
                             if options.get(option) is not None})


def normalize_search(options):
    options = {**options, "ptc": normalize_ptc(options["ptc"])}
    tool_result_policy(OffloadPolicy(), options)
    return normalize_web(options)


def normalize_web(options):
    if options["web_search_backend"] not in SEARCH_BACKENDS:
        raise ValueError(f"web_search_backend must be one of {', '.join(SEARCH_BACKENDS)}")
    if options["web_search_concurrency"] < 1:
        raise ValueError("web_search_concurrency must be positive")
    interval = options["web_search_interval"]
    if not math.isfinite(interval) or interval < 0:
        raise ValueError("web_search_interval must be finite and non-negative")
    return options


def reasoning_effort(value: str) -> str:
    return value


def model_id(value: str) -> str:
    value = value.strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("Model ID must be nonempty and contain no whitespace")
    return value
