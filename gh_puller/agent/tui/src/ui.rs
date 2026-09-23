//! Virtualized cards, persistent reading anchors, and terminal-local interaction.
use crate::{
    document::{self, Document, Job},
    model::{Card, Change, Kind, Summary},
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
    fn command_menu_and_async_search_are_usable() {
        let (_, mut app) = fixture();
        key(&mut app, KeyCode::Char(':'));
        for c in "coll".chars() {
            key(&mut app, KeyCode::Char(c));
        }
        key(&mut app, KeyCode::Enter);
        assert!(app.order.iter().all(|id| !app.open(id)));
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
        assert_eq!(footer.trim(), app.summary.footer);
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

pub struct App {
    pub summary: Summary,
    pub path: String,
    pub order: Vec<String>,
    pub cards: HashMap<String, Arc<Card>>,
    positions: HashMap<String, usize>,
    expanded: HashMap<String, bool>,
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
    ("expand", "全部展开"),
    ("collapse", "全部折叠"),
    ("copy", "复制选区或整块"),
    ("answer", "复制最新回答"),
    ("follow", "回到底部"),
    ("stats", "详细统计"),
    ("theme", "切换主题"),
    ("help", "快捷键"),
    ("error", "下一个错误"),
    ("turn", "下一轮"),
    ("step", "下一步/请求"),
    ("link", "打开当前块链接"),
    ("quit", "退出"),
];
const HELP: &str = "j/k ↑/↓       滚动（上翻暂停自动跟随）\nPgUp/PgDn      翻页\nHome/End       顶部 / 跟随底部\nTab/Shift-Tab  下一块 / 上一块\nEnter/Space    展开 / 折叠当前块\ne / E          全部展开 / 全部折叠\nh/l ←/→        横向浏览代码及宽表格\n/              搜索正文或工具名\nn / N          下一个 / 上一个匹配\n] / [          下一轮 / 上一轮\n} / {          下一步或请求 / 上一步或请求\n!              下一个错误块\nc / y          复制选区或当前整块 / 最新回答\ns              详细统计\nt              切换暗色主题\n: 或 Ctrl-P    命令面板\nm              临时菜单\no              打开当前块的第一个 HTTP(S) 链接\n? / F1         帮助\nEsc            关闭面板 / 清除选区\nq / Ctrl-C     退出\n\n鼠标：标题单击折叠；正文拖动选字；右键复制；\n滚轮滚动；右侧滚动条可拖动。\n复制使用 OSC 52；支持 tmux passthrough。\n所有时间来自事件，缺失轮/步边界时保持中性。";

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
            .unwrap_or_else(|| self.cards[id].kind.default_open())
    }
    fn height(&self, id: &str) -> usize {
        let c = &self.cards[id];
        if c.kind.heading() {
            return 1;
        }
        let open = self.open(id);
        if !open && c.kind != Kind::Tool {
            return 2;
        }
        if let Some(d) = self
            .docs
            .get(id)
            .filter(|d| d.width == self.width && d.expanded == open)
        {
            return 2 + d.rows.len();
        }
        if c.kind == Kind::Tool && !open { 6 } else { 3 }
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
            self.cards.insert(c.id.clone(), c);
        }
        if let Some(order) = change.reset {
            self.order = order;
            let live: HashSet<_> = self.order.iter().cloned().collect();
            self.cards.retain(|id, _| live.contains(id));
            self.docs.retain(|id, _| live.contains(id));
            self.expanded.retain(|id, _| live.contains(id));
            self.positions.clear();
            self.heights = Heights::default();
            for (i, id) in self.order.iter().enumerate() {
                self.positions.insert(id.clone(), i);
                self.heights.push(self.height(id));
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
                    self.order.push(id);
                }
            }
        }
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
            self.notice = format!("搜索「{}」 · {} 个匹配块", self.search, self.matches.len());
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
        if c.kind.heading() || (!expanded && c.kind != Kind::Tool) {
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
            .map(|n| format!(" · {:.2}s", n / 1000.0))
            .unwrap_or_default();
        if c.kind.heading() {
            return Line::styled(
                document::safe(&format!(
                    " {} {}{} {}",
                    c.kind.label(),
                    c.name,
                    duration,
                    c.status
                )),
                Style::default()
                    .fg(Self::color(c.kind))
                    .add_modifier(Modifier::BOLD),
            );
        }
        let symbol = if self.open(&c.id) { "▾" } else { "▸" };
        let time = if c.kind == Kind::Tool {
            duration
        } else {
            String::new()
        };
        Line::from(vec![
            Span::styled(
                document::safe(&format!("{symbol} {} {}", c.kind.label(), c.name)),
                Style::default()
                    .fg(Self::color(c.kind))
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                format!(
                    " · {} tokens{}{}{}",
                    number(c.tokens),
                    if c.provisional { " · 暂存" } else { "" },
                    time,
                    if c.status.is_empty() {
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
        self.viewport = area.height.saturating_sub(4).max(1) as usize;
        if width != self.width {
            self.width = width;
            self.dirty = true;
        }
        let bg = self.background();
        frame.render_widget(
            Block::default().style(Style::default().bg(bg).fg(Color::Rgb(217, 222, 232))),
            area,
        );
        if area.height < 5 || area.width < 12 {
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
                            Paragraph::new(self.title(&card))
                                .style(Style::default().bg(Color::Rgb(31, 36, 46))),
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
                    let edge = Style::default().fg(Self::color(card.kind));
                    frame.render_widget(
                        Paragraph::new(if row == 0 {
                            "╭"
                        } else if row == height - 1 {
                            "╰"
                        } else {
                            "│"
                        })
                        .style(edge),
                        Rect::new(0, y, 1, 1),
                    );
                    frame.render_widget(
                        Paragraph::new(if focused { "┃" } else { "│" }).style(Style::default().fg(
                            if focused {
                                Color::Gray
                            } else {
                                Color::Rgb(48, 55, 67)
                            },
                        )),
                        Rect::new(area.width - 2, y, 1, 1),
                    );
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
                    } else if row < height - 1 {
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
                                Paragraph::new("排版中…")
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
        let status = if !self.notice.is_empty() {
            self.notice.clone()
        } else if self.unseen > 0 {
            format!("↑ 已暂停跟随 · {} 条新事件 · End 回到底部", self.unseen)
        } else {
            self.summary.status.clone()
        };
        frame.render_widget(
            Paragraph::new(format!(" {}", document::safe(&status)))
                .style(Style::default().fg(Color::DarkGray)),
            Rect::new(0, area.height - 2, area.width, 1),
        );
        frame.render_widget(
            Paragraph::new(format!(" {}", self.summary.footer))
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
            Panel::Help => ("快捷键 · Esc 关闭", HELP.to_string()),
            Panel::Stats => ("统计 · ↑↓ 滚动 · Esc 关闭", self.summary.details.clone()),
            Panel::Search => (
                "搜索 · Enter 跳转",
                format!(
                    "/{}\n\n正文、思考、参数、结果及工具名；n/N 跳转匹配。",
                    self.input
                ),
            ),
            Panel::Commands | Panel::Menu => (
                "命令 · ↑↓ 选择 · Enter 执行",
                format!(
                    ":{}\n\n{}",
                    self.input,
                    COMMANDS
                        .iter()
                        .filter(|(name, label)| name.contains(&self.input)
                            || label.contains(&self.input))
                        .enumerate()
                        .map(|(i, (name, label))| format!(
                            "{} {name:<12} {label}",
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
        self.expanded.insert(id.into(), open);
        if let Some(i) = self.positions.get(id).copied() {
            self.heights.set(i, self.height(id));
        }
        self.restore(anchor);
        self.dirty = true;
    }
    fn all(&mut self, expanded: bool) {
        let anchor = self.anchor();
        for id in &self.order {
            self.expanded.insert(id.clone(), expanded);
        }
        for (i, id) in self.order.iter().enumerate() {
            self.heights.set(i, self.height(id));
        }
        self.restore(anchor);
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
        self.notice = format!("搜索「{}」…", self.search);
        std::thread::spawn(move || {
            let matches = cards
                .iter()
                .filter(|c| {
                    [
                        &c.name,
                        &c.text,
                        &c.arguments,
                        c.result.as_deref().unwrap_or(""),
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
            self.expanded.insert(id.clone(), true);
            let i = self.positions[&id];
            self.heights.set(i, self.height(&id));
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
            format!(
                "{}\n{}\n{}",
                card.name,
                card.arguments,
                card.result.as_deref().unwrap_or("")
            )
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
                Ok(()) => self.notice = format!("已发送 OSC 52 · {} bytes", text.len()),
                Err(e) => self.notice = format!("复制失败：{e}"),
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
                if dest_url.starts_with("https://") || dest_url.starts_with("http://") =>
            {
                Some(dest_url.into_string())
            }
            _ => None,
        });
        if let Some(url) = url {
            let result = std::process::Command::new(if cfg!(target_os = "macos") {
                "open"
            } else {
                "xdg-open"
            })
            .arg(&url)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
            self.notice = match result {
                Ok(mut child) => {
                    std::thread::spawn(move || {
                        let _ = child.wait();
                    });
                    format!("打开 {url}")
                }
                Err(e) => format!("无法打开链接：{e}"),
            };
        } else {
            self.notice = "当前块没有 HTTP(S) Markdown 链接".into();
        }
    }
    fn command(&mut self, command: &str) {
        match command {
            "expand" => self.all(true),
            "collapse" => self.all(false),
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
                c.kind == Kind::Error || c.status.starts_with("失败")
            }),
            "turn" => self.navigate(true, |c| c.kind == Kind::Turn),
            "step" => self.navigate(true, |c| matches!(c.kind, Kind::Step | Kind::Request)),
            "link" => self.link(),
            "quit" => self.quit = true,
            _ => self.notice = format!("未知命令：{command}"),
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
                    KeyCode::Char('e') => self.command("expand"),
                    KeyCode::Char('E') => self.command("collapse"),
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
                    KeyCode::Char('{') => {
                        self.navigate(false, |c| matches!(c.kind, Kind::Step | Kind::Request))
                    }
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
                        let panel_height = self.viewport.min(32);
                        let top = (self.viewport + 4 - panel_height) / 2;
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
