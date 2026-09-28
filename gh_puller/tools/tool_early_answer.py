"""Publish useful partial answers without ending the turn or waiting for other tools."""

import copy

from .registry import ToolInputError, ToolProvider, tool

INSTRUCTIONS = (
    "As you work, keep the user or calling agent in the conversation: use early_answer to briefly share useful, "
    "supported findings as they become clear. Do not accumulate findings for a report before replying. "
)

DESCRIPTION = (
    "Deliver a useful, evidence-supported message immediately without ending the turn. Write naturally; "
    "include sources and clarify scope or uncertainty where needed. Avoid guesses, repeated findings and "
    "empty progress updates. Continue to the requested depth, then give a self-contained final answer."
)


class EarlyAnswerTool(ToolProvider):
    def __init__(self, context, storage, on_answer=None):
        self.context, self.storage, self.on_answer = context, storage, on_answer
        self.answers = []

    def restore(self, answers):
        self.answers = copy.deepcopy(answers)
        self.context.recorder.event("tool/early_answer/cleared")
        for answer in self.answers:
            self.context.recorder.event("tool/early_answer/published", **answer)

    def clear_context(self):
        self.restore([])

    def load_events(self, events):
        answers = []
        for event in events:
            if event["type"] == "agent/set/early_answers":
                answers = copy.deepcopy(event["data"]["early_answers"])
            elif event["type"] == "tool/early_answer/cleared":
                answers = []
            elif event["type"] == "tool/early_answer/published":
                answers.append(event["data"])
        self.restore(answers)

    @tool(description=DESCRIPTION, parameters={"type": "object", "properties": {
        "text": {"type": "string", "minLength": 1,
                 "description": "The message for the user or calling agent; Markdown supported."},
    }, "required": ["text"], "additionalProperties": False})
    async def early_answer(self, call_id, text):
        if not text.strip():
            raise ToolInputError("Provide a useful partial answer in text.")
        answer = {"id": f"{self.storage.root.name}:{call_id}", "call_id": call_id,
                  "query": self.context.query, "step": self.context.step, "text": text}
        self.answers.append(answer)
        self.storage.event("answer/early", **answer)
        self.context.recorder.event("tool/early_answer/published", **answer)
        if self.on_answer is not None:
            self.on_answer(text)
        return {"published": True}
