use agent_tui::{
    document::{self, Job},
    model::{Event, Kind, Model, duration},
};
use serde_json::{Value, json};
use std::sync::Arc;

fn apply(m: &mut Model, kind: &str, data: Value, at: f64) {
    m.apply(Event {
        kind: kind.into(),
        data,
        elapsed_ms: Some(at),
        seq: None,
        ts: None,
    });
}
fn message(role: &str, text: &str) -> Value {
    json!({"type":"message","role":role,"content":[{"type":"output_text","text":text}]})
}

#[test]
fn response_then_context_commit_reuses_stream_cards() {
    let mut m = Model::new();
    apply(&mut m, "model/request", json!({"requestId":"r"}), 0.0);
    apply(
        &mut m,
        "model/delta/text",
        json!({"requestId":"r","index":0,"text":"hel"}),
        100.0,
    );
    let id = m.order.last().unwrap().clone();
    apply(
        &mut m,
        "model/response",
        json!({"requestId":"r","output":[message("assistant", "hello")]}),
        200.0,
    );
    assert_eq!(m.cards[&id].text, "hello");
    assert!(m.context.is_empty());
    apply(
        &mut m,
        "context/append/assistant",
        json!({"items":[message("assistant", "hello")]}),
        200.0,
    );
    assert_eq!(
        m.cards.values().filter(|c| c.kind == Kind::Answer).count(),
        1
    );
    assert!(!m.cards[&id].provisional);
    assert_eq!(m.context.len(), 1);
}

#[test]
fn tool_activity_does_not_replace_committed_results_and_context_set_removes_them() {
    let mut m = Model::new();
    let call =
        json!({"type":"function_call","call_id":"c","name":"read","arguments":"{\"path\":\"a\"}"});
    apply(
        &mut m,
        "context/append/assistant",
        json!({"items":[call.clone()]}),
        0.0,
    );
    apply(
        &mut m,
        "tool/start",
        json!({"callId":"c","name":"read"}),
        10.0,
    );
    apply(
        &mut m,
        "tool/end",
        json!({"callId":"c","result":"activity only"}),
        50.0,
    );
    assert!(m.cards["tool:c"].result.is_none());
    apply(
        &mut m,
        "context/append/tool",
        json!({"items":[{"type":"function_call_output","call_id":"c","output":"actual result"}]}),
        51.0,
    );
    assert_eq!(m.cards["tool:c"].result.as_deref(), Some("actual result"));
    assert_eq!(m.cards["tool:c"].duration_ms, Some(40.0));
    assert_eq!(m.cards.values().filter(|c| c.kind == Kind::Tool).count(), 1);
    apply(&mut m, "context/set", json!({"items":[call]}), 60.0);
    assert!(m.cards["tool:c"].result.is_none());
    assert_eq!(m.order, vec!["tool:c"]);
}

#[test]
fn replace_preserves_equal_content_identity_including_duplicates() {
    let mut m = Model::new();
    let a = message("user", "same");
    let b = message("assistant", "answer");
    apply(
        &mut m,
        "context/append",
        json!({"items":[a.clone(), a.clone(), b.clone()]}),
        0.0,
    );
    let old = m.order.clone();
    apply(
        &mut m,
        "context/set",
        json!({"items":[a.clone(), a, b]}),
        1.0,
    );
    assert_eq!(m.order, old);
    assert_eq!(m.order.len(), 3);
    apply(&mut m, "context/set", json!({"items":[]}), 2.0);
    assert!(m.cards.is_empty());
    assert!(m.context.is_empty());
}

#[test]
fn no_backend_metadata_invents_boundaries_and_error_does_not_commit_output() {
    let mut m = Model::new();
    apply(
        &mut m,
        "agent/set/resume",
        json!({"resume":{"query":27}}),
        0.0,
    );
    apply(
        &mut m,
        "context/set",
        json!({"items":[{"type":"message","role":"user","metadata":{"query":12,"step":3},"content":[]}]}),
        0.0,
    );
    assert_eq!(m.stats.turn, 0);
    assert_eq!(m.stats.step, 0);
    assert_eq!(
        m.agent,
        json!({"agent":null,"config":{"resume":{"query":27}}})
    );
    apply(&mut m, "model/request", json!({"requestId":"r"}), 0.0);
    apply(
        &mut m,
        "model/delta/reasoning",
        json!({"requestId":"r","index":0,"text":"partial"}),
        5.0,
    );
    apply(
        &mut m,
        "model/error",
        json!({"requestId":"r","error":{"message":"cancelled"}}),
        10.0,
    );
    apply(
        &mut m,
        "session/end",
        json!({"outcome":"failed","reasonCode":"cancelled"}),
        20.0,
    );
    assert_eq!(m.context.len(), 1);
    assert!(m.summary.ended);
    assert_eq!(m.summary.last_ms, 20.0);
}

#[test]
fn terminal_status_reports_one_outcome_or_specific_failure_reason() {
    for (data, expected) in [
        (
            json!({"outcome":"completed","reasonCode":"completed"}),
            "Completed",
        ),
        (json!({"outcome":"failed","reasonCode":"error"}), "Failed"),
        (
            json!({"outcome":"failed","reasonCode":"cancelled"}),
            "Cancelled",
        ),
        (
            json!({"outcome":"failed","reasonCode":"timeout"}),
            "Timed out",
        ),
        (
            json!({"outcome":"failed","reasonCode":"budget_exhausted"}),
            "Budget exhausted",
        ),
        (json!({"outcome":"completed"}), "Completed"),
        (json!({"outcome":"failed"}), "Failed"),
        (json!({}), "Ended"),
    ] {
        let mut m = Model::new();
        apply(&mut m, "turn/start", json!({}), 0.0);
        apply(&mut m, "turn/end", json!({}), 2000.0);
        apply(&mut m, "session/end", data, 2330.0);
        assert_eq!(m.summary.status, format!("{expected} · 2.00s"));
        apply(&mut m, "turn/start", json!({}), 3000.0);
        assert_eq!(m.summary.last_ms, 2330.0);
        assert_eq!(m.summary.status, format!("{expected} · 2.00s"));
    }
}

#[test]
fn tables_unicode_links_and_code_survive_layout_and_resize() {
    let mut m = Model::new();
    apply(
        &mut m,
        "context/append/assistant",
        json!({"items":[message("assistant", "# 你好\n\nA paragraph that wraps at the narrow width.\n\n| 名称 | 值 |\n|---|---|\n| 测试 | `42` |\n\n[文档](https://example.com)\n\n```rust\nlet long_line = 123456789;\n```")]}),
        0.0,
    );
    m.change();
    let card = Arc::new(m.cards[&m.order[0]].clone());
    let wide = document::prepare(Job {
        card: card.clone(),
        width: 80,
        expanded: true,
    });
    let narrow = document::prepare(Job {
        card,
        width: 10,
        expanded: true,
    });
    assert_eq!(wide.plain, narrow.plain);
    assert!(wide.plain.contains("│ 测试 │ 42"));
    assert!(!wide.plain.contains("https://example.com"));
    assert_eq!(wide.links[0].url, "https://example.com");
    assert_eq!(&wide.plain[wide.links[0].start..wide.links[0].end], "文档");
    assert!(narrow.rows.iter().any(|r| r.line.width() > 10));
    assert!(narrow.rows.len() > wide.rows.len());
}

#[test]
fn prior_results_survive_context_summaries_without_changing_the_fold_or_counts() {
    let mut m = Model::new();
    let call = json!({"type":"function_call", "call_id":"c", "name":"read", "arguments":"{}"});
    let payload = "full content ".repeat(2000);
    let original =
        json!({"type":"function_call_output", "call_id":"c", "output":{"content":payload}});
    let summary = json!({"type":"function_call_output", "call_id":"c", "output":"Short summary."});
    apply(
        &mut m,
        "context/append",
        json!({"items":[call.clone(), original]}),
        0.0,
    );
    apply(
        &mut m,
        "tool/end",
        json!({"callId":"c", "result":"activity only"}),
        1.0,
    );
    apply(
        &mut m,
        "context/set",
        json!({"items":[call.clone(), summary.clone()]}),
        2.0,
    );
    m.change();
    assert_eq!(m.context, vec![call.clone(), summary]);
    let card = &m.cards["tool:c"];
    assert_eq!(card.result.as_deref(), Some("Short summary."));
    assert!(card.recorded_result.as_deref().unwrap().contains(&payload));
    assert!(card.result_text().contains(&payload));
    assert!(card.tokens < 20);
    let doc = document::prepare(Job {
        card: Arc::new(card.clone()),
        width: 40,
        expanded: true,
    });
    assert!(doc.plain.contains(&payload));
    assert!(doc.plain.contains("Recorded result"));
    assert!(doc.plain.contains("Current context\nShort summary."));
    assert!(!doc.plain.contains("activity only"));
    assert!(doc.rows.iter().all(|r| r.line.width() <= 40));
    apply(&mut m, "context/set", json!({"items":[call]}), 3.0);
    assert!(!m.cards["tool:c"].body(true).contains(&payload));
}

#[test]
fn duration_uses_seconds_minutes_and_hours() {
    assert_eq!(duration(1680.0), "1.68s");
    assert_eq!(duration(60000.0), "1m 00s");
    assert_eq!(duration(3599999.0), "1h 00m 00s");
    assert_eq!(duration(22240850.0), "6h 10m 41s");
}

#[test]
fn session_duration_sums_turns_across_idle_time_and_context_replacement() {
    let mut m = Model::new();
    apply(&mut m, "session/start", json!({}), 0.0);
    apply(&mut m, "turn/start", json!({}), 69.58106404636055);
    apply(&mut m, "turn/end", json!({}), 109898.22950900998);
    apply(&mut m, "context/set", json!({"items":[]}), 200000.0);
    assert!(m.cards.is_empty());
    apply(&mut m, "turn/start", json!({}), 428642.53456296865);
    apply(&mut m, "turn/end", json!({}), 463174.2166070035);
    apply(
        &mut m,
        "session/end",
        json!({"outcome":"completed", "durationMs":22240848}),
        22240850.407439053,
    );
    assert_eq!(m.summary.status, "Completed · 2m 24s");
    assert_eq!(m.stats.turn, 2);
    assert!(m.context.is_empty());
}

#[test]
fn session_duration_remains_unknown_without_complete_monotonic_turn_timings() {
    for boundaries in [
        vec![],
        vec![("turn/end", Some(2000.0))],
        vec![("turn/start", Some(0.0))],
        vec![("turn/start", None), ("turn/end", Some(2000.0))],
        vec![("turn/start", Some(0.0)), ("turn/end", None)],
        vec![("turn/start", Some(2000.0)), ("turn/end", Some(0.0))],
        vec![
            ("turn/start", Some(0.0)),
            ("turn/end", Some(2000.0)),
            ("turn/start", Some(3000.0)),
        ],
        vec![
            ("turn/start", Some(0.0)),
            ("turn/start", Some(1000.0)),
            ("turn/end", Some(2000.0)),
        ],
    ] {
        let mut m = Model::new();
        for (i, (kind, elapsed_ms)) in boundaries.iter().enumerate() {
            m.apply(Event {
                kind: (*kind).into(),
                data: json!({}),
                elapsed_ms: *elapsed_ms,
                ts: Some(100.0 + i as f64),
                seq: Some(i as u64),
            });
        }
        apply(
            &mut m,
            "session/end",
            json!({"outcome":"completed", "durationMs":22240850}),
            22240850.0,
        );
        assert_eq!(m.summary.status, "Completed · —", "{boundaries:?}");
    }
}

#[test]
fn retained_results_come_from_context_facts_including_initial_snapshots() {
    let mut m = Model::new();
    let output = |text| json!({"type":"function_call_output", "call_id":"c", "output":text});
    apply(
        &mut m,
        "model/response",
        json!({"requestId":"r", "output":[output("provisional output")]}),
        0.0,
    );
    apply(
        &mut m,
        "context/set",
        json!({"items":[output("recorded output")]}),
        1.0,
    );
    apply(
        &mut m,
        "context/set",
        json!({"items":[output("summary")]}),
        2.0,
    );
    let card = &m.cards["tool:c"];
    assert_eq!(card.result.as_deref(), Some("summary"));
    assert_eq!(card.recorded_result.as_deref(), Some("recorded output"));
    assert!(!card.body(true).contains("provisional output"));
}

#[test]
fn all_message_roles_preserve_soft_newlines_and_literal_backslashes() {
    let text = "First line\nSecond [line](https://example.com)\nLiteral \\n stays literal.\n\n```text\ncode line 1\ncode line 2\n```";
    let mut documents = vec![];
    for role in ["system", "user", "assistant"] {
        let mut m = Model::new();
        apply(
            &mut m,
            "context/append",
            json!({"items":[message(role, text)]}),
            0.0,
        );
        let doc = document::prepare(Job {
            card: Arc::new(m.cards[&m.order[0]].clone()),
            width: 80,
            expanded: true,
        });
        assert!(
            doc.plain
                .starts_with("First line\nSecond line\nLiteral \\n stays literal.\n")
        );
        assert!(doc.plain.contains("code line 1\ncode line 2\n"));
        assert_eq!(&doc.plain[doc.links[0].start..doc.links[0].end], "line");
        documents.push(doc.plain);
    }
    assert_eq!(documents[0], documents[1]);
    assert_eq!(documents[1], documents[2]);
}

#[test]
fn tool_definitions_render_description_newlines_without_changing_context_or_counts() {
    let mut m = Model::new();
    let part = json!({"type":"tool_defs", "tools":[{
        "name":"read_file", "description":"Read a file.\nKeep its line endings.",
        "inputSchema":{"type":"object", "properties":{"path":{"type":"string"}}},
        "extra":"Preserve unknown fields"
    }, {"name":"<opaque>"}], "source":"Declared tools"});
    let item = json!({"type":"message", "role":"system", "content":[part.clone()]});
    apply(
        &mut m,
        "context/append/system",
        json!({"items":[item.clone()]}),
        0.0,
    );
    m.change();
    let card = &m.cards[&m.order[0]];
    assert_eq!(
        card.tokens,
        agent_tui::stats::count(&agent_tui::model::pretty(&part))
    );
    let doc = document::prepare(Job {
        card: Arc::new(card.clone()),
        width: 80,
        expanded: true,
    });
    assert!(
        doc.plain
            .contains("read_file\nRead a file.\nKeep its line endings.\n")
    );
    assert!(!doc.plain.contains("file.\\nKeep"));
    assert!(doc.plain.contains("Input schema\n"));
    assert!(doc.plain.contains("\"type\": \"string\""));
    assert!(doc.plain.contains("Preserve unknown fields"));
    assert!(doc.plain.contains("Declared tools"));
    assert!(doc.plain.contains("<opaque>"));
    assert_eq!(m.context, vec![item]);
}

#[test]
fn output_phase_times_survive_response_context_commit_and_replacement() {
    let mut m = Model::new();
    apply(&mut m, "model/request", json!({"requestId":"r"}), 0.0);
    apply(
        &mut m,
        "model/delta/reasoning",
        json!({"requestId":"r","text":""}),
        20.0,
    );
    apply(
        &mut m,
        "model/delta/reasoning",
        json!({"requestId":"r","text":"Think"}),
        100.0,
    );
    apply(
        &mut m,
        "model/delta/reasoning",
        json!({"requestId":"r","text":"ing"}),
        180.0,
    );
    apply(
        &mut m,
        "model/delta/text",
        json!({"requestId":"r","text":"An"}),
        300.0,
    );
    apply(
        &mut m,
        "model/delta/text",
        json!({"requestId":"r","text":"swer"}),
        400.0,
    );
    let output = json!([
        {"type":"reasoning","content":[{"type":"reasoning_text","text":"Thinking"}]},
        message("assistant", "Answer")
    ]);
    apply(
        &mut m,
        "model/response",
        json!({"requestId":"r","output":output}),
        600.0,
    );
    apply(
        &mut m,
        "context/append/assistant",
        json!({"items":output}),
        601.0,
    );
    let thought = m
        .cards
        .values()
        .find(|c| c.kind == Kind::Think)
        .unwrap()
        .id
        .clone();
    let answer = m
        .cards
        .values()
        .find(|c| c.kind == Kind::Answer)
        .unwrap()
        .id
        .clone();
    assert_eq!(m.cards[&thought].duration_ms, Some(200.0));
    assert_eq!(m.cards[&answer].duration_ms, Some(300.0));
    assert!(!m.cards[&thought].provisional);
    assert!(!m.cards[&answer].provisional);
    apply(&mut m, "context/set", json!({"items":output}), 700.0);
    apply(
        &mut m,
        "session/end",
        json!({"outcome":"completed"}),
        22_000_000.0,
    );
    assert_eq!(m.cards[&thought].duration_ms, Some(200.0));
    assert_eq!(m.cards[&answer].duration_ms, Some(300.0));
    assert_eq!(m.context, output.as_array().unwrap().clone());
}

#[test]
fn interleaved_parts_accumulate_per_request_and_tool_execution_stays_separate() {
    let mut m = Model::new();
    apply(&mut m, "model/request", json!({"requestId":"a"}), 0.0);
    apply(
        &mut m,
        "model/delta/reasoning",
        json!({"requestId":"a","text":"Reason"}),
        100.0,
    );
    apply(&mut m, "model/request", json!({"requestId":"b"}), 110.0);
    apply(
        &mut m,
        "model/delta/text",
        json!({"requestId":"b","text":"Other"}),
        120.0,
    );
    apply(
        &mut m,
        "model/delta/text",
        json!({"requestId":"a","text":"Answer"}),
        200.0,
    );
    apply(
        &mut m,
        "model/response",
        json!({"requestId":"b","output":[message("assistant", "Other")]}),
        300.0,
    );
    apply(
        &mut m,
        "model/delta/tool-call",
        json!({"requestId":"a","callId":"c","name":"read","argumentsDelta":"{}"}),
        350.0,
    );
    apply(
        &mut m,
        "model/delta/reasoning",
        json!({"requestId":"a","text":" more"}),
        400.0,
    );
    apply(
        &mut m,
        "model/response",
        json!({"requestId":"a","output":[
            {"type":"reasoning","content":[{"type":"reasoning_text","text":"Reason more"}]},
            message("assistant", "Answer"),
            {"type":"function_call","call_id":"c","name":"read","arguments":"{}"}
        ]}),
        500.0,
    );
    apply(
        &mut m,
        "tool/start",
        json!({"callId":"c","name":"read"}),
        800.0,
    );
    apply(&mut m, "tool/end", json!({"callId":"c"}), 850.0);
    let part = |kind, request| {
        m.cards
            .values()
            .find(|c| c.kind == kind && c.request == request)
            .unwrap()
    };
    assert_eq!(part(Kind::Think, "a").duration_ms, Some(200.0));
    assert_eq!(part(Kind::Answer, "a").duration_ms, Some(150.0));
    assert_eq!(part(Kind::Answer, "b").duration_ms, Some(180.0));
    assert_eq!(m.cards["tool:c"].duration_ms, Some(50.0));
}

#[test]
fn output_timing_stops_on_error_and_missing_timing_stays_unknown() {
    let mut m = Model::new();
    apply(&mut m, "model/request", json!({"requestId":"r"}), 0.0);
    apply(
        &mut m,
        "model/delta/text",
        json!({"requestId":"r","text":"Partial"}),
        100.0,
    );
    apply(
        &mut m,
        "model/error",
        json!({"requestId":"r","error":{"message":"Cancelled"}}),
        300.0,
    );
    apply(
        &mut m,
        "session/end",
        json!({"outcome":"failed"}),
        22_000_000.0,
    );
    assert_eq!(
        m.cards
            .values()
            .find(|c| c.kind == Kind::Answer)
            .unwrap()
            .duration_ms,
        Some(200.0)
    );

    for missing in ["deltas", "clock", "end"] {
        let mut m = Model::new();
        apply(&mut m, "model/request", json!({"requestId":"r"}), 0.0);
        if missing != "deltas" {
            apply(
                &mut m,
                "model/delta/text",
                json!({"requestId":"r","text":"A"}),
                100.0,
            );
            if missing == "clock" {
                m.apply(Event {
                    kind: "model/delta/text".into(),
                    data: json!({"requestId":"r","text":"B"}),
                    elapsed_ms: None,
                    ts: Some(100.0),
                    seq: None,
                });
                apply(
                    &mut m,
                    "model/delta/text",
                    json!({"requestId":"r","text":"C"}),
                    200.0,
                );
            }
        }
        if missing != "end" {
            apply(
                &mut m,
                "model/response",
                json!({"requestId":"r","output":[message("assistant", "Answer")]}),
                300.0,
            );
        }
        apply(
            &mut m,
            "session/end",
            json!({"outcome":"completed"}),
            22_000_000.0,
        );
        assert_eq!(
            m.cards
                .values()
                .find(|c| c.kind == Kind::Answer)
                .unwrap()
                .duration_ms,
            None,
            "{missing}"
        );
    }
}
