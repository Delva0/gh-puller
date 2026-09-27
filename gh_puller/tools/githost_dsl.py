"""Shared Git-host DSL support: fields, evidence and recoverable results."""

import asyncio
import copy
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any

from graphql import (
    ArgumentNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    GraphQLArgument,
    GraphQLError,
    GraphQLField,
    GraphQLID,
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLScalarType,
    GraphQLString,
    NameNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    TypeInfo,
    TypeInfoVisitor,
    ast_from_value,
    get_named_type,
    get_operation_ast,
    is_input_object_type,
    print_ast,
    print_type,
    value_from_ast_untyped,
)
from graphql.execution.collect_fields import collect_fields
from graphql.execution.values import get_variable_values
from graphql.language import Visitor, visit
from graphql.pyutils import did_you_mean, suggestion_list

from .githost_api_utils import (
    DISPLAY_OPTIONS,
    TEXT_OPTIONS,
    continue_view,
    project_json,
    response_view,
    view_error,
)
from .registry import ToolInputError, input_error
from .utils import select_json


def field_key(name, arguments=None):
    return name, json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Evidence:
    result_id: str
    at: str = ""

    def child(self, key):
        escaped = str(key).replace("~", "~0").replace("/", "~1")
        return Evidence(self.result_id, self.at + "/" + escaped)


@dataclass(frozen=True)
class Field:
    """One bound native field. Conditions and atomicity are supplied by the platform."""

    key: tuple
    output: str
    kind: str = ""
    children: tuple | None = None
    on: tuple = ()
    atomic: bool = False
    probe: bool = False
    origin: Any = dataclass_field(default=None, compare=False, repr=False)


@dataclass
class Object:
    typename: str
    identity: Any = None
    fields: dict = dataclass_field(default_factory=dict)
    attributes: dict = dataclass_field(default_factory=dict)


@dataclass
class Value:
    data: Any
    evidence: frozenset = frozenset()

    @property
    def sources(self):
        return {e.result_id for e in self.evidence}


@dataclass(frozen=True)
class Identity:
    kind: str
    key: Any = None
    addresses: tuple = ()


def fields(selections, kind):
    """Unify repeated selections only when their arguments and output names agree."""
    merged = {}
    for original in selections or ():
        selected = original
        if selected.on and kind not in selected.on:
            continue
        identity = selected.output, selected.key
        previous = merged.get(identity)
        if previous and previous.children is not None and selected.children is not None:
            selected = replace(selected, children=previous.children + selected.children)
        merged[identity] = selected
    return merged.values()


class EvidenceIndex:
    """One provider/credential/query scope. Absence, explicit null and partial pages stay distinct."""

    def __init__(self):
        self.root = Object("Query")
        self.objects = {}
        self.addresses = {}

    def object(self, identity, old=None):
        key = identity.kind, identity.key
        obj = self.objects.get(key) if identity.key is not None else old
        if not isinstance(obj, Object) or (obj.typename, obj.identity) != key:
            obj = Object(*key)
        if identity.key is not None:
            self.objects[key] = obj
        for address in identity.addresses:
            self.addresses[address] = obj
        return obj

    def entry(self, selected, parent, resolve):
        entry = parent.fields.get(selected.key)
        if entry is None and resolve:
            entry = resolve(selected, parent)
        return entry

    def read(self, selected, parent=None, *, resolve=None):
        parent = self.root if parent is None else parent
        entry = self.entry(selected, parent, resolve)
        if entry is None:
            return None, False, set()
        return self._read(entry, selected.children, resolve)

    def _read(self, entry, selections, resolve):
        value, sources = entry.data, set(entry.sources)
        if value is None or selections is None:
            return copy.deepcopy(value), True, sources
        if isinstance(value, list):
            result, complete = [], True
            for item in value:
                data, found, used = self._read(item, selections, resolve)
                result.append(data)
                complete &= found
                sources.update(used)
            return result, complete, sources
        if not isinstance(value, Object):
            return None, False, sources
        result, complete = {}, True
        for selected in fields(selections, value.typename):
            data, found, used = self.read(selected, value, resolve=resolve)
            complete &= found
            if self.entry(selected, value, resolve) is not None:
                if selected.output in result and isinstance(data, dict):
                    result[selected.output].update(data)
                else:
                    result[selected.output] = data
            sources.update(used)
        return result, complete, sources

    def missing(self, selected, parent=None, *, resolve=None):
        parent = self.root if parent is None else parent
        if self.read(selected, parent, resolve=resolve)[1]:
            return None
        entry = self.entry(selected, parent, resolve)
        if entry is None or not isinstance(entry.data, Object) or selected.atomic:
            return selected
        children = []
        for child in fields(selected.children, entry.data.typename):
            needed = self.missing(child, entry.data, resolve=resolve)
            if needed is not None or child.probe:
                children.append(child if needed is None else needed)
        return replace(selected, children=tuple(children))

    def write(self, selected, data, evidence, identify, parent=None):
        parent = self.root if parent is None else parent
        old = parent.fields.get(selected.key)
        parent.fields[selected.key] = self._write(selected, data, evidence, identify, old)
        return parent.fields[selected.key]

    def _write(self, selected, data, evidence, identify, old=None):
        if data is None or selected.children is None:
            return Value(copy.deepcopy(data), frozenset({evidence}))
        if isinstance(data, list):
            # A fetched page replaces membership/order; never join rows by their positions.
            return Value(
                [self._write(selected, item, evidence.child(i), identify) for i, item in enumerate(data)],
                frozenset({evidence}),
            )
        identity = identify(selected.kind, data)
        obj = self.object(identity, old.data if old else None)
        for child in fields(selected.children, obj.typename):
            if child.output in data:
                self.write(child, data[child.output], evidence.child(child.output), identify, obj)
        return Value(obj, frozenset({evidence}))

    def overlay(self):
        """Failed/partial reads can be displayed without entering reusable evidence."""
        return copy.deepcopy(self)


def format_error(error):
    formatted = error.formatted
    if isinstance(error.original_error, ToolInputError):
        formatted["extensions"] = {**input_error(error.original_error), **formatted.get("extensions", {})}
    return formatted


@dataclass
class DSLContext:
    adapter: object
    call_id: str
    source: str
    raw_variables: dict
    document: object
    operation: object
    variables: dict
    fragments: dict
    sources: list = dataclass_field(default_factory=list)
    pages: list = dataclass_field(default_factory=list)
    views: list = dataclass_field(default_factory=list)
    errors: list = dataclass_field(default_factory=list)
    tasks: set = dataclass_field(default_factory=set)
    state: dict = dataclass_field(default_factory=dict)
    reused: bool = False
    closed: bool = False

    def task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        if self.closed:
            task.cancel()
        return task

    async def close(self):
        self.closed = True
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def read(self, request, path=(), *, partial=False, inspect_saved=False):
        result = await self.task(self.adapter.read(self.call_id, request))
        if result.get("result_id"):
            self.sources.append(result["result_id"])
        readable_saved = (
            inspect_saved and "result_id" in request and "resolved_request" in result and result.get("body_file")
        )
        if result.get("error") and not (partial or readable_saved):
            error = result["error"]
            raise GraphQLError(
                error.get("message", "Native read failed."),
                extensions={**error, **{k: result[k] for k in ("status", "result_id", "retry_at") if k in result}},
            )
        return result


SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "description": "A DSL query using GraphQL syntax."},
        "variables": {"type": "object", "description": "Values for variables declared in the query."},
        "operation_name": {"type": "string", "description": "Operation to execute in a multi-operation document."},
        "max_chars": {
            "type": "integer",
            "minimum": 1,
            "maximum": 40000,
            "default": 16000,
            "description": "Preview and continuation budget per root (minimum 1024 for recovery metadata); "
            "full selected data remains saved.",
        },
        "refresh": {
            "type": "boolean",
            "default": False,
            "description": "Fetch current data instead of reusing this question's reads.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


OUTPUT = {
    "type": "object",
    "properties": {
        "data": {"type": ["object", "null"]},
        "errors": {"type": "array", "items": {"type": "object"}},
        "extensions": {"type": "object"},
    },
    "anyOf": [{"required": ["data"]}, {"required": ["errors"]}],
}


def json_literal(node, variables=None):
    class Check(Visitor):
        def enter_enum_value(self, current, *_):
            raise GraphQLError("JSON strings must be quoted.", nodes=current)

    visit(node, Check())
    return value_from_ast_untyped(node, variables)


JSON = GraphQLScalarType(
    "JSON",
    serialize=lambda value: value,
    parse_value=lambda value: value,
    parse_literal=json_literal,
    description="Opaque JSON. Select this scalar without a sub-selection.",
)


def dsl_fields():
    return {
        "schema": GraphQLField(
            GraphQLString,
            args={
                "type": GraphQLArgument(GraphQLNonNull(GraphQLString), default_value="Query"),
                "fields": GraphQLArgument(GraphQLList(GraphQLNonNull(GraphQLString))),
            },
            description="DSL schema help as GraphQL SDL. Defaults to Query. Omit fields for compact "
            "signatures; select fields for descriptions, argument meanings and immediate output/input types. "
            "Unknown fields are reported alongside valid selections. No network reads; introspection also works.",
        ),
        "get": GraphQLField(
            JSON,
            args={"request": GraphQLArgument(GraphQLNonNull(JSON))},
            description="Read a native API endpoint or a saved response. "
            "A field absent from a DSL type may still exist in the API. "
            "Returns opaque JSON, text or binary metadata; no sub-selection. "
            "request uses path for a native endpoint or result_id for a saved response (no network). "
            "params contains upstream URL parameters. Local json_pointer, fields, view and max_chars "
            "are siblings of params, not API parameters. json_pointer selects a JSON value; "
            "fields maps output names to JSON Pointers relative to that object or each array item. "
            'Example: get(request:{result_id:"RESULT_ID",json_pointer:"/items",'
            'fields:{number:"/number",title:"/title"}}). Use the actual saved shape: a native page '
            'and a pagination collection may have different wrappers. view:"full" preserves native fields; '
            'view:"text" decodes file contents. paginate collects native pages using max_pages/max_items '
            "and, for wrapped lists, items_pointer. Returned next_request objects can be passed unchanged.",
        ),
        "saved": GraphQLField(
            JSON,
            args={
                "id": GraphQLArgument(GraphQLNonNull(GraphQLID)),
                "at": GraphQLArgument(GraphQLString, default_value=""),
                "fields": GraphQLArgument(GraphQLList(GraphQLNonNull(GraphQLString))),
                **{
                    name: GraphQLArgument(GraphQLInt)
                    for name in ("skip", "offset", "max_chars", "start_line", "max_lines")
                },
                "find": GraphQLArgument(GraphQLString),
            },
            description="Read an exact saved response locally. at is a JSON Pointer; fields projects JSON. "
            "skip omits array items. Text windows use offset/max_chars or start_line/max_lines. "
            "Returns a JSON scalar.",
        ),
    }


def schema_request(name=None, fields=None):
    arguments = []
    if name is not None:
        arguments.append(literal("type", name, GraphQLString))
    if fields is not None:
        arguments.append(literal("fields", fields, GraphQLList(GraphQLNonNull(GraphQLString))))
    return {"query": query_document(field("schema", arguments=arguments))}


def field_suggestions(type_, name):
    fields = getattr(type_, "fields", {})
    # A native operation's spelling can differ from its return type (repo -> Repository).
    by_type = [
        n for n, definition in fields.items() if get_named_type(definition.type).name.casefold() == name.casefold()
    ]
    return list(dict.fromkeys([*by_type, *suggestion_list(name, list(fields))]))[:3]


def validation_errors(schema, document, errors):
    """Attach executable local help to standard validation errors; never repair/execute invalid queries."""
    info, locations = TypeInfo(schema), {}

    class Locate(Visitor):
        def __init__(self):
            super().__init__()
            self.stack = []

        def enter(self, node, *_):
            target = self.stack[-1] if self.stack else None
            if isinstance(node, FieldNode) and (parent := info.get_parent_type()):
                name = node.name.value
                known = info.get_field_def() is not None
                candidates = field_suggestions(parent, name) if not known else []
                selected = [name] if name in getattr(parent, "fields", {}) else None
                # Lexical suggestions alone do not identify intent (Commit.title -> files).
                # Exact return-type matches can target a native root; otherwise show its parent type.
                if candidates and get_named_type(parent.fields[candidates[0]].type).name.casefold() == name.casefold():
                    selected = [candidates[0]]
                target = {"type": parent.name, "field": name, "schema_request": schema_request(parent.name, selected)}
                if not known:
                    target["suggested_fields"] = candidates
            self.stack.append(target)
            if target:
                locations[id(node)] = target

        def leave(self, *_):
            self.stack.pop()

    visit(document, TypeInfoVisitor(info, Locate()))
    result = []
    for error in errors:
        formatted = error.formatted
        target = next((locations[id(n)] for n in error.nodes or () if id(n) in locations), None)
        if target:
            formatted["extensions"] = {**formatted.get("extensions", {}), "code": "dsl_validation", **target}
        result.append(formatted)
    return result


def schema_help(schema, name="Query", fields=None):
    """Print the actual executable schema, without a second vocabulary or recursive catalog dump."""
    type_ = schema.get_type(name)
    if type_ is None:
        names = [n for n in schema.type_map if not n.startswith("__")]
        # Derive candidates from the schema, including a root's return type (repo -> Repository).
        roots = schema.query_type.fields
        root_matches = [n for n in roots if n.casefold() == name.casefold()]
        candidates = [get_named_type(roots[n].type).name for n in root_matches]
        candidates += suggestion_list(name, names)
        candidates += sorted(
            (n for n in names if len(name) >= 3 and name.casefold() in n.casefold()),
            key=lambda n: (len(n), n),
        )
        candidates = list(dict.fromkeys(candidates))[:5]
        raise GraphQLError(
            f"Unknown type {name!r}." + did_you_mean(candidates) + " Use schema to discover query roots.",
            extensions={
                "code": "unknown_schema_type",
                "suggested_types": candidates,
                "schema_request": schema_request(candidates[0] if candidates else None),
            },
        )
    selected = getattr(type_, "fields", {})
    notices = []
    if fields is not None:
        unknown = list(dict.fromkeys(n for n in fields if n not in selected))
        suggestions = {n: field_suggestions(type_, n) for n in unknown}
        selected = {n: selected[n] for n in dict.fromkeys(fields) if n in selected}
        for unknown_name, candidates in suggestions.items():
            notices.append(f"# Unknown field {json.dumps(unknown_name)} on {name}." + did_you_mean(candidates))
        if not selected:
            raise GraphQLError(
                f"Select existing fields of {name}; unknown: {', '.join(unknown) or '(empty selection)'}. "
                f"Use schema(type:{json.dumps(name)}) to list them.",
                extensions={
                    "code": "unknown_schema_fields",
                    "unknown_fields": unknown,
                    "suggested_fields": suggestions,
                    "schema_request": schema_request(name),
                },
            )

    def excerpt(target, definitions=None, *, compact=False):
        if not hasattr(target, "fields"):
            return print_type(target)
        result = copy.copy(target)
        result.fields = {
            n: copy.copy(d) for n, d in (definitions if definitions is not None else target.fields).items()
        }
        if compact:
            result.description = None
            for definition in result.fields.values():
                definition.description = None
                if hasattr(definition, "args"):
                    definition.args = {n: copy.copy(a) for n, a in definition.args.items()}
                    for argument in definition.args.values():
                        argument.description = None
            return print_type(result)
        origins = {}
        for field_name, definition in result.fields.items():
            for source in definition.extensions.get("native_sources", []):
                label = source["operation"] + (" (observed response)" if source.get("observed_evidence") else "")
                origins.setdefault(label, set()).add(field_name)
        if origins:
            note = "Native field sources (Live responses may omit fields):\n" + "\n".join(
                f"{operation}: {', '.join(sorted(names))}" for operation, names in sorted(origins.items())
            )
            result.description = "\n".join(filter(None, (result.description, note)))
        return print_type(result)

    output = [*notices, excerpt(type_, selected, compact=fields is None)]
    # A selected operation's item shape and enum/input arguments are useful together. Only
    # input objects recurse: expanding output relations would pull in the entire host schema.
    included = {name}

    def include(target):
        target = get_named_type(target)
        if target.name in included or target.name in {"String", "Int", "Float", "Boolean", "ID"}:
            return
        included.add(target.name)
        output.append(excerpt(target, compact=not is_input_object_type(target)))
        if target.extensions.get("connection"):
            include(target.fields["nodes"].type)
        if is_input_object_type(target):
            for definition in target.fields.values():
                include(definition.type)

    if fields is not None:
        for definition in selected.values():
            include(definition.type)
            for argument in getattr(definition, "args", {}).values():
                include(argument.type)
    return "\n\n".join(output)


def changed(node, **values):
    result = copy.copy(node)
    for name, value in values.items():
        setattr(result, name, value)
    return result


def selection(*nodes):
    return SelectionSetNode(selections=tuple(nodes))


def field(name, *, alias=None, arguments=(), children=None):
    return FieldNode(
        name=NameNode(value=name),
        alias=NameNode(value=alias) if alias else None,
        arguments=tuple(arguments),
        selection_set=children,
    )


def key(node):
    return (node.alias or node.name).value


def literal(name, value, type_):
    return ArgumentNode(name=NameNode(value=name), value=ast_from_value(value, type_))


def query_document(*nodes):
    return print_ast(
        DocumentNode(
            definitions=(OperationDefinitionNode(operation=OperationType.QUERY, selection_set=selection(*nodes)),),
        ),
    )


def get_request(request):
    # JSON scalars can contain keys that are not GraphQL names; variables preserve them exactly.
    return {"query": "query($request:JSON!){get(request:$request)}", "variables": {"request": request}}


class DSLExtensions:
    """DSL fields, query-scoped evidence and result recovery shared by both platforms.

    The platform adapter supplies its schema and resolvers; native reads are callbacks
    into the platform tool module, never protocol dispatch in the HTTP client.
    """

    def __init__(self, api, read, content):
        self.api, self.read, self.response_content = api, read, content
        self._active_evidence = ContextVar("dsl_evidence", default=None)
        self.begin_query()

    def begin_query(self):
        self._evidence = EvidenceIndex()
        self._locks = {}

    @contextmanager
    def evidence_scope(self):
        token = self._active_evidence.set((self._evidence, self._locks))
        try:
            yield
        finally:
            self._active_evidence.reset(token)

    @property
    def evidence(self):
        return self._active_evidence.get()[0]

    def lock(self, identity):
        return self._active_evidence.get()[1].setdefault(identity, asyncio.Lock())

    def validation_errors(self, document, errors):
        return validation_errors(self.schema(), document, errors)

    async def resolve_dsl_field(self, context, info, args):
        if info.field_name == "schema":
            return schema_help(info.schema, args["type"], args.get("fields"))
        if info.field_name == "get":
            request = args["request"]
        else:
            request = {
                "result_id": args["id"],
                "json_pointer": args.get("at", ""),
                **{k: v for k, v in args.items() if k not in {"id", "at", "skip"}},
            }
            if request.get("fields"):
                request["fields"] = {
                    name: "/" + name.replace("~", "~0").replace("/", "~1") for name in request["fields"]
                }
        result = await context.read(request, info.path.as_list(), inspect_saved=True)
        metadata = self.api._saved(result["result_id"])
        resolved = result.get("resolved_request", request)
        content = self.response_content(metadata, enrich=resolved.get("view", "compact") == "compact")
        options = {k: v for k, v in resolved.items() if k in DISPLAY_OPTIONS}
        try:
            default_file = (
                content.is_json
                and content.is_file(content.data)
                and not any(k in options for k in ("view", "json_pointer", "fields"))
            )
            if (
                content.is_json
                and not default_file
                and options.get("view") not in {"raw", "text"}
                and not (set(options) & TEXT_OPTIONS)
            ):
                value = select_json(content.data, options.get("json_pointer", ""))
                if "skip" in args:
                    if not isinstance(value, list) or args["skip"] < 0:
                        raise GraphQLError("saved(skip:) requires an array and a nonnegative item offset.")
                    value = value[args["skip"] :]
                if options.get("fields"):
                    missing = []
                    value = project_json(value, options["fields"], "", missing)
                    missing = [item for item in missing if item["reason"] == "missing_projection_field"]
                    if missing:
                        raise GraphQLError(
                            "Some projected fields are absent from saved JSON.",
                            extensions={"omissions": missing, "result_id": result["result_id"]},
                        )
                view = {"data": value, "display_complete": True}
            else:
                view = response_view(
                    content,
                    metadata["headers"],
                    {**options, "max_chars": request.get("max_chars", 40000)},
                    default_file_text=True,
                    structured=True,
                    plain_json_strings=True,
                )
                # The outer display budget must not truncate the value before it is saved.
                # response_view bounds each text window, so join those windows for an unbounded read.
                if "max_chars" not in options and "content" in view:
                    chunks = [view["content"]]
                    while view.get("next_offset") is not None:
                        view = response_view(
                            content,
                            metadata["headers"],
                            {**options, "offset": view["next_offset"], "max_chars": 40000},
                            default_file_text=True,
                            structured=True,
                            plain_json_strings=True,
                        )
                        chunks.append(view["content"])
                    view = {**view, "content": "".join(chunks), "offset": options.get("offset", 0)}
                if following := continue_view(view, options):
                    native = {"result_id": result["result_id"], **following}
                    view["continue_request"] = get_request(native)
        except (ValueError, TypeError, LookupError, GraphQLError) as exc:
            detail = view_error(metadata, resolved, content, exc)
            detail["recovery_request"] = get_request(detail["recovery_request"])
            raise GraphQLError(
                detail["message"],
                extensions={
                    **getattr(exc, "extensions", {}),
                    **detail,
                    "result_id": result["result_id"],
                },
            ) from exc
        context.views.append(
            {
                "path": info.path.as_list(),
                **{
                    k: result[k]
                    for k in ("result_id", "status", "error", "pagination", "collection", "redirect_url")
                    if k in result
                },
                **{k: v for k, v in view.items() if k not in {"content", "data", "text"}},
            },
        )
        return view.get("content", view.get("data", view.get("text", view)))

    def content(self, result):
        return self.response_content(self.api._saved(result["result_id"]), enrich=False)

    def display(self, name, data, result_id, max_chars):
        if not isinstance(data, (dict, list, str)):
            return data, None
        pointer = "/data/" + name.replace("~", "~0").replace("/", "~1")
        saved = dsl_fields()["saved"]
        body = json.dumps(data, ensure_ascii=False).encode()
        budget, allowance = max(max_chars, 1024), max_chars
        while True:
            view = response_view(
                body,
                {"content-type": "application/json"},
                {"max_chars": allowance},
                structured=True,
                plain_json_strings=True,
            )
            preview = view.get("data", view.get("content", data))
            if view.get("display_complete", True):
                return preview, None
            pending = view.get("windows", [])
            if view.get("next_offset") is not None:
                pending = [{"path": "", "offset": view["next_offset"], "total_chars": view["total_chars"]}]
            windows = []
            for window in pending:
                at = pointer + window["path"]
                options = {
                    **window.get("options", {}),
                    **{k: window[k] for k in ("offset", "start_line", "skip") if k in window},
                }
                args = {"id": result_id, "at": at, **options}
                ast = field(
                    "saved",
                    arguments=[literal(k, v, saved.args[k].type) for k, v in args.items() if k in saved.args],
                )
                windows.append({**window, "path": at, "continue_request": {"query": query_document(ast)}})
            display = {
                "path": [name],
                "complete": False,
                "windows": windows,
                "saved_request": {
                    "query": "query($id:ID!,$at:String!){saved(id:$id,at:$at)}",
                    "variables": {"id": result_id, "at": pointer},
                },
            }
            if len(json.dumps(display, ensure_ascii=False)) >= budget:
                # A very wide object can have more omitted fields than fit in its preview.
                # Keep one exact saved read instead of flooding the model with per-field reads.
                display.pop("windows")
                display["omitted_windows"] = len(windows)
            cost = len(json.dumps({"data": preview, "display": display}, ensure_ascii=False))
            if cost <= budget or allowance == 1:
                return preview, display
            allowance = max(1, allowance - (cost - budget))

    def publish(self, context, result, max_chars):
        extensions = {"source_result_ids": sorted(set(context.sources))}
        if context.pages:
            extensions["pages"] = context.pages
        if context.views:
            extensions["reads"] = context.views
        if context.reused:
            extensions["evidence_reused"] = True
        result["extensions"] = extensions
        metadata = self.api._derived(
            self.api.storage.allocate(self.api.provider + "-api"),
            "dsl",
            {"language": self.language, "query": context.source, "variables": context.raw_variables},
            result,
            source_result_ids=extensions["source_result_ids"],
        )
        extensions["result_id"] = metadata["result_id"]
        shown = copy.deepcopy(result)
        schema = self.schema()
        roots = collect_fields(
            schema,
            context.fragments,
            context.variables,
            schema.query_type,
            context.operation.selection_set,
        )
        for name, data in (result.get("data") or {}).items():
            # Schema identifiers and enum values are machine-readable, never text excerpts.
            # The existing tool-result retention layer can offload an intact introspection result.
            if roots[name][0].name.value.startswith("__"):
                continue
            shown["data"][name], display = self.display(name, data, metadata["result_id"], max_chars)
            if display:
                shown["extensions"].setdefault("display", []).append(display)
        return shown


def operation_context(adapter, call_id, query, document, variables, operation_name):
    """Select a DSL read operation and bind its variables."""
    operation = get_operation_ast(document, operation_name)
    if operation is None:
        return None, [{"message": "Select one query with operation_name."}]
    if any(isinstance(d, OperationDefinitionNode) and d.operation != OperationType.QUERY for d in document.definitions):
        return None, [{"message": "This tool exposes read queries only.", "extensions": {"code": "read_only"}}]
    coerced = get_variable_values(adapter.schema(), operation.variable_definitions or (), variables or {})
    if isinstance(coerced, list):
        return None, [error.formatted for error in coerced]
    return DSLContext(
        adapter,
        call_id,
        query,
        variables or {},
        document,
        operation,
        coerced,
        {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)},
    ), []


def execution_result(result, context):
    """Preserve both local execution errors and upstream paths, including null list entries."""
    formatted = result.formatted
    if result.errors:
        formatted["errors"] = [format_error(error) for error in result.errors]
    if context.errors:
        errors = []
        for error in context.errors:
            detail = format_error(error)
            if detail not in errors:
                errors.append(detail)
        known = {(e["message"], tuple(e.get("path", ()))) for e in errors}
        errors.extend(e for e in formatted.get("errors", []) if (e["message"], tuple(e.get("path", ()))) not in known)
        formatted["errors"] = errors
    return formatted
