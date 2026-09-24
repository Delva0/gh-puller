//! Markdown preparation and wrapping run outside the terminal input thread.
use crate::model::{Card, Kind};
use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};
use std::{
    sync::{
        Arc,
        mpsc::{self, Receiver, Sender},
    },
    thread,
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

#[derive(Clone)]
pub struct Row {
    pub line: Line<'static>,
    pub start: usize,
    pub end: usize,
}
#[derive(Clone, Debug)]
pub struct Link {
    pub start: usize,
    pub end: usize,
    pub url: String,
}
pub struct Document {
    pub id: String,
    pub revision: u64,
    pub width: usize,
    pub expanded: bool,
    pub rows: Vec<Row>,
    pub plain: String,
    pub links: Vec<Link>,
}
pub struct Job {
    pub card: Arc<Card>,
    pub width: usize,
    pub expanded: bool,
}

#[derive(Default)]
struct SourceLine {
    spans: Vec<Span<'static>>,
    links: Vec<Link>,
    len: usize,
    nowrap: bool,
}
impl SourceLine {
    fn push(&mut self, span: Span<'static>, url: Option<&str>) {
        let start = self.len;
        self.len += span.content.len();
        if let Some(url) = url.filter(|url| web_link(url)) {
            if let Some(link) = self
                .links
                .last_mut()
                .filter(|l| l.end == start && l.url == url)
            {
                link.end = self.len;
            } else {
                self.links.push(Link {
                    start,
                    end: self.len,
                    url: url.into(),
                });
            }
        }
        self.spans.push(span);
    }
    fn append(&mut self, line: &Self) {
        self.links.extend(line.links.iter().map(|link| Link {
            start: self.len + link.start,
            end: self.len + link.end,
            url: link.url.clone(),
        }));
        self.len += line.len;
        self.spans.extend(line.spans.iter().cloned());
    }
    fn width(&self) -> usize {
        self.spans.iter().map(Span::width).sum()
    }
    fn styled(text: String, style: Style, nowrap: bool) -> Self {
        let mut line = Self {
            nowrap,
            ..Self::default()
        };
        line.push(Span::styled(text, style), None);
        line
    }
}
pub fn web_link(url: &str) -> bool {
    (url.starts_with("https://") || url.starts_with("http://"))
        && !url.chars().any(char::is_control)
}
pub fn safe(text: &str) -> String {
    text.chars()
        .filter(|c| !c.is_control() || *c == '\n' || *c == '\t')
        .collect::<String>()
        .replace('\t', "    ")
}

fn markdown(source: &str) -> Vec<SourceLine> {
    let mut lines = vec![];
    let mut line = SourceLine::default();
    let mut styles = vec![Style::default()];
    let mut code = false;
    let mut list_depth: usize = 0;
    let mut links: Vec<String> = vec![];
    let mut table: Vec<Vec<SourceLine>> = vec![];
    let mut row: Vec<SourceLine> = vec![];
    let mut cell: Option<SourceLine> = None;
    let push = |line: &mut SourceLine, lines: &mut Vec<SourceLine>| {
        lines.push(std::mem::take(line));
    };
    for event in Parser::new_ext(
        source,
        Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS,
    ) {
        match event {
            Event::Start(Tag::Table(_)) => {
                if !line.spans.is_empty() {
                    push(&mut line, &mut lines);
                }
                table.clear();
            }
            Event::Start(Tag::TableHead | Tag::TableRow) => row.clear(),
            Event::Start(Tag::TableCell) => cell = Some(SourceLine::default()),
            Event::End(TagEnd::TableCell) => row.push(cell.take().unwrap_or_default()),
            Event::End(TagEnd::TableHead | TagEnd::TableRow) => {
                table.push(std::mem::take(&mut row))
            }
            Event::End(TagEnd::Table) => {
                let cols = table.iter().map(Vec::len).max().unwrap_or(0);
                let widths: Vec<usize> = (0..cols)
                    .map(|i| {
                        table
                            .iter()
                            .filter_map(|r| r.get(i))
                            .map(SourceLine::width)
                            .max()
                            .unwrap_or(0)
                    })
                    .collect();
                for (index, cells) in table.iter().enumerate() {
                    let mut t = SourceLine::styled("│".into(), Style::default(), true);
                    for (i, w) in widths.iter().enumerate() {
                        t.push(Span::raw(" "), None);
                        let width = if let Some(c) = cells.get(i) {
                            t.append(c);
                            c.width()
                        } else {
                            0
                        };
                        t.push(
                            Span::raw(format!("{} │", " ".repeat(w.saturating_sub(width)))),
                            None,
                        );
                    }
                    if index == 0 {
                        for span in &mut t.spans {
                            span.style = span.style.fg(Color::Cyan).add_modifier(Modifier::BOLD);
                        }
                    }
                    lines.push(t);
                    if index == 0 {
                        lines.push(SourceLine::styled(
                            format!(
                                "├{}┤",
                                widths
                                    .iter()
                                    .map(|w| "─".repeat(w + 2))
                                    .collect::<Vec<_>>()
                                    .join("┼")
                            ),
                            Style::default().fg(Color::DarkGray),
                            true,
                        ));
                    }
                }
            }
            Event::Start(Tag::CodeBlock(_)) => {
                if !line.spans.is_empty() {
                    push(&mut line, &mut lines);
                }
                code = true;
                styles.push(Style::default().fg(Color::Rgb(171, 202, 226)));
            }
            Event::End(TagEnd::CodeBlock) => {
                if !line.spans.is_empty() {
                    push(&mut line, &mut lines);
                }
                code = false;
                styles.pop();
            }
            Event::Start(Tag::Heading { .. }) => {
                styles.push(
                    Style::default()
                        .fg(Color::Cyan)
                        .add_modifier(Modifier::BOLD),
                );
            }
            Event::End(TagEnd::Heading(_)) => {
                push(&mut line, &mut lines);
                styles.pop();
            }
            Event::Start(Tag::Strong) => styles.push(
                styles
                    .last()
                    .copied()
                    .unwrap_or_default()
                    .add_modifier(Modifier::BOLD),
            ),
            Event::Start(Tag::Emphasis) => styles.push(
                styles
                    .last()
                    .copied()
                    .unwrap_or_default()
                    .add_modifier(Modifier::ITALIC),
            ),
            Event::Start(Tag::Strikethrough) => styles.push(
                styles
                    .last()
                    .copied()
                    .unwrap_or_default()
                    .add_modifier(Modifier::CROSSED_OUT),
            ),
            Event::End(TagEnd::Strong | TagEnd::Emphasis | TagEnd::Strikethrough) => {
                styles.pop();
            }
            Event::Start(Tag::Link { dest_url, .. }) => {
                links.push(dest_url.to_string());
                styles.push(
                    Style::default()
                        .fg(Color::LightBlue)
                        .add_modifier(Modifier::UNDERLINED),
                );
            }
            Event::End(TagEnd::Link) => {
                links.pop();
                styles.pop();
            }
            Event::Start(Tag::Image { dest_url, .. }) => {
                let url = if dest_url.starts_with("data:") {
                    "embedded image"
                } else {
                    &dest_url
                };
                cell.as_mut().unwrap_or(&mut line).push(
                    Span::styled(
                        format!("[image: {}] ", safe(url)),
                        Style::default().fg(Color::Magenta),
                    ),
                    links.last().map(String::as_str),
                );
            }
            Event::Start(Tag::List(_)) => list_depth += 1,
            Event::End(TagEnd::List(_)) => list_depth = list_depth.saturating_sub(1),
            Event::Start(Tag::Item) => {
                if !line.spans.is_empty() {
                    push(&mut line, &mut lines);
                }
                line.push(
                    Span::raw(format!("{}• ", "  ".repeat(list_depth.saturating_sub(1)))),
                    None,
                );
            }
            Event::End(TagEnd::Item | TagEnd::Paragraph | TagEnd::BlockQuote(_)) => {
                if !line.spans.is_empty() {
                    push(&mut line, &mut lines);
                }
            }
            Event::Start(Tag::BlockQuote(_)) => line.push(
                Span::styled("│ ", Style::default().fg(Color::DarkGray)),
                None,
            ),
            Event::Text(text) | Event::Html(text) | Event::InlineHtml(text) => {
                let text = safe(&text);
                if let Some(cell) = cell.as_mut() {
                    cell.push(
                        Span::styled(text.replace('\n', " "), *styles.last().unwrap()),
                        links.last().map(String::as_str),
                    );
                    continue;
                }
                for (i, s) in text.split('\n').enumerate() {
                    if i > 0 {
                        push(&mut line, &mut lines);
                    }
                    line.nowrap = code;
                    if !s.is_empty() {
                        line.push(
                            Span::styled(s.to_string(), *styles.last().unwrap()),
                            links.last().map(String::as_str),
                        );
                    }
                }
            }
            Event::Code(text) => {
                cell.as_mut().unwrap_or(&mut line).push(
                    Span::styled(
                        safe(&text),
                        styles.last().copied().unwrap_or_default().fg(Color::Yellow),
                    ),
                    links.last().map(String::as_str),
                );
            }
            Event::SoftBreak if cell.is_some() => cell
                .as_mut()
                .unwrap()
                .push(Span::raw(" "), links.last().map(String::as_str)),
            Event::SoftBreak => push(&mut line, &mut lines),
            Event::HardBreak => push(&mut line, &mut lines),
            Event::Rule => {
                push(&mut line, &mut lines);
                lines.push(SourceLine::styled(
                    "────────────────".into(),
                    Style::default().fg(Color::DarkGray),
                    false,
                ));
            }
            Event::TaskListMarker(done) => {
                line.push(Span::raw(if done { "[✓] " } else { "[ ] " }), None)
            }
            _ => (),
        }
    }
    if !line.spans.is_empty() {
        lines.push(line);
    }
    lines
}

pub fn prepare(job: Job) -> Document {
    let width = job.width.max(1);
    let source = job.card.body(job.expanded);
    let lines = markdown(&source);
    let mut rows = vec![];
    let mut plain = String::new();
    let mut links = vec![];
    for line in lines {
        let base = plain.len();
        let mut spans = vec![];
        let mut cells = 0;
        let mut start = plain.len();
        for span in line.spans {
            let style = span.style;
            let mut piece = String::new();
            for g in span.content.graphemes(true) {
                let n = g.width();
                if (!line.nowrap || job.card.kind == Kind::Tool) && cells > 0 && cells + n > width {
                    if !piece.is_empty() {
                        spans.push(Span::styled(std::mem::take(&mut piece), style));
                    }
                    rows.push(Row {
                        line: Line::from(std::mem::take(&mut spans)),
                        start,
                        end: plain.len(),
                    });
                    start = plain.len();
                    cells = 0;
                }
                piece.push_str(g);
                plain.push_str(g);
                cells += n;
            }
            if !piece.is_empty() {
                spans.push(Span::styled(piece, style));
            }
        }
        rows.push(Row {
            line: Line::from(spans),
            start,
            end: plain.len(),
        });
        links.extend(line.links.into_iter().map(|link| Link {
            start: base + link.start,
            end: base + link.end,
            url: link.url,
        }));
        plain.push('\n');
    }
    Document {
        id: job.card.id.clone(),
        revision: job.card.revision,
        width,
        expanded: job.expanded,
        rows,
        plain,
        links,
    }
}

pub fn spawn() -> (Sender<Job>, Receiver<Document>) {
    let (tx, jobs) = mpsc::channel::<Job>();
    let (done, rx) = mpsc::channel();
    thread::spawn(move || {
        while let Ok(job) = jobs.recv() {
            if done.send(prepare(job)).is_err() {
                break;
            }
        }
    });
    (tx, rx)
}

/// Clip terminal columns without splitting UTF-8 or wide graphemes.
pub fn clip(line: &Line<'static>, left: usize, width: usize) -> Line<'static> {
    let mut spans = vec![];
    let mut x = 0;
    for s in &line.spans {
        let mut text = String::new();
        for g in s.content.graphemes(true) {
            let n = g.width();
            if x >= left && x + n <= left + width {
                text.push_str(g);
            } else if x < left && x + n > left {
                text.push(' ');
            }
            x += n;
            if x >= left + width {
                break;
            }
        }
        if !text.is_empty() {
            spans.push(Span::styled(text, s.style));
        }
        if x >= left + width {
            break;
        }
    }
    Line::from(spans)
}
