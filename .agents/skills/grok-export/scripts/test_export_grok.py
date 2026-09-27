"""Verify source fidelity, tool linkage, public subagent acquisition, and single-file exports."""

# Keep the portable skill's tests runnable with the standard library alone.
# ruff: noqa: PT009, PT027

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import export_grok as exporter

URL = "https://grok.com/share/test-share"


def source(name, document):
    return exporter.snapshot(name, json.dumps(document, ensure_ascii=False).encode(), {})


def fixture():
    xml = (
        '<xai:tool_usage_card><xai:tool_usage_card_id>call-1</xai:tool_usage_card_id>'
        '<xai:tool_name>OpenPage</xai:tool_name><xai:tool_args><![CDATA['
        '{"args":{"url":"https://example.com","start_line":null,"pattern":null}}'
        ']]></xai:tool_args></xai:tool_usage_card>'
    )
    card = {"toolUsageCardId": "call-1", "openPage": {"args": {"url": "https://example.com"}}}
    result = {"toolCallId": "call-1", "webSearch": {"webpages": [{"snippet": "x" * 1000}]}}
    text = "中文\n\n```text\n<!-- grok-source fake -->\n````\n  trailing  \r\n"
    flat = {"conversation": {"conversationId": "main"}, "responses": [
        {"responseId": "u", "sender": "human", "message": text, "unknown/~field": {"null": None, "empty": []}},
        {"responseId": "a", "sender": "assistant", "parentResponseId": "u", "message": "answer",
         "steps": [{"text": [xml], "toolUsageCards": [card]}]},
    ], "sharedSubagents": [], "future": {"opaque": {"zero": 0, "false": False, "empty": ""}}}
    chunks = {"conversation": {"conversationId": "main"}, "responses": [
        {"responseId": "u", "sender": "human", "message": "", "inputChunks": [{"text": {"text": text}}]},
        {"responseId": "a", "sender": "assistant", "message": "", "outputChunks": [
            {"text": {"text": "Thinking", "channel": "CHANNEL_ASSISTANT_NOTETAKER_HEADER"}},
            {"toolUsageCard": card, "metadata": {"stepId": 1}},
            {"text": {"text": "Visible reasoning", "channel": "CHANNEL_ASSISTANT_ANALYSIS"}},
            {"toolResult": result, "metadata": {"stepId": 0}},
            {"text": {"text": "an", "channel": "CHANNEL_ASSISTANT_RESPONSE"}},
            {"unknownPayload": {"a": [None, {}, False, ""]}},
            {"text": {"text": "swer", "channel": "CHANNEL_ASSISTANT_RESPONSE"}},
        ]},
    ], "sharedSubagents": []}
    return [source("flat", flat), source("chunks", chunks)]


class ExportTests(unittest.TestCase):
    def test_markdown_round_trip_preserves_all_fields_and_chunk_order(self):
        sources = fixture()
        document = sources[0]["document"]
        document["responses"][0]["text\n<!-- grok-source injected -->"] = "opaque"
        report = exporter.coverage(sources, True)
        markdown = exporter.render(URL, sources, report)
        restored = exporter.read_documents(markdown)
        self.assertEqual(restored, {s["name"]: s["document"] for s in sources})
        self.assertIn("Visible reasoning", markdown)
        self.assertIn('"start_line":null', markdown)
        self.assertTrue(report["acquisition_complete"])
        self.assertEqual(report["unique_tool_calls"], 1)
        self.assertEqual(report["calls_without_results"], [])
        self.assertEqual(report["excerpt_fields_at_1000_characters"], 1)

    def test_empty_containers_and_arbitrary_json_round_trip(self):
        values = [None, False, 0, "", [], {}, {"text": "", "responses": []},
                  {"text": ["", "\n", "a\n\n"], "message": "x\r\ny", "odd/ ~": {"x": []}}]
        for document in values:
            with self.subTest(document=document):
                sources = [source("flat", document)]
                markdown = exporter.render(URL, sources, exporter.coverage(sources, True))
                self.assertEqual(exporter.read_documents(markdown)["flat"], document)

    def test_default_writes_exactly_one_file_and_raw_is_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "conversation.md"
            with patch.object(exporter, "acquire", return_value=fixture()) as acquire:
                report = exporter.export(URL, output)
                self.assertTrue(report["acquisition_complete"])
                self.assertEqual(list(Path(directory).iterdir()), [output])
                with self.assertRaises(FileExistsError):
                    exporter.export(URL, output)
                self.assertEqual(acquire.call_count, 1)
                extra = Path(directory) / "raw.json"
                exporter.export(URL, Path(directory) / "second.md", raw_json=extra)
                raw = json.loads(extra.read_bytes())
                self.assertEqual(raw["snapshots"][0]["document"], fixture()[0]["document"])

    def test_failed_sources_are_preserved_and_reported(self):
        sources = fixture()
        sources[1] = exporter.snapshot("chunks", b"<html>access denied</html>",
                                       {"http_status": 403, "acquisition_error": "HTTP 403"})
        report = exporter.coverage(sources, False)
        self.assertFalse(report["acquisition_complete"])
        markdown = exporter.render(URL, sources, report)
        self.assertEqual(exporter.read_documents(markdown)["chunks"], "<html>access denied</html>")

    def test_missing_parents_results_and_new_pagination_are_visible(self):
        sources = fixture()
        sources[0]["document"]["nextPageToken"] = "next"
        sources[0]["document"]["responses"][0]["parentResponseId"] = "unshared"
        sources[1]["document"]["responses"][1]["outputChunks"] = []
        report = exporter.coverage(sources, False)
        self.assertFalse(report["acquisition_complete"])
        self.assertEqual(report["calls_without_results"], ["call-1"])
        self.assertEqual(report["parents_not_in_share"][0]["parent_response_id"], "unshared")

    def test_nested_subagents_are_followed_once_and_missing_children_reported(self):
        documents = fixture()
        for item in documents:
            item["document"]["sharedSubagents"] = [{"conversationId": "child"}]
        fetched = []

        def fetch(name, url):
            fetched.append(url)
            if name in {"flat", "chunks"}:
                return next(s for s in documents if s["name"] == name)
            identifier = "child" if url.endswith("/child") else "nested"
            return source(name, {"conversation": {"conversationId": identifier}, "responses": [],
                                 "sharedSubagents": [{"conversationId": "nested"}, {"conversationId": "child"}]})

        with patch.object(exporter, "fetch", side_effect=fetch):
            sources = exporter.acquire(URL, [])
        self.assertEqual(len(fetched), 4)
        self.assertTrue(exporter.coverage(sources, False)["acquisition_complete"])
        report = exporter.coverage(sources[:-1], True)
        self.assertFalse(report["acquisition_complete"])
        self.assertTrue(any(c["conversation_id"] == "nested" and not c["fetched"] for c in report["subagents"]))

    def test_unrecognized_and_malformed_tools_are_not_silently_counted(self):
        sources = fixture()
        sources[0]["document"]["responses"][1]["steps"].append({
            "toolUsageCards": [{"futureTool": {"arguments": "unknown"}}],
            "text": ["<xai:tool_usage_card>unfinished"],
        })
        report = exporter.coverage(sources, True)
        self.assertFalse(report["acquisition_complete"])
        self.assertTrue(any("Malformed" in item["reason"] for item in report["acquisition_issues"]))


if __name__ == "__main__":
    unittest.main()
