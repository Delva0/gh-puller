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
                "tools": [{"id": tool.name, "credentials": list(tool.credentials)} for tool in self.agent.tool_configs]}

    def resolve(self, values, resources):
        """Validate public overrides and inject only declared operator resource bindings.

        Args:
            values: Public agent and shared-tool values keyed by native option name.
            resources: Available runtime resources; bindings can supply container IDs
                or paths, while choice dependencies only test resource availability.
        """
        defaults = self.public_defaults()
        if unknown := values.keys() - defaults.keys():
            raise ValueError(f"Unsupported {self.agent.name} options: {', '.join(sorted(unknown))}")
        resolved = {**self.defaults(), **values}
        for key, spec in self.fields.items():
            if spec.binding:
                resolved[key] = resources.get(spec.binding, spec.default)
                if spec.required and not resolved[key]:
                    raise ValueError(f"Requires: {spec.binding}")
            else:
                spec.validate(key, resolved[key], resources)
        native = {key: resolved[key] for key in self.agent.defaults}
        native = self.agent.normalize_options(native)
        return self.agent.normalize_runtime({**{key: resolved[key] for key in self.agent.runtime_defaults},
                                             "agent_options": native})

    def validate_credentials(self, options, credentials):
        for key, spec in self.credentials.items():
            if spec.required_when and all(options.get(name) == value for name, value in spec.required_when.items()) \
                    and not credentials.get(key):
                raise ValueError(f"Required credential: {key}")
