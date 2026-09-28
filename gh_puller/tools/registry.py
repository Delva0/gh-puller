"""One decorated tool definition for function calls and the PTC SDK."""

import json
from dataclasses import dataclass, replace

from jsonschema import Draft202012Validator, ValidationError


class ToolInputError(ValueError):
    """An actionable input error; retrying the same input cannot fix it."""

    def __init__(self, message: str, *, code: str = "invalid_argument", path: str = "", **details):
        super().__init__(message)
        self.details = {"code": code, "path": path, **details}


def input_error(exc: Exception, *, prefix: tuple = ()) -> dict:
    """Keep model-visible errors short, without dumping the entire input schema."""
    error = {"type": type(exc).__name__, "code": "invalid_argument", "message": str(exc)[:600], "retryable": False}
    if isinstance(exc, ToolInputError):
        error.update(exc.details)
    elif isinstance(exc, ValidationError):
        parts = (*prefix, *exc.absolute_path)
        error["path"] = "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)
        error["message"] = exc.message[:400]
        if exc.validator == "oneOf":
            choices = [branch["required"][0] for branch in exc.validator_value if branch.get("required")]
            error.update(message="Choose exactly one entry.", choices=choices)
        elif exc.validator in {"type", "enum", "minimum", "maximum", "minItems", "maxItems", "required"}:
            error["constraint"] = {exc.validator: exc.validator_value}
    elif isinstance(exc, json.JSONDecodeError):
        error.update(code="invalid_json", message=exc.msg, line=exc.lineno, column=exc.colno)
    return error


BATCH_OUTPUT = {"type": "object", "properties": {
    "results": {"type": "array", "items": {"type": "object"}},
}, "required": ["results"]}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    returns: dict
    batch_parameter: str | None = None
    configuration: tuple[str, ...] = ()

    @property
    def definition(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}


def tool(*, description: str, parameters: dict, returns: dict | None = None,
         batch_parameter: str | None = None, configuration: tuple[str, ...] = ()):
    """Register metadata without wrapping the implementation or changing its signature.

    Args:
        description: Tool instructions included in the model-facing definition.
        parameters: JSON Schema for tool inputs, excluding the injected call ID.
        returns: Output schema used by programmatic tool calling.
        batch_parameter: Input array whose items consume individual concurrency slots.
        configuration: Native option and credential keys required by this tool; discovery
            metadata only, excluded from the model-facing definition.
    """
    def decorate(function):
        function.tool_spec = ToolSpec(function.__name__, description, parameters, returns or {"type": "object"},
                                      batch_parameter, configuration)
        return function

    return decorate


class ToolProvider:
    """Capture declarations once; handlers remain replaceable on each provider instance."""

    tool_specs: tuple[ToolSpec, ...] = ()

    def handler(self, name: str):
        return getattr(self, name)

    def normalize_arguments(self, name: str, args: dict):
        """Providers may accept documented wire coercions without weakening JSON schemas."""
        return args

    def load_events(self, events):
        """Replay this tool's observations into a fresh provider; stateless tools do nothing.

        Args:
            events: Ordered observations from the owning Agent, with tool files restored.
                Implementations read their own routes and publish their restored facts.
                Network connections and running work are not replayed.
        """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        inherited = {spec.name: spec for spec in cls.tool_specs}
        inherited.update({value.tool_spec.name: value.tool_spec for value in vars(cls).values()
                          if hasattr(value, "tool_spec")})
        cls.tool_specs = tuple(inherited.values())


def installed_specs(provider, name=None):
    """Resolve a public name, or per-tool names for a provider, without mutating declarations."""
    if isinstance(name, str):
        if len(provider.tool_specs) != 1:
            raise ValueError("Use a name mapping when installing a provider with multiple tools")
        names = {provider.tool_specs[0].name: name}
    elif name is None or isinstance(name, dict):
        names = name or {}
    else:
        raise TypeError("Tool name must be a string, mapping, or None")
    if unknown := names.keys() - {spec.name for spec in provider.tool_specs}:
        raise ValueError(f"Cannot rename unregistered tools: {', '.join(sorted(unknown))}")
    result = []
    for spec in provider.tool_specs:
        public_name = names.get(spec.name)
        if public_name is None:
            public_name = spec.name
        if not isinstance(public_name, str) or not public_name.strip():
            raise ValueError("Installed tool names must be nonempty strings")
        result.append((spec.name, replace(spec, name=public_name)))
    return result


def tool_definitions(*providers) -> list[dict]:
    """Accept providers or (provider, public name) pairs, just like ToolRegistry."""
    return [spec.definition for entry in providers
            for _, spec in installed_specs(*(entry if isinstance(entry, tuple) else (entry, None)))]


def tool_configuration(*providers, shared=()):
    """Discover registered identities and installed names without constructing providers.

    Args:
        providers: Provider classes or instances with the same aliases used by ToolRegistry.
        shared: Configuration keys applied to all outputs by the enclosing agent.

    Returns:
        Registered function names as identities, with call_name retaining the installed
        alias. Backends sharing a call alias remain distinct configuration targets.
    """
    return [{"id": registered, "call_name": spec.name,
             "configuration": list(dict.fromkeys((*spec.configuration, *shared)))}
            for entry in providers
            for registered, spec in installed_specs(*(entry if isinstance(entry, tuple) else (entry, None)))]


def typescript_type(schema: dict) -> str:
    """Render the JSON subset used by the tools; validation still uses the original schema."""
    if "enum" in schema:
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in schema["enum"])
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(typescript_type({**schema, "type": item}) for item in kind)
    if kind == "array":
        return f"Array<{typescript_type(schema.get('items', {}))}>"
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        fields = [f"{json.dumps(name)}{'' if name in required else '?'}: {typescript_type(value)}"
                  for name, value in properties.items()]
        extra = schema.get("additionalProperties", True)
        if extra is not False:
            fields.append(f"[key: string]: {typescript_type(extra) if isinstance(extra, dict) else 'any'}")
        return "{ " + "; ".join(fields) + " }"
    return {"string": "string", "integer": "number", "number": "number", "boolean": "boolean",
            "null": "null"}.get(kind, "any")


class ToolRegistry:
    def __init__(self, *providers):
        self.specs, self.handlers, self.normalizers = {}, {}, {}
        self.registered_names, self.providers = {}, {}
        for entry in providers:
            self.install(*(entry if isinstance(entry, tuple) else (entry, None)))

    def install(self, provider, name=None) -> None:
        """Install declared tools; None keeps each registration name, strings rename one tool."""
        pending, names = [], set(self.specs)
        for registered_name, spec in installed_specs(provider, name):
            if spec.name in names:
                raise ValueError(f"Duplicate tool: {spec.name}")
            names.add(spec.name)
            pending.append((registered_name, spec, provider.handler(registered_name)))
        for registered_name, spec, handler in pending:
            self.specs[spec.name] = spec
            self.handlers[spec.name] = handler
            self.normalizers[spec.name] = provider.normalize_arguments
            self.registered_names[spec.name] = registered_name
            self.providers[spec.name] = provider

    async def call(self, name: str, call_id: str, args: dict):
        """Validate the envelope once and isolate malformed members of declared batches."""
        spec = self.specs[name]
        args = self.normalizers[name](self.registered_names[name], args)
        key, schema = spec.batch_parameter, spec.parameters
        if key is None:
            Draft202012Validator(schema).validate(args)
            return await self.handlers[name](call_id, **args)
        array_schema = schema["properties"][key]
        envelope = {**schema, "properties": {**schema["properties"], key: {**array_schema, "items": {}}}}
        Draft202012Validator(envelope).validate(args)
        validator = Draft202012Validator(array_schema["items"])
        results, valid, positions = [], [], []
        for index, item in enumerate(args[key]):
            error = next(validator.iter_errors(item), None)
            if error:
                original = item if isinstance(item, dict) else {"input": item}
                results.append({**original, "error": input_error(error, prefix=(key, index)), "fatal": False})
            else:
                positions.append(index)
                valid.append(item)
                results.append(None)
        if not valid:
            return {"results": results}
        output = await self.handlers[name](call_id, **{**args, key: valid})
        if len(valid) == len(results):
            return output
        for index, result in zip(positions, output["results"], strict=True):
            results[index] = result
        return {**output, "results": results}

    def load_events(self, events):
        """Restore each provider once, even when it exposes multiple tools or aliases.

        Args:
            events: Ordered observations from the owning Agent.
        """
        for provider in dict.fromkeys(self.providers.values()):
            provider.load_events(events)

    @property
    def definitions(self) -> list[dict]:
        return [spec.definition for spec in self.specs.values()]

    def sdk(self) -> str:
        lines = ["declare const tools: {"]
        for name, spec in self.specs.items():
            # Keep constraints and field help as well as convenient TypeScript signatures.
            help_text = spec.description + "\nJSON Schema: " + json.dumps(spec.parameters, ensure_ascii=False)
            lines.append("/** " + help_text.replace("*/", "* /") + " */")
            lines.append(f"{json.dumps(name)}(args: {typescript_type(spec.parameters)}): "
                         f"Promise<{typescript_type(spec.returns)}>;")
        return "\n".join([*lines, "};"])
