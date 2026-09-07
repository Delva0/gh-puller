"""Check source-preserving diagnostic roles independently of repository vocabulary."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

from copy import deepcopy
from types import SimpleNamespace

import pytest

from . import diagnostic_bindings
from .diagnostic_bindings import bind, evaluate, occurrences, select_mentions


def response(*rows, truncated=False):
    return {"cols": ["name", "label", "lines", "in", "out"], "has_more": truncated,
            "groups": [{"qn_prefix": "probe.pkg", "file": "pkg/core.py", "rows": list(rows)}]}


def test_native_case_insensitive_search_does_not_create_extra_exact_bindings():
    result = bind("TypeError: CacheManager is unsupported", response(
        ["CacheManager", "Class", "1-5"], ["cachemanager", "Variable", "7-8"],
    ))
    assert len(result) == 1 and result[0]["status"] == "unique_name_match"
    assert result[0]["symbols"] == [{"qn": "probe.pkg.CacheManager", "file": "pkg/core.py",
                                       "start": 1, "end": 5, "label": "Class"}]


@pytest.mark.parametrize("name", ["_DeferredValue", "RequestEnvelope", "get_adapter"])
def test_message_names_retain_every_source_span(name):
    log = f"noise\nAttributeError: '{name}' is unsupported\nTypeError: '{name}' failed"
    result = bind(log, response([name, "Class", "1-5"]))
    assert len(result[0]["occurrences"]) == 2
    for item in result[0]["occurrences"]:
        assert log[item["start"]:item["end"]] == name
        assert item["role"] == "diagnostic_message"
        assert item["message_start"] <= item["start"] < item["end"] <= item["message_end"]
    assert select_mentions(result, source_roles=True, node_kinds=True)[0]["symbols"]


def test_command_argument_is_not_a_runtime_definition_even_when_the_name_is_unique():
    log = "subprocess.CalledProcessError: Command '['builder', '--target=CoreObject']' returned non-zero exit status 1."
    result = bind(log, response(["CoreObject", "Variable", "1-1"]))
    assert result[0]["status"] == "unique_name_match"
    assert result[0]["occurrences"][0]["role"] == "command_argument"
    assert select_mentions(result, source_roles=True, node_kinds=False) == []
    assert select_mentions(result, source_roles=False, node_kinds=True)[0]["symbols"]


def test_repeated_name_with_direct_message_evidence_is_not_globally_blacklisted():
    log = ("CalledProcessError: Command '['worker', 'DataLoader']' returned non-zero exit status 1.\n"
           "TypeError: DataLoader is not callable")
    result = bind(log, response(["DataLoader", "Class", "1-9"]))
    assert {item["role"] for item in result[0]["occurrences"]} == {"command_argument", "diagnostic_message"}
    assert select_mentions(result, source_roles=True, node_kinds=True)[0]["symbols"]


@pytest.mark.parametrize("path", ["/opt/ProjectEnv/pkg/a.py", r"D:\\Users\\ProjectEnv\\a.py", "envs/ProjectEnv/a.py"])
def test_path_names_remain_visible_without_seeding_definition_links(path):
    result = bind(f"PermissionError: access denied: '{path}'", response(["ProjectEnv", "Class", "1-9"]))
    assert result[0]["name"] == "ProjectEnv" and result[0]["occurrences"][0]["role"] == "path_like"
    assert select_mentions(result, source_roles=True, node_kinds=False) == []


def test_serialized_traceback_context_is_not_unescaped_or_promoted_to_a_message():
    log = ('RuntimeError: WorkerType failed\\n\\nfrom user code:\\n  File "/env/ProjectEnv/core.py", line 3, in run'
           '\\n    hidden_states = worker_value()\\n\\nSet TRACE_VERBOSE=1')
    items = occurrences(log)
    by_name = {item["name"]: item for item in items}
    assert by_name["WorkerType"]["role"] == "diagnostic_message"
    assert by_name["ProjectEnv"]["role"] == "path_like"
    for name in ("hidden_states", "worker_value", "TRACE_VERBOSE", "nSet"):
        assert by_name[name]["role"] == "serialized_traceback_context"
    assert all(log[item["start"]:item["end"]] == item["name"] for item in items)


def test_windows_path_escape_does_not_start_serialized_context():
    log = r"RuntimeError: D:\new\ProjectEnv\data.bin failed in WorkerType"
    assert next(item for item in occurrences(log) if item["name"] == "WorkerType")["role"] == "diagnostic_message"


@pytest.mark.parametrize("prefix", ["a" * 10000, "fragment\\n" * 1000])
def test_long_nonpath_payload_keeps_following_message_name(prefix):
    items = occurrences("RuntimeError: " + prefix + " WorkerType failed")
    assert items[-1]["name"] == "WorkerType" and items[-1]["role"] == "diagnostic_message"


def test_node_kind_ablation_is_separate_from_source_roles():
    result = bind("ValueError: ProjectName has no compatible implementation", response(
        ["ProjectName", "Section", "2-4"], ["ProjectName", "Folder", ""],
    ))
    assert result[0]["status"] == "ambiguous"
    assert result[0]["symbols"][1]["start"] == 0
    assert len(select_mentions(result, source_roles=True, node_kinds=False)[0]["symbols"]) == 2
    assert select_mentions(result, source_roles=False, node_kinds=True)[0]["symbols"] == []
    assert len(result[0]["symbols"]) == 2


def test_filtering_a_container_does_not_upgrade_an_ambiguous_binding():
    result = bind("TypeError: WidgetType failed", response(
        ["WidgetType", "Section", "1-3"], ["WidgetType", "Class", "4-7"],
    ))
    selected = select_mentions(result, source_roles=True, node_kinds=True)
    assert len(selected[0]["symbols"]) == 1 and selected[0]["status"] == "ambiguous"


def test_missing_and_truncated_lookups_are_not_claimed_resolved():
    assert bind("TypeError: MissingType failed", response())[0]["status"] == "not_found"
    result = bind("TypeError: PartialType failed", response(["PartialType", "Class", "1-4"], truncated=True))
    assert result[0]["status"] == "query_truncated"


def test_supported_model_enumeration_does_not_enter_the_control_vocabulary():
    result = occurrences("ValueError: MissingModel unsupported. Supported models: OtherModel")
    assert [item["name"] for item in result] == ["MissingModel"]


def test_native_column_drift_is_rejected():
    changed = response(["WidgetType", "Class", "1-4"])
    changed["cols"][1] = "file"
    with pytest.raises(ValueError, match="compact columns"):
        bind("TypeError: WidgetType failed", changed)


def test_checkpoint_rejects_a_corpus_with_different_exclusions():
    scope = {"fixture": True}
    data = {"scope": scope}
    lexical = {"scope": scope, "primary_messages": True, "corpus": {"excluded": [1]}}
    registration = {"scope": scope, "sampling": {"development": [1], "held_out": [2], "known_regression": []}}
    with pytest.raises(ValueError, match="registered corpus"):
        evaluate(data, data, lexical, data, registration, None)
    changed = deepcopy(lexical)
    changed["scope"] = {"fixture": False}
    with pytest.raises(ValueError, match="evidence boundaries"):
        evaluate(data, data, changed, data, registration, None)


@pytest.fixture
def checkpoint_inputs(tmp_path, monkeypatch):
    github = tmp_path / "source.sqlite3"
    github.touch()
    scope = {"github": str(github), "repository": "example/project", "cutoff": 10, "selected_digest": "source"}
    log = "TypeError: '/env/BadEnv/core.py' failed; ValidType unavailable"
    native = response(["BadEnv", "Section", "1-4"])
    native["groups"].append({"qn_prefix": "probe.other", "file": "pkg/other.py",
                              "rows": [["ValidType", "Class", "2-8"]]})
    mentions = bind(log, native)
    baseline_mentions = [{"name": item["name"], "status": item["status"],
                          "symbols": [{key: value for key, value in node.items() if key != "label"}
                                      for node in item["symbols"]]} for item in mentions]
    data = {"scope": scope, "cases": [{"number": 1, "commit": "lower", "log": {"text": log},
                                        "references": [{"number": 12, "merged": True}]}]}
    entities = {"scope": scope, "upper": "upper", "cases": [{
        "number": 1, "commit": "lower", "probe": {
            "symbol_lookup": {"arguments": {"project": "graphub-probe", "name_pattern": "^(BadEnv|ValidType)$",
                                             "format": "json", "limit": 5000}, "response": native},
            "mentions": baseline_mentions,
        },
        "histories": [{"kind": "entity_file", "file": file, "commits": [{"sha": commit}]}
                      for file, commit in (("pkg/core.py", "a"), ("pkg/other.py", "b"))],
        "arms": {"evidence_entity_file": {"ranking": [11, 12]}},
    }]}
    lexical = {"scope": scope, "primary_messages": True,
               "corpus": {"excluded": [1, 2], "scope": {key: value for key, value in scope.items() if key != "github"}},
               "cases": [{"number": 1, "fusion": [99, 98, 97]}]}
    temporal = {"scope": scope, "upper": "upper", "cases": [{"number": 1, "lower": "lower",
                                                             "arms": {"terminal_trace_file": {"ranking": [98, 96]}}}]}
    registration = {"scope": scope, "sampling": {"development": [1], "held_out": [2], "known_regression": []}}
    links = {sha: [{"number": number, "relation": "pull_landing"}] for sha, number in (("a", 11), ("b", 12))}
    monkeypatch.setattr(diagnostic_bindings, "verify_inputs", lambda *_: None)
    monkeypatch.setattr(diagnostic_bindings, "read_landings", lambda *_: (links, {"fixture": True}))
    monkeypatch.setattr(diagnostic_bindings, "git", lambda *_: SimpleNamespace(stdout="a\nb\n"))
    return data, entities, lexical, temporal, registration


def test_checkpoint_rebuilds_filtered_lanes_without_reading_reference_labels(checkpoint_inputs):
    first = evaluate(*checkpoint_inputs, None)
    case = first["cases"][0]
    assert case["arms"]["evidence_baseline"]["ranking"] == [11, 12]
    assert case["arms"]["evidence_source_and_kind"]["ranking"] == [12]
    assert case["rankings"]["20"]["baseline"] == [99, 98, 11, 96, 12, 97]
    assert case["rankings"]["20"]["source_and_kind"] == [99, 98, 12, 96, 97]
    changed = deepcopy(checkpoint_inputs)
    changed[0]["cases"][0]["references"] = [{"number": 999, "merged": False}]
    second = evaluate(*changed, None)
    assert second["cases"][0]["rankings"] == case["rankings"]
    assert second["cases"][0]["references"] != case["references"]


def test_checkpoint_rejects_native_lookup_and_baseline_rank_drift(checkpoint_inputs):
    changed = deepcopy(checkpoint_inputs)
    changed[1]["cases"][0]["probe"]["symbol_lookup"]["arguments"]["limit"] = 1
    with pytest.raises(ValueError, match="Native name query"):
        evaluate(*changed, None)
    changed = deepcopy(checkpoint_inputs)
    changed[1]["cases"][0]["arms"]["evidence_entity_file"]["ranking"] = [12, 11]
    with pytest.raises(ValueError, match="Re-ranked baseline"):
        evaluate(*changed, None)


def test_checkpoint_rejects_corpus_identity_and_case_set_drift(checkpoint_inputs):
    changed = deepcopy(checkpoint_inputs)
    changed[2]["corpus"]["scope"]["cutoff"] = 9
    with pytest.raises(ValueError, match="corpus has a different source identity"):
        evaluate(*changed, None)
    changed = deepcopy(checkpoint_inputs)
    changed[3]["cases"] = []
    with pytest.raises(ValueError, match="different cases"):
        evaluate(*changed, None)
