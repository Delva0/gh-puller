"""prompts/get semantics: verbatim templates and argument validation.

prompt_result returns an SDK GetPromptResult.
"""

from __future__ import annotations

import pytest

from gh_puller_mcp.server import PromptError, prompt_result


def test_explore_renders_verbatim_template() -> None:
    result = prompt_result("explore_codebase", {"project": "myproj", "question": "how does X work?"})
    assert result.description == "Graph-first codebase exploration"
    message = result.messages[0]
    assert message.role == "user"
    assert message.content.type == "text"
    text = message.content.text
    assert text.startswith('Explore project "myproj" to answer: how does X work?\n\n')
    assert 'trace_path(direction="both")' in text
    assert text.endswith("or where graph coverage is incomplete.")


def test_review_default_base_branch() -> None:
    result = prompt_result("review_change_impact", {"project": "p", "change": "the change"})
    text = result.messages[0].content.text
    assert text.startswith('Review change impact in project "p" for: the change\n\n')
    assert 'detect_changes with base_branch "main"' in text
    assert text.endswith("do not modify files.")


def test_review_explicit_base_branch() -> None:
    result = prompt_result(
        "review_change_impact", {"project": "p", "change": "c", "base_branch": "HEAD~2"},
    )
    assert 'base_branch "HEAD~2"' in result.messages[0].content.text


def test_extra_arguments_ignored() -> None:
    result = prompt_result("explore_codebase", {"project": "p", "question": "q", "extra": "ignored"})
    assert result.messages[0].content.text.startswith('Explore project "p" to answer: q\n\n')


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {},
        {"project": "p"},  # missing question
        {"question": "q"},  # missing project
        {"project": "", "question": "q"},  # empty project
        {"project": "p", "question": ""},
        "not-a-dict",
    ],
)
def test_missing_required_arguments(arguments) -> None:
    with pytest.raises(PromptError, match="Missing required prompt arguments"):
        prompt_result("explore_codebase", arguments)


@pytest.mark.parametrize("name", [None, "", "not_a_prompt", "explore_codebase_typo", 42])
def test_invalid_prompt_name(name) -> None:
    with pytest.raises(PromptError, match="Invalid prompt name"):
        prompt_result(name, {"project": "p", "question": "q"})


@pytest.mark.parametrize("base_branch", [5, True, [], "", {}])
def test_invalid_base_branch(base_branch) -> None:
    with pytest.raises(PromptError, match="Invalid prompt arguments"):
        prompt_result(
            "review_change_impact",
            {"project": "p", "change": "c", "base_branch": base_branch},
        )
