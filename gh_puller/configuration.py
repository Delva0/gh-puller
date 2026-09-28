"""Describe native configuration values and ownership without prescribing application controls.

Plain defaults need no metadata. Options add enums, dependencies or operator-only
bindings; tool groups share settings and credential declarations across agents.
Credentials describe environment names, never secret values.
"""

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Option:
    default: object
    choices: tuple = ()
    requires: dict = field(default_factory=dict)
    binding: str = ""
    required: bool = False
    internal: bool = False
    value_type: type | None = None
    description: str = ""

    def schema(self, key, resources):
        kind = self.value_type or type(self.default)
        return {"key": key, "default": self.default,
                "type": {str: "string", bool: "boolean", int: "integer", float: "number"}.get(kind, "json"),
                "nullable": self.default is None, "description": self.description,
                "choices": [{"value": value, "reason": self.unavailable(value, resources)}
                            for value in self.choices]}

    def unavailable(self, value, resources):
        missing = [name for name in self.requires.get(value, ()) if not resources.get(name)]
        return "Requires: " + ", ".join(missing) if missing else ""

    def validate(self, key, value, resources):
        if self.choices:
            if not any(type(value) is type(choice) and value == choice for choice in self.choices):
                raise ValueError(f"{key} must be one of {self.choices}")
            if reason := self.unavailable(value, resources):
                raise ValueError(f"{key}: {reason}")
        elif value is not None or self.default is not None:
            kind = self.value_type or type(self.default)
            if kind in {str, bool, int, float} and not (
                    type(value) is kind or (kind is float and type(value) is int)):
                raise ValueError(f"{key} must be {kind.__name__}")


def option(default, **metadata):
    """Annotate a default only where its value cannot express the contract.

    Args:
        default: Native runtime value, also used to infer its scalar type.
        metadata: Option attributes; choices and dependencies describe capabilities,
            binding identifies an operator-provided resource, internal hides runtime
            controls from public configuration, and value_type types nullable values.
    """
    return Option(default, **metadata)


@dataclass(frozen=True)
class Credential:
    env: tuple[str, ...]
    required_when: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolConfig:
    name: str
    defaults: dict = field(default_factory=dict)
    credentials: dict[str, Credential] = field(default_factory=dict)


def declarations(values, inherited=None):
    inherited = inherited or {}
    return {key: value if isinstance(value, Option) else
            replace(inherited[key], default=value) if key in inherited else option(value)
            for key, value in values.items()}
