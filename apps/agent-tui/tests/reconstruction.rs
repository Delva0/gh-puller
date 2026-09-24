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
        apply(&mut m, "session/end", data, 2330.0);
        assert_eq!(m.summary.status, format!("{expected} · session 2.33s"));
        apply(&mut m, "turn/start", json!({}), 3000.0);
        assert_eq!(m.summary.last_ms, 2330.0);
        assert_eq!(m.summary.status, format!("{expected} · session 2.33s"));
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
fn session_duration_is_labeled_and_keeps_idle_time_in_the_recorded_lifetime() {
    assert_eq!(duration(1680.0), "1.68s");
    assert_eq!(duration(60000.0), "1m 00s");
    assert_eq!(duration(3599999.0), "1h 00m 00s");
    assert_eq!(duration(22240850.0), "6h 10m 41s");
    let mut m = Model::new();
    apply(&mut m, "turn/start", json!({}), 0.0);
    apply(&mut m, "turn/end", json!({}), 2000.0);
    apply(
        &mut m,
        "session/end",
        json!({"outcome":"completed", "durationMs":22240850}),
        22242000.0,
    );
    assert_eq!(m.summary.status, "Completed · session 6h 10m 41s");
}
