"""Register agent defaults and derive their runtime and discovery contracts together."""

from ..configuration import declarations

AGENTS = {}


def register(agent_class):
    """Register an imported concrete agent and resolve its shared tool configuration.

    Args:
        agent_class: Agent with a unique name, defaults and optional tool_configs.
            Scalar defaults are inferred; Option metadata is unwrapped so existing
            callers still receive ordinary defaults and credential environment names.
    """
    if agent_class.name in AGENTS:
        raise ValueError(f"Agent already registered: {agent_class.name}")
    parent = getattr(agent_class, "configuration", None)
    inherited = parent.fields if parent else {}
    fields = declarations(agent_class.defaults, inherited)
    owners, credentials = {}, {}
    for tool in agent_class.tool_configs:
        for key, spec in declarations(tool.defaults).items():
            if key in fields and (not parent or parent.owners.get(key) != tool.name
                                  or fields[key].default != spec.default):
                raise ValueError(f"Duplicate configuration: {key}")
            fields[key], owners[key] = spec, tool.name
        credentials.update(tool.credentials)
    runtime = declarations(agent_class.runtime_defaults, inherited)
    if runtime.keys() & fields.keys():
        raise ValueError("Runtime and tool option names must be distinct")
    if credentials.keys() & (fields.keys() | runtime.keys()):
        raise ValueError("Credentials cannot also be public configuration")
    agent_class.defaults = {key: spec.default for key, spec in fields.items()}
    agent_class.runtime_defaults = {key: spec.default for key, spec in runtime.items()}
    agent_class.configuration = AgentConfiguration(agent_class, {**runtime, **fields}, owners, credentials)
    agent_class.credential_names = {key: value.env for key, value in credentials.items()}
    AGENTS[agent_class.name] = agent_class
    return agent_class


class AgentConfiguration:
    def __init__(self, agent, fields, owners, credentials):
        self.agent, self.fields, self.owners, self.credentials = agent, fields, owners, credentials

    def defaults(self):
        return {**self.agent.runtime_defaults, **self.agent.defaults}

    def public_defaults(self):
        return {key: value for key, value in self.defaults().items()
                if not self.fields[key].internal and not self.fields[key].binding}

    def catalog(self, resources):
        missing = [spec.binding for spec in self.fields.values()
                   if spec.binding and spec.required and not resources.get(spec.binding)]
        fields = [{**spec.schema(key, resources), "default": self.defaults()[key], "tool": self.owners.get(key)}
                  for key, spec in self.fields.items() if not spec.internal and not spec.binding]
        return {"id": self.agent.name, "name": self.agent.__name__.removesuffix("Agent"),
                "available": not missing, "reason": "Requires: " + ", ".join(missing) if missing else "",
                "defaults": self.public_defaults(), "fields": fields,
                "tools": self.agent.configuration_tools(self.defaults()),
                "tool_catalog": self.agent.configuration_tools(),
                "credentials": {key: {"required_when": spec.required_when, "active_when": spec.active_when,
                                      "testable": spec.validator is not None}
                                for key, spec in self.credentials.items()}}

    def validate(self, values, credentials, resources, checks=None):
        """Evaluate configuration and tool readiness using the same declarations as construction.

        Args:
            values: Native public overrides. No application presentation ranges are imposed.
            credentials: In-memory secret values, never returned in the report.
            resources: Operator-owned runtime bindings and capabilities.
            checks: Exact-value results from credential validators, retained by the calling application.
        """
        options = {**self.defaults(), **values}
        fields = {}
        for key, spec in self.fields.items():
            try:
                if spec.binding:
                    options[key] = resources.get(spec.binding, spec.default)
                    if spec.required and not options[key]:
                        raise ValueError(f"Requires: {spec.binding}")
                else:
                    spec.validate(key, options[key], resources)
                fields[key] = {"valid": True, "reason": ""}
            except (ValueError, TypeError) as exc:
                fields[key] = {"valid": False, "reason": str(exc)}
        for key, spec in self.credentials.items():
            fields[key] = spec.validate(credentials.get(key, ""), options, (checks or {}).get(key))
        try:
            tools = self.agent.configuration_tools(options)
        except (KeyError, ValueError, TypeError):
            tools = self.agent.configuration_tools(self.defaults())
        catalog = self.agent.configuration_tools()
        for tool in [*tools, *catalog]:
            failures = {key: fields[key] for key in tool["configuration"] if not fields[key]["valid"]}
            tool.update(valid=not failures, issues=failures)
        active = {key for tool in tools for key in tool["configuration"]}
        issues = {key: value for key, value in fields.items()
                  if not value["valid"] and (key not in self.owners or key in active)}
        if not issues:
            try:
                self.resolve(values, resources)
            except (ValueError, TypeError) as exc:
                issues["configuration"] = {"valid": False, "reason": str(exc)}
        return {"valid": not issues, "issues": issues, "fields": fields, "tools": tools, "tool_catalog": catalog}

    def resolve(self, values, resources):
        """Validate public overrides and inject only declared operator resource bindings.

        Args:
            values: Public agent and shared-tool values keyed by native option name.
            resources: Available runtime resources; bindings can supply container IDs
                or paths, while choice dependencies only test resource availability.
        """
        defaults = {key: value for key, value in self.defaults().items()
                    if not self.fields[key].binding and (not self.fields[key].internal or key in self.owners)}
        if unknown := values.keys() - defaults.keys():
            raise ValueError(f"Unsupported {self.agent.name} options: {', '.join(sorted(unknown))}")
        resolved = {**self.defaults(), **values}
        for key, spec in self.fields.items():
            if spec.binding:
                resolved[key] = resources.get(spec.binding, spec.default)
                if spec.required and not resolved[key]:
                    raise ValueError(f"Requires: {spec.binding}")
            elif key not in self.owners:
                spec.validate(key, resolved[key], resources)
        active = {key for tool in self.agent.configuration_tools(resolved) for key in tool["configuration"]}
        for key in active & self.owners.keys():
            self.fields[key].validate(key, resolved[key], resources)
        native = {key: resolved[key] for key in self.agent.defaults}
        native = self.agent.normalize_options(native)
        return self.agent.normalize_runtime({**{key: resolved[key] for key in self.agent.runtime_defaults},
                                             "agent_options": native})

    def validate_credentials(self, options, credentials):
        for key, spec in self.credentials.items():
            if spec.required_when and all(options.get(name) == value for name, value in spec.required_when.items()) \
                    and not credentials.get(key):
                raise ValueError(f"Required credential: {key}")
