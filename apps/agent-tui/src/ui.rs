//! Virtualized cards, persistent reading anchors, and terminal-local interaction.
use crate::{
    document::{self, Document, Job},
    hyperlinks,
    model::{Card, Change, Kind, Summary, duration},
    stats::number,
};
use base64::Engine;
use crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers, MouseButton, MouseEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph, Wrap},
};
use std::{
    collections::{HashMap, HashSet},
    io::{self, Write},
    sync::{
        Arc,
        mpsc::{Receiver, Sender},
    },
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

#[derive(Default)]
struct Heights {
    values: Vec<usize>,
    tree: Vec<usize>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Event as AgentEvent, Model};
    use crossterm::event::{KeyEvent, MouseEvent};
    use ratatui::{Terminal, backend::TestBackend};
    use serde_json::json;

    fn apply(model: &mut Model, kind: &str, data: serde_json::Value) {
        model.apply(AgentEvent {
            kind: kind.into(),
            data,
            elapsed_ms: Some(0.0),
            ts: None,
            seq: None,
        });
    }
    fn fixture() -> (Model, App) {
        let mut model = Model::new();
        apply(
            &mut model,
            "context/append",
            json!({"items":[
                {"type":"message","role":"user","content":[{"type":"input_text","text":"你好 hello world"}]},
                {"type":"reasoning","content":[{"type":"reasoning_text","text":"thinking"}]},
                {"type":"message","role":"assistant","content":[{"type":"output_text","text":"answer"}]}
            ]}),
        );
        let mut app = App::new("events.jsonl".into());
        app.apply(model.change());
        app.follow = false;
        app.scroll = 0;
        app.width = 75;
        layout(&mut app);
        (model, app)
    }
    fn layout(app: &mut App) {
        for id in app.order.clone() {
            let doc = document::prepare(Job {
                card: app.cards[&id].clone(),
                width: app.width,
                expanded: app.open(&id),
            });
            app.docs.insert(id.clone(), doc);
            let i = app.positions[&id];
            app.heights.set(i, app.height(&id));
        }
    }
    fn key(app: &mut App, code: KeyCode) {
        app.handle(Event::Key(KeyEvent::new(code, KeyModifiers::NONE)));
    }

    #[test]
    fn defaults_fold_state_and_focus_survive_context_replacement() {
        let (mut model, mut app) = fixture();
        let ids = app.order.clone();
        assert!(app.open(&ids[0]));
        assert!(!app.open(&ids[1]));
        assert!(app.open(&ids[2]));
        app.focus = Some(ids[1].clone());
        key(&mut app, KeyCode::Enter);
        assert!(app.open(&ids[1]));
        let items = model.context.clone();
        apply(&mut model, "context/set", json!({"items":items}));
        app.apply(model.change());
        assert!(app.open(&ids[1]));
        assert_eq!(app.focus.as_ref(), Some(&ids[1]));
        assert_eq!(app.copy_text(true).as_deref(), Some("answer"));
        key(&mut app, KeyCode::Char('E'));
        assert!(!app.open(&ids[0]));
        key(&mut app, KeyCode::Char('e'));
        assert!(app.open(&ids[1]));
    }

    #[test]
    fn collapsed_cards_use_one_unframed_row_and_tools_expand_in_place() {
        let (mut model, mut app) = fixture();
        apply(
            &mut model,
            "context/append",
            json!({"items":[
                {"type":"message","role":"system","content":[{"type":"input_text","text":"System instructions"}]},
                {"type":"function_call","call_id":"read","name":"read_file","arguments":"{\"path\":\"file.rs\"}"},
                {"type":"function_call_output","call_id":"read","output":"File contents"}
            ]}),
        );
        app.apply(model.change());
        let tool = "tool:read";
        assert!(!app.open(tool));
        app.command("collapse");
        for id in app.order.clone() {
            assert_eq!(app.height(&id), 1);
            assert!(app.cards[&id].body(false).is_empty());
            app.request(&id);
        }
        assert!(app.requested.is_empty());
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|f| app.render(f)).unwrap();
        assert_eq!(app.heights.total(), app.order.len());
        for (i, id) in app.order.iter().enumerate() {
            assert_eq!(app.hits[i].id, *id);
            assert_eq!(app.hits[i].y, i as u16 + 2);
            assert!(app.hits[i].title);
            assert_eq!(terminal.backend().buffer()[(0, i as u16 + 2)].symbol(), " ");
        }
        app.focus = Some(tool.into());
        key(&mut app, KeyCode::Enter);
        layout(&mut app);
        terminal.draw(|f| app.render(f)).unwrap();
        assert!(app.docs[tool].plain.contains("file.rs"));
        assert!(app.docs[tool].plain.contains("File contents"));
        assert!(app.hits.iter().any(|h| h.id == tool && !h.title));
        key(&mut app, KeyCode::Enter);
        assert_eq!(app.height(tool), 1);
        assert_eq!(app.focus.as_deref(), Some(tool));
    }

    #[test]
    fn request_headings_stay_hidden_across_updates_and_context_replacement() {
        let (mut model, mut app) = fixture();
        apply(&mut model, "turn/start", json!({}));
        apply(&mut model, "step/start", json!({}));
        apply(&mut model, "model/request", json!({"requestId":"r1"}));
        apply(
            &mut model,
            "context/append/assistant",
            json!({"items":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"First step"}]}]}),
        );
        app.apply(model.change());
        app.focus = Some("step:1:1".into());
        apply(&mut model, "step/start", json!({}));
        apply(&mut model, "model/request", json!({"requestId":"r2"}));
        apply(
            &mut model,
            "context/append/assistant",
            json!({"items":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Second step"}]}]}),
        );
        app.apply(model.change());
        assert!(model.cards.values().any(|c| c.kind == Kind::Request));
        assert!(!app.cards.values().any(|c| c.kind == Kind::Request));
        key(&mut app, KeyCode::Char('}'));
        assert_eq!(app.focus.as_deref(), Some("step:1:2"));
        let items = model.context.clone();
        apply(&mut model, "context/set", json!({"items":items}));
        app.apply(model.change());
        assert!(!app.cards.values().any(|c| c.kind == Kind::Request));
        assert!(app.order.iter().all(|id| app.cards.contains_key(id)));
        key(&mut app, KeyCode::Char('{'));
        assert_eq!(app.focus.as_deref(), Some("step:1:1"));
        assert!(
            app.title(&app.cards["turn:1"])
                .to_string()
                .contains("turn 1")
        );
        assert!(
            app.title(&app.cards["step:1:1"])
                .to_string()
                .contains("step 1")
        );
    }

    #[test]
    fn command_menu_and_async_search_are_usable() {
        let (_, mut app) = fixture();
        key(&mut app, KeyCode::Char(':'));
        for c in "coll".chars() {
            key(&mut app, KeyCode::Char(c));
        }
        key(&mut app, KeyCode::Enter);
        assert!(app.order.iter().all(|id| !app.open(id)));
        key(&mut app, KeyCode::Char('m'));
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|f| app.render(f)).unwrap();
        app.handle(Event::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 8,
            row: 7,
            modifiers: KeyModifiers::NONE,
        }));
        assert!(app.panel.is_none());
        assert!(app.order.iter().all(|id| app.open(id)));
        key(&mut app, KeyCode::Char('/'));
        for c in "answer".chars() {
            key(&mut app, KeyCode::Char(c));
        }
        key(&mut app, KeyCode::Enter);
        for _ in 0..100 {
            app.poll_layout();
            if app.search_result.is_none() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        assert_eq!(app.matches.len(), 1);
        assert_eq!(app.cards[app.focus.as_ref().unwrap()].kind, Kind::Answer);
        assert!(app.open(app.focus.as_ref().unwrap()));
    }

    #[test]
    fn unicode_mouse_selection_copy_and_resize_preserve_text() {
        let (_, mut app) = fixture();
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|f| app.render(f)).unwrap();
        let y = app.hits.iter().find(|h| !h.title).unwrap().y;
        app.handle(Event::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 2,
            row: y,
            modifiers: KeyModifiers::NONE,
        }));
        app.handle(Event::Mouse(MouseEvent {
            kind: MouseEventKind::Drag(MouseButton::Left),
            column: 6,
            row: y,
            modifiers: KeyModifiers::NONE,
        }));
        assert_eq!(app.selected_text().as_deref(), Some("你好"));
        assert_eq!(app.copy_text(false).as_deref(), Some("你好"));
        terminal.draw(|f| app.render(f)).unwrap();
        let buffer = terminal.backend().buffer();
        assert_eq!(buffer[(2, y)].bg, Color::Rgb(75, 91, 126));
        assert_eq!(buffer[(10, y)].bg, app.focus_background());
        assert_eq!(buffer[(78, y)].symbol(), " ");
        app.width = 10;
        layout(&mut app);
        assert_eq!(app.selected_text().as_deref(), Some("你好"));
        assert!(!app.follow);
    }

    #[test]
    fn scroll_pause_new_content_and_end_behavior() {
        let (mut model, mut app) = fixture();
        app.viewport = 3;
        app.follow = true;
        app.restore(None);
        key(&mut app, KeyCode::Up);
        let scroll = app.scroll;
        let focus = app.focus.clone();
        apply(
            &mut model,
            "context/append/user",
            json!({"items":[{"type":"message","role":"user","content":[{"type":"input_text","text":"new text"}]}]}),
        );
        app.apply(model.change());
        assert_eq!(app.scroll, scroll);
        assert_eq!(app.focus, focus);
        assert_eq!(app.unseen, 1);
        key(&mut app, KeyCode::End);
        assert!(app.follow);
        assert_eq!(app.unseen, 0);
        apply(&mut model, "session/end", json!({"outcome":"completed"}));
        app.apply(model.change());
        assert!(!app.follow);
        key(&mut app, KeyCode::End);
        assert!(!app.follow);
    }

    #[test]
    fn presets_track_custom_toggles_new_cards_and_context_replacements() {
        let (mut model, mut app) = fixture();
        let ids = app.order.clone();
        assert_eq!(app.view(), Some(Preset::Conversation));
        key(&mut app, KeyCode::Char('1'));
        assert_eq!(app.view(), Some(Preset::Collapsed));
        apply(&mut model, "turn/start", json!({}));
        apply(
            &mut model,
            "context/append/system",
            json!({"items":[
                {"type":"message","role":"system","content":[{"type":"input_text","text":"Instructions"}]}
            ]}),
        );
        app.apply(model.change());
        let system = app.order.last().unwrap().clone();
        assert!(!app.open(&system));
        assert_eq!(app.view(), Some(Preset::Collapsed));
        app.focus = Some(ids[0].clone());
        key(&mut app, KeyCode::Enter);
        assert_eq!(app.view(), None);
        assert_eq!(app.view_label(), "Custom");
        app.toggle(&ids[2]);
        assert_eq!(app.view(), Some(Preset::Conversation));
        app.toggle(&system);
        assert_eq!(app.view(), None);
        app.toggle(&system);
        assert_eq!(app.view(), Some(Preset::Conversation));

        key(&mut app, KeyCode::Char('3'));
        apply(
            &mut model,
            "context/append/tool",
            json!({"items":[
                {"type":"function_call_output","call_id":"new","output":"Result"}
            ]}),
        );
        app.apply(model.change());
        assert!(app.open("tool:new"));
        assert_eq!(app.view(), Some(Preset::Expanded));
        key(&mut app, KeyCode::Char('2'));
        assert!(!app.open(&system));
        assert!(!app.open(&ids[1]));
        assert!(app.open(&ids[0]) && app.open(&ids[2]));
        app.toggle("tool:new");
        let items = model.context.clone();
        apply(&mut model, "context/set", json!({"items":items}));
        app.apply(model.change());
        assert_eq!(app.view(), None);
        assert!(app.open("tool:new"));
        let items: Vec<_> = model
            .context
            .iter()
            .filter(|i| i["call_id"] != "new")
            .cloned()
            .collect();
        apply(&mut model, "context/set", json!({"items":items}));
        app.apply(model.change());
        assert_eq!(app.view(), Some(Preset::Conversation));
        assert_eq!(app.notice, "View: Conversation");
    }

    #[test]
    fn custom_views_preserve_overrides_and_search_updates_the_view() {
        let (mut model, mut app) = fixture();
        let ids = app.order.clone();
        app.toggle(&ids[0]);
        assert_eq!(app.view(), None);
        apply(
            &mut model,
            "context/append",
            json!({"items":[
                {"type":"message","role":"user","content":[{"type":"input_text","text":"Another question"}]},
                {"type":"reasoning","content":[{"type":"reasoning_text","text":"Another thought"}]}
            ]}),
        );
        app.apply(model.change());
        assert!(!app.open(&ids[0]));
        assert!(app.open(&app.order[3]));
        assert!(!app.open(&app.order[4]));
        assert_eq!(app.view(), None);
        app.matches = vec![ids[0].clone()];
        app.next_match(true);
        assert!(app.open(&ids[0]));
        assert_eq!(app.view(), Some(Preset::Conversation));
        assert_eq!(app.focus.as_ref(), Some(&ids[0]));
    }

    #[test]
    fn resizing_preserves_character_reading_anchor() {
        let (mut model, mut app) = fixture();
        apply(
            &mut model,
            "context/set",
            json!({"items":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"a long readable line ".repeat(100)}]}]}),
        );
        app.apply(model.change());
        app.viewport = 3;
        app.width = 40;
        layout(&mut app);
        app.scroll = 8;
        let anchor = app.anchor();
        let byte = anchor.as_ref().unwrap().2.unwrap();
        app.width = 20;
        layout(&mut app);
        app.restore(anchor);
        let now = app.anchor().unwrap().2.unwrap();
        assert!(now <= byte && byte - now < 20);
    }

    #[test]
    fn scrollbar_navigation_and_footer_are_independent_of_context_size() {
        let (_, mut app) = fixture();
        app.viewport = 3;
        app.handle(Event::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 79,
            row: 5,
            modifiers: KeyModifiers::NONE,
        }));
        assert_eq!(app.scroll, app.heights.total() - app.viewport);
        key(&mut app, KeyCode::Home);
        assert_eq!(app.scroll, 0);
        let first = app.order[0].clone();
        app.focus = Some(first.clone());
        key(&mut app, KeyCode::Tab);
        assert_ne!(app.focus, Some(first));
        key(&mut app, KeyCode::Right);
        assert_eq!(app.horizontal, 8);
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|f| app.render(f)).unwrap();
        let footer: String = (0..80)
            .map(|x| terminal.backend().buffer()[(x, 23)].symbol())
            .collect();
        assert_eq!(
            footer.trim(),
            format!("{}  {}", app.summary.status, app.summary.footer)
        );
        assert_eq!(app.viewport, 21);
    }

    #[test]
    fn completion_and_statistics_share_one_footer_row_with_room_for_notices() {
        let (mut model, mut app) = fixture();
        apply(&mut model, "turn/start", json!({}));
        model.apply(AgentEvent {
            kind: "turn/end".into(),
            data: json!({}),
            elapsed_ms: Some(2330.0),
            ts: None,
            seq: None,
        });
        model.apply(AgentEvent {
            kind: "session/end".into(),
            data: json!({"outcome":"completed","reasonCode":"completed","durationMs":60000}),
            elapsed_ms: Some(60000.0),
            ts: None,
            seq: None,
        });
        app.apply(model.change());
        key(&mut app, KeyCode::End);
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|f| app.render(f)).unwrap();
        let row = |terminal: &Terminal<TestBackend>, y| -> String {
            (0..80)
                .map(|x| terminal.backend().buffer()[(x, y)].symbol())
                .collect()
        };
        assert_eq!(
            row(&terminal, 23).trim(),
            format!("Completed · 2.33s  {}", app.summary.footer)
        );
        assert!(!row(&terminal, 22).contains("Completed"));
        app.notice = "A long notification ".repeat(10);
        terminal.draw(|f| app.render(f)).unwrap();
        let footer = row(&terminal, 23);
        assert!(footer.trim().starts_with("Completed · 2.33s"));
        assert!(footer.trim().ends_with(&app.summary.footer));
    }

    #[test]
    fn height_index_supports_large_incremental_history() {
        let mut heights = Heights::default();
        for _ in 0..100_000 {
            heights.push(3);
        }
        heights.set(1, 8);
        assert_eq!(heights.total(), 300_005);
        assert_eq!(heights.locate(10), 1);
        assert_eq!(heights.locate(11), 2);
    }

    #[test]
    fn expanded_cards_end_on_the_last_content_row() {
        let (mut model, mut app) = fixture();
        apply(
            &mut model,
            "context/set",
            json!({"items":[
                {"type":"message","role":"assistant","content":[{"type":"output_text","text":"One line."}]},
                {"type":"function_call","call_id":"next","name":"read","arguments":"{}"}
            ]}),
        );
        app.apply(model.change());
        layout(&mut app);
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|f| app.render(f)).unwrap();
        assert_eq!(app.heights.values, vec![2, 1]);
        assert_eq!(
            app.hits.iter().map(|h| h.y).collect::<Vec<_>>(),
            vec![2, 3, 4]
        );
        let buffer = terminal.backend().buffer();
        assert_eq!(buffer[(0, 3)].symbol(), "╰");
        assert_eq!(buffer[(2, 3)].symbol(), "O");
        assert_eq!(buffer[(2, 4)].symbol(), "▸");
    }

    #[test]
    fn tool_headers_show_only_failure_status_without_error_payloads() {
        let (mut model, mut app) = fixture();
        apply(
            &mut model,
            "tool/start",
            json!({"callId":"c", "name":"read", "arguments":{}}),
        );
        app.apply(model.change());
        assert!(
            !app.title(&app.cards["tool:c"])
                .to_string()
                .contains("Running")
        );
        apply(
            &mut model,
            "tool/end",
            json!({"callId":"c", "result":"done"}),
        );
        app.apply(model.change());
        assert!(
            !app.title(&app.cards["tool:c"])
                .to_string()
                .contains("Completed")
        );
        apply(
            &mut model,
            "tool/end",
            json!({"callId":"c", "error":{"message":"long error"}}),
        );
        app.apply(model.change());
        let title = app.title(&app.cards["tool:c"]).to_string();
        assert!(title.ends_with(" · Failed"));
        assert!(!title.contains("long error"));
    }

    #[test]
    fn link_hit_targets_follow_unicode_wrapping_tables_and_horizontal_scroll() {
        let (mut model, mut app) = fixture();
        let source = "中 [**#16712**](https://example.com/16712) and [second](https://example.com/second).\n\n| PR | Note |\n|---|---|\n| [#16713](https://example.com/16713) | A wide table cell |";
        apply(
            &mut model,
            "context/set",
            json!({"items":[
                {"type":"message","role":"assistant","content":[{"type":"output_text","text":source}]}
            ]}),
        );
        app.apply(model.change());
        let mut urls = HashSet::new();
        for width in [75, 14] {
            app.width = width;
            layout(&mut app);
            for horizontal in [0, 4] {
                app.horizontal = horizontal;
                let mut terminal = Terminal::new(TestBackend::new(width as u16 + 5, 30)).unwrap();
                terminal.draw(|f| app.render(f)).unwrap();
                let links = app.hyperlinks();
                let url_at = |x, y| {
                    links
                        .iter()
                        .find(|link| link.y == y && link.start <= x && x < link.end)
                        .map(|link| link.url.as_str())
                };
                for hit in app.hits.iter().filter(|h| !h.title) {
                    let mut column = 0;
                    let doc = &app.docs[&hit.id];
                    assert!(!doc.plain.contains("https://"));
                    for (byte, g) in hit.text.grapheme_indices(true) {
                        if column >= horizontal && column + g.width() <= horizontal + width {
                            let expected = doc
                                .links
                                .iter()
                                .find(|l| l.start <= hit.start + byte && hit.start + byte < l.end)
                                .map(|l| l.url.as_str());
                            let actual = url_at((column - horizontal + 2) as u16, hit.y);
                            assert_eq!(actual, expected);
                            if let Some(url) = actual {
                                urls.insert(url.to_string());
                            }
                        }
                        column += g.width();
                    }
                    assert!(url_at(0, hit.y).is_none());
                    assert!(url_at(width as u16 + 2, hit.y).is_none());
                }
            }
        }
        assert_eq!(urls.len(), 3);
        key(&mut app, KeyCode::Char('m'));
        assert!(app.hyperlinks().is_empty());
    }
}
impl Heights {
    fn sum(&self, mut end: usize) -> usize {
        let mut n = 0;
        while end > 0 {
            n += self.tree[end - 1];
            end &= end - 1;
        }
        n
    }
    fn push(&mut self, height: usize) {
        let n = self.values.len() + 1;
        let low = n.isolate_lowest_one();
        let value = self.sum(n - 1) - self.sum(n - low) + height;
        self.values.push(height);
        self.tree.push(value);
    }
    fn set(&mut self, i: usize, height: usize) {
        let old = self.values[i];
        self.values[i] = height;
        let mut p = i + 1;
        while p <= self.tree.len() {
            self.tree[p - 1] = self.tree[p - 1] + height - old;
            p += p.isolate_lowest_one();
        }
    }
    fn total(&self) -> usize {
        self.sum(self.values.len())
    }
    fn locate(&self, offset: usize) -> usize {
        let mut lo = 0;
        let mut hi = self.values.len();
        while lo < hi {
            let mid = (lo + hi) / 2;
            if self.sum(mid + 1) <= offset {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        lo.min(self.values.len().saturating_sub(1))
    }
}

#[derive(Clone)]
struct Point {
    id: String,
    byte: usize,
}
#[derive(Clone)]
struct Selection {
    start: Point,
    end: Point,
}
struct Hit {
    y: u16,
    id: String,
    start: usize,
    end: usize,
    text: String,
    title: bool,
}

#[derive(PartialEq, Eq)]
enum Panel {
    Help,
    Stats,
    Commands,
    Search,
    Menu,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Preset {
    Collapsed,
    Conversation,
    Expanded,
}
const PRESETS: [Preset; 3] = [Preset::Collapsed, Preset::Conversation, Preset::Expanded];
impl Preset {
    fn opens(self, kind: Kind) -> bool {
        !kind.heading()
            && match self {
                Self::Collapsed => false,
                Self::Conversation => kind.default_open(),
                Self::Expanded => true,
            }
    }
    fn label(self) -> &'static str {
        match self {
            Self::Collapsed => "Collapsed",
            Self::Conversation => "Conversation",
            Self::Expanded => "Expanded",
        }
    }
}

pub struct App {
    pub summary: Summary,
    pub path: String,
    pub order: Vec<String>,
    pub cards: HashMap<String, Arc<Card>>,
    positions: HashMap<String, usize>,
    expanded: HashMap<String, bool>,
    preset: Preset,
    view_mismatches: [usize; 3],
    heights: Heights,
    docs: HashMap<String, Document>,
    requested: HashSet<(String, u64, usize, bool)>,
    jobs: Sender<Job>,
    completed: Receiver<Document>,
    pub scroll: usize,
    pub horizontal: usize,
    pub focus: Option<String>,
    pub follow: bool,
    pub unseen: usize,
    width: usize,
    viewport: usize,
    selection: Option<Selection>,
    hits: Vec<Hit>,
    drag_scrollbar: bool,
    panel: Option<Panel>,
    input: String,
    search: String,
    matches: Vec<String>,
    match_index: usize,
    search_result: Option<Receiver<Vec<String>>>,
    panel_scroll: u16,
    menu_index: usize,
    theme: usize,
    pub notice: String,
    pub quit: bool,
    pub dirty: bool,
}

const COMMANDS: &[(&str, &str)] = &[
    ("collapse", "1 · Collapse all cards"),
    ("conversation", "2 · Expand only user and assistant answer"),
    ("expand", "3 · Expand all cards"),
    ("copy", "Copy selection or card"),
    ("answer", "Copy latest answer"),
    ("follow", "Go to bottom"),
    ("stats", "Token statistics"),
    ("theme", "Change theme"),
    ("help", "Keyboard shortcuts"),
    ("error", "Next error"),
    ("turn", "Next turn"),
    ("step", "Next step"),
    ("link", "Open link in card"),
    ("quit", "Quit"),
];
const HELP: &str = "j/k ↑/↓        Scroll (pauses follow)\nPgUp/PgDn      Page up / down\nHome/End       Top / follow bottom\nTab/Shift-Tab  Next / previous card\nEnter/Space    Expand / collapse card\n1              Collapse all cards\n2              Expand user and assistant answer\n3              Expand all cards\ne / E          Expand / collapse all (aliases)\nh/l ←/→        Scroll horizontally\n/              Search text and tool names\nn / N          Next / previous match\n] / [          Next / previous turn\n} / {          Next / previous step\n!              Next error\nc / y          Copy selection or card / latest answer\ns              Token statistics\nt              Change theme\n: or Ctrl-P    Command palette\nm              Menu\no              Open first link in card\nCtrl+click     Open link under pointer\n? / F1         Help\nEsc            Close panel / clear selection\nq / Ctrl-C     Quit\n\nManual toggles show Custom when no preset matches.\nNew cards follow the last preset in Custom.\nClick a title to toggle a card. Drag to select text.\nRight-click to copy. Scroll with the wheel or scrollbar.\nCopy uses OSC 52 with tmux passthrough.";

impl App {
    pub fn new(path: String) -> Self {
        let (jobs, completed) = document::spawn();
        Self {
            summary: Summary::default(),
            path,
            order: vec![],
            cards: HashMap::new(),
            positions: HashMap::new(),
            expanded: HashMap::new(),
            preset: Preset::Conversation,
            view_mismatches: [0; 3],
            heights: Heights::default(),
            docs: HashMap::new(),
            requested: HashSet::new(),
            jobs,
            completed,
            scroll: 0,
            horizontal: 0,
            focus: None,
            follow: true,
            unseen: 0,
            width: 80,
            viewport: 24,
            selection: None,
            hits: vec![],
            drag_scrollbar: false,
            panel: None,
            input: String::new(),
            search: String::new(),
            matches: vec![],
            match_index: 0,
            search_result: None,
            panel_scroll: 0,
            menu_index: 0,
            theme: 0,
            notice: String::new(),
            quit: false,
            dirty: true,
        }
    }
    fn open(&self, id: &str) -> bool {
        self.expanded
            .get(id)
            .copied()
            .unwrap_or_else(|| self.preset.opens(self.cards[id].kind))
    }
    fn view(&self) -> Option<Preset> {
        if self.view_mismatches[self.preset as usize] == 0 {
            Some(self.preset)
        } else {
            PRESETS
                .into_iter()
                .find(|preset| self.view_mismatches[*preset as usize] == 0)
        }
    }
    fn view_label(&self) -> &'static str {
        self.view().map(Preset::label).unwrap_or("Custom")
    }
    fn count_view(&mut self, kind: Kind, open: bool, add: bool) {
        if kind.heading() {
            return;
        }
        for preset in PRESETS {
            if preset.opens(kind) != open {
                let count = &mut self.view_mismatches[preset as usize];
                if add {
                    *count += 1;
                } else {
                    *count -= 1;
                }
            }
        }
    }
    fn settle_view(&mut self) {
        if let Some(preset) = self.view() {
            self.preset = preset;
            self.expanded.clear();
        }
        if self.notice.starts_with("View: ") {
            self.notice = format!("View: {}", self.view_label());
        }
    }
    fn height(&self, id: &str) -> usize {
        let c = &self.cards[id];
        let open = self.open(id);
        if c.kind.heading() || !open {
            return 1;
        }
        if let Some(d) = self
            .docs
            .get(id)
            .filter(|d| d.width == self.width && d.expanded == open)
        {
            return 1 + d.rows.len();
        }
        2
    }
    fn anchor(&self) -> Option<(String, usize, Option<usize>)> {
        if self.order.is_empty() {
            return None;
        }
        let i = self.heights.locate(self.scroll);
        let id = self.order[i].clone();
        let row = self.scroll.saturating_sub(self.heights.sum(i));
        let byte = row
            .checked_sub(1)
            .and_then(|r| self.docs.get(&id)?.rows.get(r).map(|r| r.start));
        Some((id, row, byte))
    }
    fn restore(&mut self, anchor: Option<(String, usize, Option<usize>)>) {
        if self.follow {
            self.scroll = self.heights.total().saturating_sub(self.viewport);
        } else if let Some((id, offset, byte)) = anchor
            && let Some(i) = self.positions.get(&id)
        {
            let offset = byte
                .and_then(|byte| {
                    self.docs
                        .get(&id)?
                        .rows
                        .iter()
                        .position(|r| r.start <= byte && byte < r.end)
                        .map(|r| r + 1)
                })
                .unwrap_or(offset);
            self.scroll =
                self.heights.sum(*i) + offset.min(self.heights.values[*i].saturating_sub(1));
        }
        self.scroll = self
            .scroll
            .min(self.heights.total().saturating_sub(self.viewport));
    }
    pub fn apply(&mut self, change: Change) {
        let anchor = self.anchor();
        let old_events = self.summary.events;
        for c in change.cards {
            if c.kind != Kind::Request {
                if self.positions.contains_key(&c.id)
                    && let Some(old) = self.cards.get(&c.id)
                    && old.kind != c.kind
                {
                    let kind = old.kind;
                    self.count_view(kind, self.open(&c.id), false);
                    let open = self
                        .expanded
                        .get(&c.id)
                        .copied()
                        .unwrap_or_else(|| self.preset.opens(c.kind));
                    self.count_view(c.kind, open, true);
                }
                self.cards.insert(c.id.clone(), c);
            }
        }
        if let Some(order) = change.reset {
            self.order = order
                .into_iter()
                .filter(|id| self.cards.contains_key(id))
                .collect();
            let live: HashSet<_> = self.order.iter().cloned().collect();
            self.cards.retain(|id, _| live.contains(id));
            self.docs.retain(|id, _| live.contains(id));
            self.expanded.retain(|id, _| live.contains(id));
            self.positions.clear();
            self.heights = Heights::default();
            self.view_mismatches = [0; 3];
            for i in 0..self.order.len() {
                let id = self.order[i].clone();
                self.positions.insert(id.clone(), i);
                self.heights.push(self.height(&id));
                self.count_view(self.cards[&id].kind, self.open(&id), true);
            }
            if self.focus.as_ref().is_some_and(|id| !live.contains(id)) {
                self.focus = self.order.first().cloned();
            }
            if self
                .selection
                .as_ref()
                .is_some_and(|s| !live.contains(&s.start.id) || !live.contains(&s.end.id))
            {
                self.selection = None;
            }
        } else {
            for id in change.append {
                if !self.positions.contains_key(&id) && self.cards.contains_key(&id) {
                    self.positions.insert(id.clone(), self.order.len());
                    self.heights.push(self.height(&id));
                    self.count_view(self.cards[&id].kind, self.open(&id), true);
                    self.order.push(id);
                }
            }
        }
        self.settle_view();
        self.summary = change.summary;
        if !self.follow {
            self.unseen += self.summary.events.saturating_sub(old_events);
        }
        self.restore(anchor);
        if self.summary.ended {
            self.follow = false;
        }
        if self.focus.is_none() {
            self.focus = self.order.last().cloned();
        }
        self.dirty = true;
    }
    pub fn poll_layout(&mut self) {
        if let Some(matches) = self
            .search_result
            .as_ref()
            .and_then(|rx| rx.try_recv().ok())
        {
            self.search_result = None;
            self.matches = matches
                .into_iter()
                .filter(|id| self.cards.contains_key(id))
                .collect();
            self.match_index = self.matches.len().saturating_sub(1);
            self.next_match(true);
            self.notice = format!(
                "\"{}\" · {} matching cards",
                self.search,
                self.matches.len()
            );
            self.dirty = true;
        }
        let anchor = self.anchor();
        let mut changed = false;
        for _ in 0..64 {
            let Ok(doc) = self.completed.try_recv() else {
                break;
            };
            self.requested
                .remove(&(doc.id.clone(), doc.revision, doc.width, doc.expanded));
            if self
                .cards
                .get(&doc.id)
                .is_none_or(|c| c.revision != doc.revision)
                || doc.width != self.width
                || doc.expanded != self.open(&doc.id)
            {
                self.dirty = true;
                continue;
            }
            let id = doc.id.clone();
            if let Some(selection) = &self.selection
                && let Some(old) = self.docs.get(&id)
            {
                let max_byte = [&selection.start, &selection.end]
                    .iter()
                    .filter(|p| p.id == id)
                    .map(|p| p.byte)
                    .max()
                    .unwrap_or(0);
                if max_byte > 0 && old.plain.get(..max_byte) != doc.plain.get(..max_byte) {
                    self.selection = None;
                }
            }
            self.docs.insert(id.clone(), doc);
            if let Some(i) = self.positions.get(&id).copied() {
                self.heights.set(i, self.height(&id));
            }
            changed = true;
        }
        if changed {
            self.restore(anchor);
            self.dirty = true;
        }
    }
    fn request(&mut self, id: &str) {
        let c = &self.cards[id];
        let expanded = self.open(id);
        if c.kind.heading() || !expanded {
            return;
        }
        if self.docs.get(id).is_some_and(|d| {
            d.width == self.width && d.revision == c.revision && d.expanded == expanded
        }) {
            return;
        }
        // At most one outstanding layout per card; a newer revision follows completion.
        if self.requested.iter().any(|(key, _, _, _)| key == id) {
            return;
        }
        self.requested
            .insert((id.into(), c.revision, self.width, expanded));
        let _ = self.jobs.send(Job {
            card: c.clone(),
            width: self.width,
            expanded,
        });
    }
    fn background(&self) -> Color {
        [
            Color::Rgb(20, 23, 29),
            Color::Rgb(28, 27, 37),
            Color::Rgb(0, 0, 0),
        ][self.theme]
    }
    fn focus_background(&self) -> Color {
        [
            Color::Rgb(31, 36, 46),
            Color::Rgb(43, 39, 54),
            Color::Rgb(24, 28, 34),
        ][self.theme]
    }
    fn color(kind: Kind) -> Color {
        match kind {
            Kind::User => Color::Rgb(229, 192, 123),
            Kind::System => Color::Rgb(141, 153, 172),
            Kind::Think => Color::Rgb(197, 153, 220),
            Kind::Answer => Color::Rgb(133, 202, 161),
            Kind::Tool => Color::Rgb(109, 175, 219),
            Kind::Error => Color::Rgb(242, 120, 120),
            _ => Color::Rgb(150, 163, 183),
        }
    }
    fn title(&self, c: &Card) -> Line<'static> {
        let duration = c
            .duration_ms
            .map(|n| format!(" · {}", duration(n)))
            .unwrap_or_default();
        if c.kind.heading() {
            return Line::styled(
                document::safe(&format!(
                    "─ {} {}{}{}",
                    c.kind.label(),
                    c.name,
                    duration,
                    if c.status.is_empty() {
                        String::new()
                    } else {
                        format!(" · {}", c.status)
                    }
                )),
                Style::default().fg(Color::Rgb(83, 94, 112)),
            );
        }
        let symbol = if self.open(&c.id) { "▾" } else { "▸" };
        let time = if duration.is_empty() {
            " · —".into()
        } else {
            duration
        };
        Line::from(vec![
            Span::styled(
                document::safe(&format!(
                    "{symbol} {}{}",
                    c.kind.label(),
                    if c.name.is_empty() {
                        String::new()
                    } else {
                        format!(" {}", c.name)
                    }
                )),
                Style::default()
                    .fg(Self::color(c.kind))
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                format!(
                    " · {} tokens{}{}{}",
                    number(c.tokens),
                    if c.provisional { " · Pending" } else { "" },
                    time,
                    if c.status.is_empty() || (c.kind == Kind::Tool && c.status != "Failed") {
                        String::new()
                    } else {
                        format!(" · {}", document::safe(&c.status).replace('\n', " "))
                    }
                ),
                Style::default().fg(Color::Gray),
            ),
        ])
    }
    pub fn render(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let width = area.width.saturating_sub(5).max(1) as usize;
        self.viewport = area.height.saturating_sub(3).max(1) as usize;
        if width != self.width {
            self.width = width;
            self.dirty = true;
        }
        let bg = self.background();
        frame.render_widget(
            Block::default().style(Style::default().bg(bg).fg(Color::Rgb(217, 222, 232))),
            area,
        );
        if area.height < 4 || area.width < 12 {
            return;
        }
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled(
                    format!(" {} ", document::safe(&self.summary.title)),
                    Style::default()
                        .fg(Color::White)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    document::safe(&self.path),
                    Style::default().fg(Color::DarkGray),
                ),
            ])),
            Rect::new(0, 0, area.width, 1),
        );
        frame.render_widget(
            Paragraph::new("─".repeat(area.width as usize))
                .style(Style::default().fg(Color::Rgb(48, 55, 67))),
            Rect::new(0, 1, area.width, 1),
        );
        self.hits.clear();
        if !self.order.is_empty() {
            let mut i = self.heights.locate(self.scroll);
            let mut position = self.heights.sum(i);
            let bottom = self.scroll + self.viewport;
            while i < self.order.len() && position < bottom {
                let id = self.order[i].clone();
                self.request(&id);
                let card = self.cards[&id].clone();
                let height = self.heights.values[i];
                let focused = self.focus.as_ref() == Some(&id);
                for row in 0..height {
                    let absolute = position + row;
                    if absolute < self.scroll || absolute >= bottom {
                        continue;
                    }
                    let y = 2 + (absolute - self.scroll) as u16;
                    if card.kind.heading() {
                        frame.render_widget(
                            Paragraph::new(self.title(&card)),
                            Rect::new(1, y, area.width - 2, 1),
                        );
                        self.hits.push(Hit {
                            y,
                            id: id.clone(),
                            start: 0,
                            end: 0,
                            text: String::new(),
                            title: true,
                        });
                        continue;
                    }
                    if focused {
                        frame.render_widget(
                            Block::default().style(Style::default().bg(self.focus_background())),
                            Rect::new(0, y, area.width - 1, 1),
                        );
                    }
                    if self.open(&id) {
                        frame.render_widget(
                            Paragraph::new(if row == 0 {
                                "╭"
                            } else if row == height - 1 {
                                "╰"
                            } else {
                                "│"
                            })
                            .style(Style::default().fg(Self::color(card.kind))),
                            Rect::new(0, y, 1, 1),
                        );
                    }
                    if row == 0 {
                        frame.render_widget(
                            Paragraph::new(self.title(&card)),
                            Rect::new(2, y, area.width - 4, 1),
                        );
                        self.hits.push(Hit {
                            y,
                            id: id.clone(),
                            start: 0,
                            end: 0,
                            text: String::new(),
                            title: true,
                        });
                    } else {
                        if let Some(doc) =
                            self.docs.get(&id).filter(|d| d.expanded == self.open(&id))
                        {
                            if let Some(r) = doc.rows.get(row - 1) {
                                let mut line = document::clip(&r.line, self.horizontal, self.width);
                                if let Some((start, end)) =
                                    self.selection_range(&id, doc.plain.len())
                                {
                                    line = self.highlight(
                                        line,
                                        &doc.plain[r.start..r.end],
                                        r.start,
                                        start,
                                        end,
                                    );
                                }
                                frame.render_widget(
                                    Paragraph::new(line),
                                    Rect::new(2, y, area.width - 5, 1),
                                );
                                self.hits.push(Hit {
                                    y,
                                    id: id.clone(),
                                    start: r.start,
                                    end: r.end,
                                    text: doc.plain[r.start..r.end].into(),
                                    title: false,
                                });
                            }
                        } else if row == 1 {
                            frame.render_widget(
                                Paragraph::new("Loading…")
                                    .style(Style::default().fg(Color::DarkGray)),
                                Rect::new(2, y, area.width - 5, 1),
                            );
                        }
                    }
                }
                position += height;
                i += 1;
            }
        }
        let total = self.heights.total().max(1);
        let bar_height = (self.viewport * self.viewport / total).clamp(1, self.viewport);
        let bar_top = if total > self.viewport {
            self.scroll * (self.viewport - bar_height) / (total - self.viewport)
        } else {
            0
        };
        for y in 0..self.viewport {
            frame.render_widget(
                Paragraph::new(if y >= bar_top && y < bar_top + bar_height {
                    "█"
                } else {
                    "│"
                })
                .style(Style::default().fg(Color::Rgb(84, 96, 114))),
                Rect::new(area.width - 1, 2 + y as u16, 1, 1),
            );
        }
        let mut status = self.summary.status.clone();
        if !self.notice.is_empty() {
            status.push_str(&format!(" · {}", self.notice));
        } else if self.unseen > 0 {
            status.push_str(&format!(" · ↑ {} new events · End to follow", self.unseen));
        }
        let stats = format!("  {}", self.summary.footer);
        let status_width = (area.width as usize).saturating_sub(stats.width() + 1);
        let mut footer = document::clip(
            &Line::styled(document::safe(&status), Style::default().fg(Color::Gray)),
            0,
            status_width,
        );
        footer.spans.insert(0, Span::raw(" "));
        footer.spans.push(Span::raw(stats));
        frame.render_widget(
            Paragraph::new(footer)
                .style(Style::default().fg(Color::White).bg(Color::Rgb(38, 44, 55))),
            Rect::new(0, area.height - 1, area.width, 1),
        );
        if self.panel.is_some() {
            self.render_panel(frame);
        }
        self.dirty = false;
    }
    fn highlight(
        &self,
        line: Line<'static>,
        source: &str,
        base: usize,
        start: usize,
        end: usize,
    ) -> Line<'static> {
        let mut byte = base;
        let mut x = 0;
        for g in source.graphemes(true) {
            if x >= self.horizontal {
                break;
            }
            x += g.width();
            byte += g.len();
        }
        let mut spans = vec![];
        for span in line.spans {
            for g in span.content.graphemes(true) {
                let style = if byte < end && byte + g.len() > start {
                    span.style.bg(Color::Rgb(75, 91, 126)).fg(Color::White)
                } else {
                    span.style
                };
                spans.push(Span::styled(g.to_string(), style));
                byte += g.len();
            }
        }
        Line::from(spans)
    }
    fn selection_range(&self, id: &str, len: usize) -> Option<(usize, usize)> {
        let s = self.selection.as_ref()?;
        let a = *self.positions.get(&s.start.id)?;
        let b = *self.positions.get(&s.end.id)?;
        let i = *self.positions.get(id)?;
        let (start, end, ai, bi) = if (a, s.start.byte) <= (b, s.end.byte) {
            (&s.start, &s.end, a, b)
        } else {
            (&s.end, &s.start, b, a)
        };
        if i < ai || i > bi {
            return None;
        }
        Some((
            if i == ai { start.byte.min(len) } else { 0 },
            if i == bi { end.byte.min(len) } else { len },
        ))
    }
    fn render_panel(&self, f: &mut Frame) {
        let a = f.area();
        let width = a.width.saturating_sub(6).min(94);
        let height = a.height.saturating_sub(4).min(32);
        let rect = Rect::new(
            (a.width - width) / 2,
            (a.height - height) / 2,
            width,
            height,
        );
        let (title, body) = match self.panel.as_ref().unwrap() {
            Panel::Help => ("Shortcuts · Esc to close", HELP.to_string()),
            Panel::Stats => (
                "Tokens · ↑↓ to scroll · Esc to close",
                self.summary.details.clone(),
            ),
            Panel::Search => (
                "Search · Enter to find",
                format!(
                    "/{}\n\nSearch text, reasoning, arguments, results and tool names.\nUse n/N to move between matches.",
                    self.input
                ),
            ),
            Panel::Commands | Panel::Menu => (
                "Commands · ↑↓ to select · Enter to run",
                format!(
                    ":{}    View: {}\n\n{}",
                    self.input,
                    self.view_label(),
                    COMMANDS
                        .iter()
                        .filter(|(name, label)| name.contains(&self.input)
                            || label.contains(&self.input))
                        .enumerate()
                        .map(|(i, (name, label))| format!(
                            "{} {name:<14} {label}",
                            if i == self.menu_index { "›" } else { " " }
                        ))
                        .collect::<Vec<_>>()
                        .join("\n")
                ),
            ),
        };
        f.render_widget(Clear, rect);
        f.render_widget(
            Paragraph::new(document::safe(&body))
                .block(
                    Block::default()
                        .title(title)
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(Color::Cyan)),
                )
                .style(Style::default().bg(self.background()).fg(Color::White))
                .wrap(Wrap { trim: false })
                .scroll((self.panel_scroll, 0)),
            rect,
        );
    }
    fn toggle(&mut self, id: &str) {
        if self.cards[id].kind.heading() {
            return;
        }
        let anchor = self.anchor();
        let open = !self.open(id);
        self.set_open(id, open);
        self.notice = format!("View: {}", self.view_label());
        self.restore(anchor);
        self.dirty = true;
    }
    fn set_open(&mut self, id: &str, open: bool) {
        let kind = self.cards[id].kind;
        if kind.heading() || self.open(id) == open {
            return;
        }
        self.count_view(kind, self.open(id), false);
        if open == self.preset.opens(kind) {
            self.expanded.remove(id);
        } else {
            self.expanded.insert(id.into(), open);
        }
        self.count_view(kind, open, true);
        self.settle_view();
        if let Some(i) = self.positions.get(id).copied() {
            self.heights.set(i, self.height(id));
        }
    }
    fn set_view(&mut self, preset: Preset) {
        let anchor = self.anchor();
        self.preset = preset;
        self.expanded.clear();
        self.view_mismatches = [0; 3];
        for i in 0..self.order.len() {
            let id = self.order[i].clone();
            self.count_view(self.cards[&id].kind, self.open(&id), true);
            self.heights.set(i, self.height(&id));
        }
        self.notice = format!("View: {}", self.view_label());
        self.restore(anchor);
        self.dirty = true;
    }
    fn scroll_by(&mut self, delta: isize) {
        self.follow = false;
        self.notice.clear();
        self.scroll = self
            .scroll
            .saturating_add_signed(delta)
            .min(self.heights.total().saturating_sub(self.viewport));
    }
    fn jump(&mut self, id: String) {
        if let Some(i) = self.positions.get(&id) {
            self.scroll = self
                .heights
                .sum(*i)
                .min(self.heights.total().saturating_sub(self.viewport));
            self.focus = Some(id);
            self.follow = false;
        }
    }
    fn navigate(&mut self, forward: bool, predicate: impl Fn(&Card) -> bool) {
        let current = self
            .focus
            .as_ref()
            .and_then(|id| self.positions.get(id))
            .copied()
            .unwrap_or(0);
        let n = self.order.len();
        if n == 0 {
            return;
        }
        for offset in 1..=n {
            let i = if forward {
                (current + offset) % n
            } else {
                (current + n - offset % n) % n
            };
            if predicate(&self.cards[&self.order[i]]) {
                self.jump(self.order[i].clone());
                break;
            }
        }
    }
    fn search(&mut self) {
        self.search = self.input.to_lowercase();
        self.match_index = 0;
        let cards: Vec<_> = self.order.iter().map(|id| self.cards[id].clone()).collect();
        let query = self.search.clone();
        let (tx, rx) = std::sync::mpsc::channel();
        self.search_result = Some(rx);
        self.notice = format!("Searching \"{}\"…", self.search);
        std::thread::spawn(move || {
            let matches = cards
                .iter()
                .filter(|c| {
                    [
                        &c.name,
                        &c.text,
                        &c.arguments,
                        c.result.as_deref().unwrap_or(""),
                        c.recorded_result.as_deref().unwrap_or(""),
                    ]
                    .iter()
                    .any(|s| s.to_lowercase().contains(&query))
                })
                .map(|c| c.id.clone())
                .collect();
            let _ = tx.send(matches);
        });
    }
    fn next_match(&mut self, forward: bool) {
        if self.matches.is_empty() {
            return;
        }
        self.match_index = if forward {
            (self.match_index + 1) % self.matches.len()
        } else {
            (self.match_index + self.matches.len() - 1) % self.matches.len()
        };
        let id = self.matches[self.match_index].clone();
        if self.cards.contains_key(&id) {
            self.set_open(&id, true);
            self.jump(id);
        }
    }
    pub fn selected_text(&self) -> Option<String> {
        self.selection.as_ref()?;
        let mut chunks = vec![];
        for id in &self.order {
            if let Some(d) = self.docs.get(id)
                && let Some((start, end)) = self.selection_range(id, d.plain.len())
                && let Some(text) = d.plain.get(start..end).filter(|s| !s.is_empty())
            {
                chunks.push(text);
            }
        }
        if chunks.is_empty() {
            None
        } else {
            Some(chunks.join("\n"))
        }
    }
    pub fn copy_text(&self, latest: bool) -> Option<String> {
        if !latest && let Some(s) = self.selected_text() {
            return Some(s);
        }
        let card = if latest {
            self.order
                .iter()
                .rev()
                .filter_map(|id| self.cards.get(id))
                .find(|c| c.kind == Kind::Answer)
        } else {
            self.focus.as_ref().and_then(|id| self.cards.get(id))
        }?;
        Some(if card.kind == Kind::Tool {
            format!("{}\n{}\n{}", card.name, card.arguments, card.result_text())
        } else {
            card.text.clone()
        })
    }
    fn copy(&mut self, latest: bool) {
        if let Some(text) = self.copy_text(latest) {
            let osc = format!(
                "\x1b]52;c;{}\x07",
                base64::engine::general_purpose::STANDARD.encode(text.as_bytes())
            );
            let sequence = if std::env::var_os("TMUX").is_some() {
                format!("\x1bPtmux;{}\x1b\\", osc.replace('\x1b', "\x1b\x1b"))
            } else {
                osc
            };
            match io::stdout()
                .write_all(sequence.as_bytes())
                .and_then(|_| io::stdout().flush())
            {
                Ok(()) => self.notice = format!("Sent to clipboard · {} bytes", text.len()),
                Err(e) => self.notice = format!("Copy failed: {e}"),
            }
        }
    }
    fn link(&mut self) {
        let Some(c) = self.focus.as_ref().and_then(|id| self.cards.get(id)) else {
            return;
        };
        let text = c.body(true);
        let url = pulldown_cmark::Parser::new(&text).find_map(|e| match e {
            pulldown_cmark::Event::Start(pulldown_cmark::Tag::Link { dest_url, .. })
                if document::web_link(&dest_url) =>
            {
                Some(dest_url.into_string())
            }
            _ => None,
        });
        if let Some(url) = url {
            self.open_link(&url);
        } else {
            self.notice = "No link in this card".into();
        }
    }
    fn open_link(&mut self, url: &str) {
        let result = std::process::Command::new(if cfg!(target_os = "macos") {
            "open"
        } else {
            "xdg-open"
        })
        .arg(url)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
        self.notice = match result {
            Ok(mut child) => {
                std::thread::spawn(move || {
                    let _ = child.wait();
                });
                "Opening link".into()
            }
            Err(e) => format!("Could not open link: {e}"),
        };
    }
    pub fn hyperlinks(&self) -> Vec<hyperlinks::Link> {
        if self.panel.is_some() {
            return vec![];
        }
        let mut links = vec![];
        for hit in self.hits.iter().filter(|h| !h.title) {
            let Some(doc) = self.docs.get(&hit.id) else {
                continue;
            };
            let first = doc.links.partition_point(|link| link.end <= hit.start);
            for link in doc.links[first..].iter().take_while(|l| l.start < hit.end) {
                let start = doc.plain[hit.start..link.start.max(hit.start)].width();
                let end = doc.plain[hit.start..link.end.min(hit.end)].width();
                let start = start.saturating_sub(self.horizontal).min(self.width);
                let end = end.saturating_sub(self.horizontal).min(self.width);
                if start < end {
                    links.push(hyperlinks::Link {
                        y: hit.y,
                        start: start as u16 + 2,
                        end: end as u16 + 2,
                        url: link.url.clone(),
                    });
                }
            }
        }
        links
    }
    fn command(&mut self, command: &str) {
        match command {
            "expand" => self.set_view(Preset::Expanded),
            "collapse" => self.set_view(Preset::Collapsed),
            "conversation" => self.set_view(Preset::Conversation),
            "copy" => self.copy(false),
            "answer" => self.copy(true),
            "follow" => {
                self.scroll = self.heights.total().saturating_sub(self.viewport);
                self.follow = !self.summary.ended;
                self.unseen = 0;
                self.notice.clear();
            }
            "stats" => self.panel = Some(Panel::Stats),
            "help" => self.panel = Some(Panel::Help),
            "theme" => self.theme = (self.theme + 1) % 3,
            "error" => self.navigate(true, |c| {
                c.kind == Kind::Error || c.status.starts_with("Failed")
            }),
            "turn" => self.navigate(true, |c| c.kind == Kind::Turn),
            "step" => self.navigate(true, |c| c.kind == Kind::Step),
            "link" => self.link(),
            "quit" => self.quit = true,
            _ => self.notice = format!("Unknown command: {command}"),
        }
    }
    fn chosen_command(&self, index: usize) -> Option<&'static str> {
        COMMANDS
            .iter()
            .filter(|(name, label)| name.contains(&self.input) || label.contains(&self.input))
            .nth(index)
            .map(|(name, _)| *name)
    }
    fn point(&self, x: u16, y: u16) -> Option<Point> {
        let hit = self.hits.iter().find(|h| h.y == y && !h.title)?;
        let mut byte = hit.start;
        let mut columns = 0;
        let target = x.saturating_sub(2) as usize + self.horizontal;
        for g in hit.text.graphemes(true) {
            if columns + g.width() > target {
                break;
            }
            columns += g.width();
            byte += g.len();
        }
        Some(Point {
            id: hit.id.clone(),
            byte: byte.min(hit.end),
        })
    }
    pub fn handle(&mut self, event: Event) {
        self.dirty = true;
        match event {
            Event::Key(key) if key.kind != KeyEventKind::Release => {
                if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
                    self.quit = true;
                    return;
                }
                if self.panel.is_some() {
                    match key.code {
                        KeyCode::Esc => self.panel = None,
                        KeyCode::Up => {
                            if matches!(self.panel, Some(Panel::Commands | Panel::Menu)) {
                                self.menu_index = self.menu_index.saturating_sub(1);
                            } else {
                                self.panel_scroll = self.panel_scroll.saturating_sub(1);
                            }
                        }
                        KeyCode::Down => {
                            if matches!(self.panel, Some(Panel::Commands | Panel::Menu)) {
                                if self.chosen_command(self.menu_index + 1).is_some() {
                                    self.menu_index += 1;
                                }
                            } else {
                                self.panel_scroll = self.panel_scroll.saturating_add(1);
                            }
                        }
                        KeyCode::PageDown => {
                            self.panel_scroll = self.panel_scroll.saturating_add(10)
                        }
                        KeyCode::PageUp => self.panel_scroll = self.panel_scroll.saturating_sub(10),
                        KeyCode::Backspace => {
                            self.input.pop();
                            self.menu_index = 0;
                        }
                        KeyCode::Enter => {
                            let panel = self.panel.take();
                            if panel == Some(Panel::Search) {
                                self.search();
                            } else if matches!(panel, Some(Panel::Commands | Panel::Menu))
                                && let Some(cmd) = self.chosen_command(self.menu_index)
                            {
                                self.command(cmd);
                            }
                        }
                        KeyCode::Char(c)
                            if matches!(
                                self.panel,
                                Some(Panel::Search | Panel::Commands | Panel::Menu)
                            ) =>
                        {
                            self.input.push(c);
                            self.menu_index = 0;
                        }
                        _ => (),
                    }
                    return;
                }
                match key.code {
                    KeyCode::Char('q') => self.quit = true,
                    KeyCode::Up | KeyCode::Char('k') => self.scroll_by(-3),
                    KeyCode::Down | KeyCode::Char('j') => self.scroll_by(3),
                    KeyCode::PageUp => self.scroll_by(-(self.viewport as isize)),
                    KeyCode::PageDown => self.scroll_by(self.viewport as isize),
                    KeyCode::Home => {
                        self.scroll = 0;
                        self.follow = false;
                    }
                    KeyCode::End => self.command("follow"),
                    KeyCode::Left | KeyCode::Char('h') => {
                        self.horizontal = self.horizontal.saturating_sub(8)
                    }
                    KeyCode::Right | KeyCode::Char('l') => self.horizontal += 8,
                    KeyCode::Tab => self.navigate(true, |c| !c.kind.heading()),
                    KeyCode::BackTab => self.navigate(false, |c| !c.kind.heading()),
                    KeyCode::Enter | KeyCode::Char(' ') => {
                        if let Some(id) = self.focus.clone() {
                            self.toggle(&id);
                        }
                    }
                    KeyCode::Char('1' | 'E') => self.command("collapse"),
                    KeyCode::Char('2') => self.command("conversation"),
                    KeyCode::Char('3' | 'e') => self.command("expand"),
                    KeyCode::Char('c') => self.copy(false),
                    KeyCode::Char('y') => self.copy(true),
                    KeyCode::Char('o') => self.link(),
                    KeyCode::Char('s') => self.command("stats"),
                    KeyCode::Char('t') => self.command("theme"),
                    KeyCode::Char('?') | KeyCode::F(1) => self.command("help"),
                    KeyCode::Char('/') => {
                        self.panel = Some(Panel::Search);
                        self.input.clear();
                    }
                    KeyCode::Char(':') => {
                        self.panel = Some(Panel::Commands);
                        self.input.clear();
                    }
                    KeyCode::Char('p') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.panel = Some(Panel::Commands);
                        self.input.clear();
                    }
                    KeyCode::Char('m') => {
                        self.panel = Some(Panel::Menu);
                        self.input.clear();
                    }
                    KeyCode::Char('n') => self.next_match(true),
                    KeyCode::Char('N') => self.next_match(false),
                    KeyCode::Char(']') => self.command("turn"),
                    KeyCode::Char('[') => self.navigate(false, |c| c.kind == Kind::Turn),
                    KeyCode::Char('}') => self.command("step"),
                    KeyCode::Char('{') => self.navigate(false, |c| c.kind == Kind::Step),
                    KeyCode::Char('!') => self.command("error"),
                    KeyCode::Esc => {
                        self.selection = None;
                        self.notice.clear();
                    }
                    _ => (),
                }
                self.panel_scroll = 0;
                self.menu_index = 0;
            }
            Event::Mouse(m) => {
                if self.panel.is_some() {
                    if matches!(self.panel, Some(Panel::Commands | Panel::Menu))
                        && m.kind == MouseEventKind::Down(MouseButton::Left)
                    {
                        let panel_height = self.viewport.saturating_sub(1).min(32);
                        let top = (self.viewport + 3 - panel_height) / 2;
                        if let Some(index) = (m.row as usize).checked_sub(top + 3)
                            && let Some(cmd) = self.chosen_command(index)
                        {
                            self.panel = None;
                            self.command(cmd);
                        }
                        return;
                    }
                    if matches!(m.kind, MouseEventKind::ScrollDown) {
                        self.panel_scroll = self.panel_scroll.saturating_add(3);
                    } else if matches!(m.kind, MouseEventKind::ScrollUp) {
                        self.panel_scroll = self.panel_scroll.saturating_sub(3);
                    }
                    return;
                }
                match m.kind {
                    MouseEventKind::ScrollUp => self.scroll_by(-3),
                    MouseEventKind::ScrollDown => self.scroll_by(3),
                    MouseEventKind::Down(MouseButton::Right) => {
                        if let Some(h) = self.hits.iter().find(|h| h.y == m.row) {
                            self.focus = Some(h.id.clone());
                        }
                        self.copy(false);
                    }
                    MouseEventKind::Down(MouseButton::Left) => {
                        if m.column as usize >= self.width + 4 {
                            self.drag_scrollbar = true;
                            self.follow = false;
                            self.scroll = m.row.saturating_sub(2) as usize
                                * self.heights.total().saturating_sub(self.viewport)
                                / self.viewport.max(1);
                        } else if let Some(hit) = self.hits.iter().find(|h| h.y == m.row) {
                            let id = hit.id.clone();
                            let title = hit.title;
                            self.focus = Some(id.clone());
                            if title {
                                self.toggle(&id);
                            } else if let Some(point) = self.point(m.column, m.row) {
                                self.selection = Some(Selection {
                                    start: point.clone(),
                                    end: point,
                                });
                                self.follow = false;
                            }
                        }
                    }
                    MouseEventKind::Drag(MouseButton::Left) => {
                        if self.drag_scrollbar {
                            self.scroll = (m.row.saturating_sub(2) as usize
                                * self.heights.total().saturating_sub(self.viewport)
                                / self.viewport.max(1))
                            .min(self.heights.total().saturating_sub(self.viewport));
                        } else {
                            if m.row <= 2 {
                                self.scroll_by(-1);
                            } else if m.row as usize > self.viewport {
                                self.scroll_by(1);
                            }
                            if let Some(p) = self.point(m.column, m.row)
                                && let Some(s) = &mut self.selection
                            {
                                s.end = p;
                            }
                        }
                    }
                    MouseEventKind::Up(MouseButton::Left) => self.drag_scrollbar = false,
                    _ => (),
                }
            }
            Event::Resize(_, _) => (),
            _ => (),
        }
    }
}
