//! Docking, tab ownership and input routing around persistent, per-file reading views.
use crate::{
    document, hyperlinks,
    model::Change,
    ui::{self, App},
};
use crossterm::event::{
    Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    buffer::Buffer,
    layout::{Direction, Rect},
    style::{Color, Modifier, Style},
    text::Line,
    widgets::{Block, Borders, Clear, Paragraph},
};
use ratatui_hypertile::{Hypertile, HypertileAction, PaneId, SplitSnapshot, Towards, raw::Node};
use std::{collections::HashMap, path::PathBuf};
use unicode_width::UnicodeWidthStr;

pub type FileId = usize;

#[derive(Default)]
struct Group {
    tabs: Vec<FileId>,
    active: Option<FileId>,
    selecting: bool,
    query: String,
    choice: usize,
    first_tab: usize,
    shown_active: Option<FileId>,
    tab_width: u16,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Side {
    Left,
    Right,
    Up,
    Down,
}
impl Side {
    fn axis(self) -> Direction {
        match self {
            Self::Left | Self::Right => Direction::Horizontal,
            _ => Direction::Vertical,
        }
    }
    fn towards(self) -> Towards {
        match self {
            Self::Left | Self::Up => Towards::Start,
            _ => Towards::End,
        }
    }
    fn from_key(key: KeyCode) -> Option<Self> {
        match key {
            KeyCode::Left | KeyCode::Char('h' | 'H') => Some(Self::Left),
            KeyCode::Right | KeyCode::Char('l' | 'L') => Some(Self::Right),
            KeyCode::Up | KeyCode::Char('k' | 'K') => Some(Self::Up),
            KeyCode::Down | KeyCode::Char('j' | 'J') => Some(Self::Down),
            _ => None,
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DropZone {
    Tab(usize),
    Center,
    Edge(Side),
}
#[derive(Clone, Copy, Debug)]
struct DropTarget {
    pane: PaneId,
    zone: DropZone,
    rect: Rect,
}
enum Capture {
    Tab {
        file: FileId,
        source: PaneId,
        origin: (u16, u16),
        target: Option<DropTarget>,
        moved: bool,
    },
    View(FileId),
    Divider(SplitSnapshot),
}
struct TabHit {
    pane: PaneId,
    file: FileId,
    index: usize,
    rect: Rect,
}
enum Overlay {
    Commands {
        input: String,
        index: usize,
        file: Option<FileId>,
        pane: PaneId,
    },
    Help {
        scroll: u16,
    },
}

const WORKSPACE_COMMANDS: &[(&str, &str)] = &[
    ("tab-left", "Reorder current tab to the left"),
    ("tab-right", "Reorder current tab to the right"),
    ("merge-left", "Merge this tab group into the left pane"),
    ("merge-right", "Merge this tab group into the right pane"),
    ("merge-up", "Merge this tab group into the upper pane"),
    ("merge-down", "Merge this tab group into the lower pane"),
    ("split-left", "Split and select a file on the left"),
    ("split-up", "Split and select a file above"),
    ("focus-left", "Ctrl-W h · Focus left pane"),
    ("focus-down", "Ctrl-W j · Focus lower pane"),
    ("focus-up", "Ctrl-W k · Focus upper pane"),
    ("focus-right", "Ctrl-W l · Focus right pane"),
    ("next-tab", "Ctrl-W n · Next tab"),
    ("previous-tab", "Ctrl-W p · Previous tab"),
    ("split-right", "Ctrl-W v · Split left / right"),
    ("split-down", "Ctrl-W s · Split top / bottom"),
    ("move-left", "Ctrl-W H · Move tab left"),
    ("move-down", "Ctrl-W J · Move tab down"),
    ("move-up", "Ctrl-W K · Move tab up"),
    ("move-right", "Ctrl-W L · Move tab right"),
    ("close-tab", "Ctrl-W c · Hide current tab"),
    ("maximize", "Ctrl-W z · Maximize / restore pane"),
    ("open", "Ctrl-W o · Open a monitored file here"),
    ("resize", "Ctrl-W r · Resize with arrows; Enter / Esc ends"),
    ("resize-left", "Move vertical divider left"),
    ("resize-right", "Move vertical divider right"),
    ("resize-up", "Move horizontal divider up"),
    ("resize-down", "Move horizontal divider down"),
];
const WORKSPACE_HELP: &str = "Workspace · one observer; agents keep running\nCtrl-W, then (Esc cancels):\nh/j/k/l or arrows    Focus a pane\nn/p                 Next / previous tab\nv/s                 Split left-right / top-bottom\nH/J/K/L or Shift+arrow  Move tab; split if needed\nc                   Close tab (keep monitoring)\nz                   Maximize / restore\no                   Open a monitored file here\nr                   Resize; arrows, Enter/Esc to finish\n\nDrag a tab onto a tab bar to move or reorder it.\nDrop in a pane center to merge the source tab group.\nDrop at an edge to split. Esc cancels the preview.\nDrag dividers to resize. Click to focus; wheel follows pointer.\nEach file has one view, including hidden tabs.\nNew files open in background. Layout is not saved.\nSmall terminals temporarily show only the focused pane.\n\nFile · shortcuts below affect the current file only.\nPalette File commands keep the file selected at opening.\nTheme (t), help, and quit (q/Ctrl-C) affect the workspace.\nq remains text in search, session filter and palette.\n";

pub struct Workspace {
    pub views: Vec<App>,
    pub issues: Vec<String>,
    tile: Hypertile,
    groups: HashMap<PaneId, Group>,
    area: Rect,
    visible: Vec<(PaneId, Rect)>,
    tab_hits: Vec<TabHit>,
    cached: Buffer,
    cached_views: HashMap<PaneId, (FileId, Rect)>,
    capture: Option<Capture>,
    overlay: Option<Overlay>,
    overlay_rect: Rect,
    prefix: bool,
    resizing: bool,
    zoomed: bool,
    small: bool,
    theme: usize,
    pub dirty: bool,
    pub quit: bool,
}
impl Default for Workspace {
    fn default() -> Self {
        Self::new()
    }
}
impl Workspace {
    pub fn new() -> Self {
        Self {
            views: vec![],
            issues: vec![],
            tile: Hypertile::new(),
            groups: HashMap::from([(PaneId::ROOT, Group::default())]),
            area: Rect::default(),
            visible: vec![],
            tab_hits: vec![],
            cached: Buffer::empty(Rect::default()),
            cached_views: HashMap::new(),
            capture: None,
            overlay: None,
            overlay_rect: Rect::default(),
            prefix: false,
            resizing: false,
            zoomed: false,
            small: false,
            theme: 0,
            dirty: true,
            quit: false,
        }
    }
    pub fn add_file(&mut self, path: PathBuf) -> FileId {
        self.discover_file(path, true)
    }
    pub fn discover_file(&mut self, path: PathBuf, initial: bool) -> FileId {
        let id = self.views.len();
        let mut view = App::new(path.display().to_string());
        view.theme = self.theme;
        self.views.push(view);
        let group = self.groups.get_mut(&self.focused()).unwrap();
        group.tabs.push(id);
        if id == 0 && initial {
            group.active = Some(id);
        }
        self.dirty = true;
        id
    }
    pub fn apply(&mut self, id: FileId, change: Change) {
        self.views[id].apply(change);
        if self
            .visible
            .iter()
            .any(|(pane, _)| self.groups[pane].active == Some(id) || self.is_selecting(*pane))
        {
            self.dirty = true;
        }
    }
    pub fn event_counts(&self) -> Vec<usize> {
        self.views.iter().map(|v| v.summary.events).collect()
    }
    pub fn visible_files(&self) -> Vec<FileId> {
        self.visible
            .iter()
            .filter(|(pane, _)| !self.is_selecting(*pane))
            .filter_map(|(pane, _)| self.groups[pane].active)
            .collect()
    }
    fn focused(&self) -> PaneId {
        self.tile.focused_pane().unwrap()
    }
    fn active(&self) -> Option<FileId> {
        self.groups[&self.focused()].active
    }
    fn is_selecting(&self, pane: PaneId) -> bool {
        self.groups[&pane].selecting || self.groups[&pane].active.is_none()
    }
    fn focus(&mut self, pane: PaneId) {
        let _ = self.tile.focus_pane(pane);
        self.dirty = true;
    }
    fn background(&self) -> Color {
        [Color::Rgb(20, 23, 29), Color::Rgb(28, 27, 37), Color::Black][self.theme]
    }
    fn compute(&mut self) {
        self.tile.compute_layout(self.area);
        let panes: Vec<_> = self
            .tile
            .panes_iter()
            .map(|p| {
                let left = u16::from(p.rect.x > self.area.x);
                let top = u16::from(p.rect.y > self.area.y);
                (
                    p.id,
                    Rect::new(
                        p.rect.x + left,
                        p.rect.y + top,
                        p.rect.width.saturating_sub(left),
                        p.rect.height.saturating_sub(top),
                    ),
                )
            })
            .collect();
        self.small = panes
            .iter()
            .any(|(_, rect)| rect.width < 32 || rect.height < 8);
        self.visible = if self.zoomed || self.small {
            vec![(self.focused(), self.area)]
        } else {
            panes
        };
    }
    pub fn poll_layout(&mut self) {
        for (pane, _) in &self.visible {
            if !self.is_selecting(*pane)
                && let Some(id) = self.groups[pane].active
            {
                self.views[id].poll_layout();
                self.dirty |= self.views[id].dirty;
            }
        }
    }
    fn split(&mut self, pane: PaneId, side: Side) -> PaneId {
        self.focus(pane);
        let id = self.tile.split_focused(side.axis()).unwrap();
        self.groups.insert(id, Group::default());
        if side.towards() == Towards::Start {
            let _ = self.tile.swap_panes(id, pane);
        }
        self.zoomed = false;
        self.compute();
        id
    }
    fn remove_empty(&mut self, pane: PaneId) {
        if self.groups[&pane].tabs.is_empty() && self.groups.len() > 1 {
            let focused = self.focused();
            self.focus(pane);
            if self.tile.close_focused().is_ok() {
                self.groups.remove(&pane);
            }
            if self.groups.contains_key(&focused) {
                self.focus(focused);
            }
        }
    }
    fn detach(&mut self, file: FileId) -> Option<PaneId> {
        let pane = self
            .groups
            .iter()
            .find(|(_, group)| group.tabs.contains(&file))
            .map(|(id, _)| *id)?;
        let group = self.groups.get_mut(&pane).unwrap();
        let index = group.tabs.iter().position(|id| *id == file).unwrap();
        group.tabs.remove(index);
        if group.active == Some(file) {
            group.active = group
                .tabs
                .get(index.min(group.tabs.len().saturating_sub(1)))
                .copied();
        }
        Some(pane)
    }
    fn open_here(&mut self, pane: PaneId, file: FileId, index: Option<usize>) {
        self.transfer(pane, file, index, false);
    }
    fn transfer(&mut self, pane: PaneId, file: FileId, index: Option<usize>, keep_empty: bool) {
        let source = self.detach(file);
        let group = self.groups.get_mut(&pane).unwrap();
        group.tabs.insert(
            index.unwrap_or(group.tabs.len()).min(group.tabs.len()),
            file,
        );
        group.active = Some(file);
        group.selecting = false;
        group.query.clear();
        self.focus(pane);
        if !keep_empty && let Some(source) = source.filter(|s| *s != pane) {
            self.remove_empty(source);
        }
        self.compute();
    }
    pub fn show_file(&mut self, file: FileId) {
        self.open_here(self.focused(), file, None);
    }
    fn cycle(&mut self, forward: bool) {
        let pane = self.focused();
        let group = self.groups.get_mut(&pane).unwrap();
        if group.tabs.is_empty() {
            return;
        }
        let current = group
            .active
            .and_then(|id| group.tabs.iter().position(|f| *f == id))
            .unwrap_or(0);
        let n = group.tabs.len();
        group.active = Some(group.tabs[(current + if forward { 1 } else { n - 1 }) % n]);
        group.selecting = false;
    }
    fn direction(&mut self, side: Side, moving: bool) {
        self.tile.compute_layout(self.area);
        let source = self.focused();
        let file = self.active();
        self.tile.apply_action(HypertileAction::FocusDirection {
            direction: side.axis(),
            towards: side.towards(),
        });
        if moving && let Some(file) = file {
            let mut target = self.focused();
            let create = target == source;
            if create {
                target = self.split(source, side);
            }
            self.transfer(target, file, None, create);
        }
        self.compute();
    }
    fn resize(&mut self, side: Side) {
        let mut path = self.tile.pane_path(self.focused()).unwrap();
        while path.pop().is_some() {
            let mut node = self.tile.root();
            for branch in &path {
                if let Node::Split { first, second, .. } = node {
                    node = if *branch == 0 { first } else { second };
                }
            }
            if let Node::Split {
                direction, ratio, ..
            } = node
                && *direction == side.axis()
            {
                let ratio = ratio
                    + if side.towards() == Towards::Start {
                        -0.05
                    } else {
                        0.05
                    };
                let _ = self.tile.set_split_ratio(&path, ratio);
                self.compute();
                break;
            }
        }
    }
    /// File commands bind to a file id captured when the palette was opened.
    pub fn command(&mut self, command: &str, file: Option<FileId>) {
        self.dirty = true;
        match command {
            "tab-left" | "tab-right" => {
                let pane = self.focused();
                let g = self.groups.get_mut(&pane).unwrap();
                if let Some(index) = g.active.and_then(|id| g.tabs.iter().position(|f| *f == id)) {
                    let next = if command == "tab-left" {
                        index.saturating_sub(1)
                    } else {
                        (index + 1).min(g.tabs.len() - 1)
                    };
                    g.tabs.swap(index, next);
                    g.shown_active = None;
                }
            }
            "split-left" => {
                self.split(self.focused(), Side::Left);
            }
            "split-up" => {
                self.split(self.focused(), Side::Up);
            }
            "next-tab" => self.cycle(true),
            "previous-tab" => self.cycle(false),
            "split-right" => {
                self.split(self.focused(), Side::Right);
            }
            "split-down" => {
                self.split(self.focused(), Side::Down);
            }
            "close-tab" => {
                if let Some(id) = self.active() {
                    let pane = self.detach(id).unwrap();
                    self.remove_empty(pane);
                }
                self.compute();
            }
            "maximize" => {
                self.zoomed = !self.zoomed;
                self.compute();
            }
            "open" => {
                let pane = self.focused();
                let g = self.groups.get_mut(&pane).unwrap();
                g.selecting = true;
                g.query.clear();
                g.choice = 0;
            }
            "resize" => self.resizing = true,
            "theme" => {
                self.theme = (self.theme + 1) % 3;
                for view in &mut self.views {
                    view.theme = self.theme;
                    view.dirty = true;
                }
            }
            "quit" => self.quit = true,
            "help" => self.overlay = Some(Overlay::Help { scroll: 0 }),
            name if name.starts_with("focus-")
                || name.starts_with("move-")
                || name.starts_with("resize-")
                || name.starts_with("merge-") =>
            {
                let side = match name.rsplit('-').next().unwrap() {
                    "left" => Side::Left,
                    "right" => Side::Right,
                    "up" => Side::Up,
                    "down" => Side::Down,
                    _ => return,
                };
                if name.starts_with("resize-") {
                    self.resize(side);
                } else if name.starts_with("merge-") {
                    let source = self.focused();
                    let file = self.active();
                    self.direction(side, false);
                    let target = self.focused();
                    if let Some(file) = file
                        && target != source
                    {
                        self.dock(
                            source,
                            file,
                            DropTarget {
                                pane: target,
                                zone: DropZone::Center,
                                rect: Rect::default(),
                            },
                        );
                    }
                } else {
                    self.direction(side, name.starts_with("move-"));
                }
            }
            name => {
                if let Some(id) = file {
                    self.views[id].command(name);
                    if self.views[id].has_panel() {
                        let owner = self
                            .groups
                            .iter()
                            .find(|(_, g)| g.tabs.contains(&id))
                            .map(|(p, _)| *p);
                        if let Some(pane) = owner {
                            self.focus(pane);
                            let group = self.groups.get_mut(&pane).unwrap();
                            group.active = Some(id);
                            group.selecting = false;
                        } else {
                            self.open_here(self.focused(), id, None);
                        }
                    }
                }
            }
        }
    }
    fn palette(&mut self) {
        self.overlay = Some(Overlay::Commands {
            input: String::new(),
            index: 0,
            file: self.active(),
            pane: self.focused(),
        });
    }
    fn commands(input: &str) -> Vec<(&'static str, &'static str, &'static str)> {
        let query = input.to_lowercase();
        let mut commands: Vec<_> = ui::COMMANDS
            .iter()
            .map(|(name, label)| {
                (
                    *name,
                    *label,
                    if matches!(*name, "theme" | "help" | "quit") {
                        "Workspace"
                    } else {
                        "File"
                    },
                )
            })
            .chain(
                WORKSPACE_COMMANDS
                    .iter()
                    .map(|(name, label)| (*name, *label, "Workspace")),
            )
            .filter(|(name, label, scope)| {
                format!("{scope} {name} {label}")
                    .to_lowercase()
                    .contains(&query)
            })
            .collect();
        commands.sort_by_key(|(name, _, _)| *name != query);
        commands
    }
    fn sessions(&self, pane: PaneId) -> Vec<FileId> {
        let query = self.groups[&pane].query.to_lowercase();
        self.views
            .iter()
            .enumerate()
            .filter(|(_, v)| {
                format!("{} {}", v.path, v.summary.title)
                    .to_lowercase()
                    .contains(&query)
            })
            .map(|(i, _)| i)
            .collect()
    }
    fn prefix_key(&mut self, key: KeyEvent) {
        self.prefix = false;
        if let Some(side) = Side::from_key(key.code) {
            let moving = key.modifiers.contains(KeyModifiers::SHIFT)
                || matches!(key.code, KeyCode::Char('H' | 'J' | 'K' | 'L'));
            self.direction(side, moving);
            return;
        }
        let name = match key.code {
            KeyCode::Char('n') => "next-tab",
            KeyCode::Char('p') => "previous-tab",
            KeyCode::Char('v') => "split-right",
            KeyCode::Char('s') => "split-down",
            KeyCode::Char('c') => "close-tab",
            KeyCode::Char('z') => "maximize",
            KeyCode::Char('o') => "open",
            KeyCode::Char('r') => "resize",
            KeyCode::Char('q') => "quit",
            _ => return,
        };
        self.command(name, self.active());
    }
    pub fn handle(&mut self, event: Event) {
        self.dirty = true;
        match event {
            Event::Key(key) if key.kind != KeyEventKind::Release => {
                if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
                    self.quit = true;
                    return;
                }
                if self.capture.is_some() {
                    if key.code == KeyCode::Esc {
                        self.cancel_capture();
                    }
                    return;
                }
                if self.prefix {
                    self.prefix_key(key);
                    return;
                }
                if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('w') {
                    self.prefix = true;
                    return;
                }
                if self.resizing {
                    if key.code == KeyCode::Char('q') {
                        self.quit = true;
                    } else if matches!(key.code, KeyCode::Esc | KeyCode::Enter) {
                        self.resizing = false;
                    } else if let Some(side) = Side::from_key(key.code) {
                        self.resize(side);
                    }
                    return;
                }
                if self.overlay.is_some() {
                    self.overlay_key(key);
                    return;
                }
                let pane = self.focused();
                let id = self.active();
                let input =
                    self.is_selecting(pane) || id.is_some_and(|id| self.views[id].input_active());
                if key.code == KeyCode::Char('q') && !input {
                    self.quit = true;
                    return;
                }
                if (matches!(key.code, KeyCode::Char(':')) && !input)
                    || (key.code == KeyCode::Char('p')
                        && key.modifiers.contains(KeyModifiers::CONTROL))
                    || (key.code == KeyCode::Char('m') && !input)
                {
                    self.palette();
                    return;
                }
                if matches!(key.code, KeyCode::F(1)) || (key.code == KeyCode::Char('?') && !input) {
                    self.command("help", id);
                    return;
                }
                if key.code == KeyCode::Char('t') && !input {
                    self.command("theme", id);
                    return;
                }
                if self.is_selecting(pane) {
                    self.selector_key(pane, key);
                } else if let Some(id) = id {
                    self.views[id].handle(Event::Key(key));
                    self.quit |= self.views[id].quit;
                }
            }
            Event::Mouse(mouse) => self.mouse(mouse),
            Event::Resize(_, _) => self.cancel_capture(),
            _ => (),
        }
    }
    fn cancel_capture(&mut self) {
        if let Some(Capture::View(id)) = self.capture.take() {
            self.views[id].handle(Event::Mouse(MouseEvent {
                kind: MouseEventKind::Up(MouseButton::Left),
                column: 0,
                row: 0,
                modifiers: KeyModifiers::NONE,
            }));
        }
    }
    fn selector_key(&mut self, pane: PaneId, key: KeyEvent) {
        let sessions = self.sessions(pane);
        let g = self.groups.get_mut(&pane).unwrap();
        match key.code {
            KeyCode::Esc => {
                g.selecting = false;
                g.query.clear();
            }
            KeyCode::Up => g.choice = g.choice.saturating_sub(1),
            KeyCode::Down => g.choice = (g.choice + 1).min(sessions.len().saturating_sub(1)),
            KeyCode::Enter => {
                if let Some(id) = sessions.get(g.choice).copied() {
                    self.open_here(pane, id, None);
                }
            }
            KeyCode::Backspace => {
                g.query.pop();
                g.choice = 0;
            }
            KeyCode::Char(c) => {
                g.query.push(c);
                g.choice = 0;
            }
            _ => (),
        }
    }
    fn overlay_key(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Esc {
            self.overlay = None;
            return;
        }
        match self.overlay.as_mut().unwrap() {
            Overlay::Help { scroll } => match key.code {
                KeyCode::Char('q') => self.quit = true,
                KeyCode::Down | KeyCode::Char('j') => *scroll = scroll.saturating_add(1),
                KeyCode::Up | KeyCode::Char('k') => *scroll = scroll.saturating_sub(1),
                KeyCode::PageDown => *scroll = scroll.saturating_add(10),
                KeyCode::PageUp => *scroll = scroll.saturating_sub(10),
                _ => (),
            },
            Overlay::Commands {
                input,
                index,
                file,
                pane,
            } => match key.code {
                KeyCode::Up => *index = index.saturating_sub(1),
                KeyCode::Down => {
                    *index = (*index + 1).min(Self::commands(input).len().saturating_sub(1))
                }
                KeyCode::Backspace => {
                    input.pop();
                    *index = 0;
                }
                KeyCode::Char(c) => {
                    input.push(c);
                    *index = 0;
                }
                KeyCode::Enter => {
                    if let Some((name, _, scope)) = Self::commands(input).get(*index).copied() {
                        let file = *file;
                        let pane = *pane;
                        self.overlay = None;
                        if scope == "Workspace" && self.groups.contains_key(&pane) {
                            self.focus(pane);
                        }
                        self.command(name, file);
                    }
                }
                _ => (),
            },
        }
    }
    fn pane_at(&self, x: u16, y: u16) -> Option<PaneId> {
        self.visible
            .iter()
            .find(|(_, rect)| rect.contains((x, y).into()))
            .map(|(pane, _)| *pane)
    }
    fn drop_target(&self, x: u16, y: u16) -> Option<DropTarget> {
        let pane = self.pane_at(x, y)?;
        let rect = self.visible.iter().find(|(p, _)| *p == pane)?.1;
        if y == rect.y {
            let index = self
                .tab_hits
                .iter()
                .find(|h| h.pane == pane && h.rect.right() > x)
                .map_or(self.groups[&pane].tabs.len(), |h| h.index);
            return Some(DropTarget {
                pane,
                zone: DropZone::Tab(index),
                rect: Rect::new(x.min(rect.right().saturating_sub(1)), y, 1, 1),
            });
        }
        let left = x - rect.x;
        let top = y - rect.y;
        let side = if left < (rect.width / 4).min(12) {
            Some(Side::Left)
        } else if rect.right() - x <= (rect.width / 4).min(12) {
            Some(Side::Right)
        } else if top < (rect.height / 4).min(4) {
            Some(Side::Up)
        } else if rect.bottom() - y <= (rect.height / 4).min(4) {
            Some(Side::Down)
        } else {
            None
        };
        let mut preview = rect;
        if let Some(side) = side {
            match side {
                Side::Left => preview.width /= 2,
                Side::Right => {
                    preview.x += preview.width / 2;
                    preview.width -= preview.width / 2;
                }
                Side::Up => preview.height /= 2,
                Side::Down => {
                    preview.y += preview.height / 2;
                    preview.height -= preview.height / 2;
                }
            }
        }
        Some(DropTarget {
            pane,
            zone: side.map_or(DropZone::Center, DropZone::Edge),
            rect: preview,
        })
    }
    fn dock(&mut self, source: PaneId, file: FileId, target: DropTarget) {
        match target.zone {
            DropZone::Tab(mut index) => {
                if source == target.pane
                    && let Some(old) = self.groups[&source].tabs.iter().position(|id| *id == file)
                    && old < index
                {
                    index -= 1;
                }
                self.open_here(target.pane, file, Some(index));
            }
            DropZone::Center if source != target.pane => {
                let tabs = self.groups[&source].tabs.clone();
                for id in tabs {
                    self.open_here(target.pane, id, None);
                }
                self.groups.get_mut(&target.pane).unwrap().active = Some(file);
            }
            DropZone::Edge(side) => {
                let pane = self.split(target.pane, side);
                self.transfer(pane, file, None, source == target.pane);
            }
            _ => (),
        }
    }
    fn mouse(&mut self, mouse: MouseEvent) {
        if let Some(mut capture) = self.capture.take() {
            match &mut capture {
                Capture::View(id) => self.views[*id].handle_at(Event::Mouse(mouse)),
                Capture::Divider(split) => {
                    if matches!(mouse.kind, MouseEventKind::Drag(MouseButton::Left)) {
                        let position = if split.direction == Direction::Horizontal {
                            mouse.column.saturating_sub(split.rect.x)
                        } else {
                            mouse.row.saturating_sub(split.rect.y)
                        };
                        let size = if split.direction == Direction::Horizontal {
                            split.rect.width
                        } else {
                            split.rect.height
                        };
                        let _ = self
                            .tile
                            .set_split_ratio(&split.path, position as f32 / size.max(1) as f32);
                        self.compute();
                    }
                }
                Capture::Tab {
                    source,
                    file,
                    origin,
                    target,
                    moved,
                } => {
                    if matches!(mouse.kind, MouseEventKind::Drag(MouseButton::Left)) {
                        *moved |= (mouse.column, mouse.row) != *origin;
                        *target = self.drop_target(mouse.column, mouse.row);
                    }
                    if mouse.kind == MouseEventKind::Up(MouseButton::Left)
                        && *moved
                        && let Some(target) = *target
                    {
                        self.dock(*source, *file, target);
                    }
                }
            }
            if mouse.kind != MouseEventKind::Up(MouseButton::Left) {
                self.capture = Some(capture);
            }
            return;
        }
        if self.overlay.is_some() {
            if self.overlay_rect.contains((mouse.column, mouse.row).into()) {
                let key = match mouse.kind {
                    MouseEventKind::ScrollDown => Some(KeyCode::Down),
                    MouseEventKind::ScrollUp => Some(KeyCode::Up),
                    _ => None,
                };
                if let Some(key) = key {
                    self.overlay_key(KeyEvent::new(key, KeyModifiers::NONE));
                }
                if mouse.kind == MouseEventKind::Down(MouseButton::Left)
                    && let Some(Overlay::Commands { input, index, .. }) = &mut self.overlay
                {
                    let height = self.overlay_rect.height.saturating_sub(4) as usize;
                    let first = index.saturating_sub(height.saturating_sub(1));
                    if mouse.row >= self.overlay_rect.y + 3
                        && mouse.row < self.overlay_rect.bottom().saturating_sub(1)
                    {
                        let chosen = first + (mouse.row - self.overlay_rect.y - 3) as usize;
                        if chosen < Self::commands(input).len() {
                            *index = chosen;
                            self.overlay_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                        }
                    }
                }
            } else if mouse.kind == MouseEventKind::Down(MouseButton::Left)
                && let Some(pane) = self.pane_at(mouse.column, mouse.row)
            {
                self.focus(pane);
            }
            return;
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left)
            && !self.zoomed
            && !self.small
            && let Some(split) = self.tile.split_at(mouse.column, mouse.row, 0)
        {
            self.capture = Some(Capture::Divider(split));
            return;
        }
        let Some(pane) = self.pane_at(mouse.column, mouse.row) else {
            return;
        };
        let rect = self.visible.iter().find(|(id, _)| *id == pane).unwrap().1;
        if matches!(mouse.kind, MouseEventKind::Down(_)) {
            self.focus(pane);
        }
        if mouse.row == rect.y {
            if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                if let Some(hit) = self.tab_hits.iter().find(|h| {
                    h.pane == pane
                        && self.groups[&pane].tabs.contains(&h.file)
                        && h.rect.contains((mouse.column, mouse.row).into())
                }) {
                    let file = hit.file;
                    let g = self.groups.get_mut(&pane).unwrap();
                    if g.active == Some(file) && mouse.column == hit.rect.x {
                        g.selecting = !g.selecting;
                    } else {
                        g.active = Some(file);
                        g.selecting = false;
                        self.capture = Some(Capture::Tab {
                            file,
                            source: pane,
                            origin: (mouse.column, mouse.row),
                            target: None,
                            moved: false,
                        });
                    }
                }
            } else if matches!(
                mouse.kind,
                MouseEventKind::ScrollUp | MouseEventKind::ScrollDown
            ) {
                let g = self.groups.get_mut(&pane).unwrap();
                g.first_tab = if mouse.kind == MouseEventKind::ScrollDown {
                    (g.first_tab + 1).min(g.tabs.len().saturating_sub(1))
                } else {
                    g.first_tab.saturating_sub(1)
                };
            }
            return;
        }
        if self.is_selecting(pane) {
            let sessions = self.sessions(pane);
            let g = self.groups.get_mut(&pane).unwrap();
            match mouse.kind {
                MouseEventKind::ScrollDown => {
                    g.choice = (g.choice + 3).min(sessions.len().saturating_sub(1))
                }
                MouseEventKind::ScrollUp => g.choice = g.choice.saturating_sub(3),
                MouseEventKind::Down(MouseButton::Left) => {
                    let height = rect.height.saturating_sub(5) as usize;
                    let first = g.choice.saturating_sub(height.saturating_sub(1));
                    if mouse.row >= rect.y + 4 && mouse.row < rect.bottom().saturating_sub(1) {
                        let index = first + (mouse.row - rect.y - 4) as usize;
                        if let Some(id) = sessions.get(index).copied() {
                            self.open_here(pane, id, None);
                        }
                    }
                }
                _ => (),
            }
            return;
        }
        if let Some(id) = self.groups[&pane].active {
            self.views[id].handle_at(Event::Mouse(mouse));
            if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                self.capture = Some(Capture::View(id));
            }
        }
    }
    fn render_tabs(&mut self, frame: &mut Frame, pane: PaneId, rect: Rect) {
        if rect.is_empty() {
            return;
        }
        let focused = pane == self.focused();
        let g = self.groups.get_mut(&pane).unwrap();
        let max_label = 28.min(rect.width.saturating_sub(3) as usize).max(1);
        let labels: Vec<_> = g
            .tabs
            .iter()
            .map(|id| {
                let path = std::path::Path::new(&self.views[*id].path);
                let name = path.file_name().unwrap_or_default().to_string_lossy();
                // Run-owned files often share a basename; include their parent directory.
                let label = path.parent().and_then(|p| p.file_name()).map_or_else(
                    || name.to_string(),
                    |p| format!("{}/{}", p.to_string_lossy(), name),
                );
                document::clip(
                    &Line::raw(document::safe(&label).replace('\n', " ")),
                    0,
                    max_label,
                )
                .to_string()
            })
            .collect();
        if (g.shown_active != g.active || g.tab_width != rect.width)
            && let Some(index) = g.active.and_then(|id| g.tabs.iter().position(|f| *f == id))
        {
            if index < g.first_tab {
                g.first_tab = index;
            }
            while g.first_tab < index
                && labels[g.first_tab..=index]
                    .iter()
                    .map(|l| l.width() + 3)
                    .sum::<usize>()
                    > rect.width as usize
            {
                g.first_tab += 1;
            }
        }
        g.shown_active = g.active;
        g.tab_width = rect.width;
        let style = Style::default().bg(Color::Rgb(38, 44, 55)).fg(Color::Gray);
        frame.render_widget(
            Block::default().style(style),
            Rect::new(rect.x, rect.y, rect.width, 1),
        );
        let mut x = rect.x;
        for (index, label) in labels.iter().enumerate().skip(g.first_tab) {
            if x >= rect.right() {
                break;
            }
            let file = g.tabs[index];
            let active = g.active == Some(file);
            let width = (label.width() as u16 + 3).min(rect.right() - x);
            let hit = Rect::new(x, rect.y, width, 1);
            frame.render_widget(
                Paragraph::new(format!("{} {label} ", if active { "▾" } else { " " })).style(
                    if active {
                        style
                            .fg(if focused { Color::Cyan } else { Color::White })
                            .add_modifier(Modifier::BOLD)
                            .bg(Color::Rgb(48, 58, 72))
                    } else {
                        style
                    },
                ),
                hit,
            );
            self.tab_hits.push(TabHit {
                pane,
                file,
                index,
                rect: hit,
            });
            x += width;
        }
        if g.tabs.is_empty() {
            frame.render_widget(
                Paragraph::new(" Sessions · Ctrl-W o").style(style.fg(Color::Cyan)),
                Rect::new(rect.x, rect.y, rect.width, 1),
            );
        }
    }
    fn render_selector(&self, frame: &mut Frame, pane: PaneId, rect: Rect) {
        if rect.height < 5 || rect.width < 4 {
            return;
        }
        let group = &self.groups[&pane];
        let list = self.sessions(pane);
        let height = rect.height.saturating_sub(5) as usize;
        let first = group.choice.saturating_sub(height.saturating_sub(1));
        let text = if self.views.is_empty() {
            " Waiting for monitored files…".to_string()
        } else {
            " Sessions · Enter opens or moves a file here".to_string()
        };
        frame.render_widget(
            Paragraph::new(text).style(Style::default().fg(Color::Cyan)),
            Rect::new(rect.x, rect.y + 1, rect.width, 1),
        );
        frame.render_widget(
            Paragraph::new(format!(" Filter: {}", document::safe(&group.query)))
                .style(Style::default().fg(Color::White)),
            Rect::new(rect.x, rect.y + 2, rect.width, 1),
        );
        for (index, id) in list.iter().enumerate().skip(first).take(height) {
            let view = &self.views[*id];
            let owner = self
                .groups
                .iter()
                .find(|(_, g)| g.tabs.contains(id))
                .map(|(p, _)| *p);
            let location = match owner {
                Some(p) if p == pane => "here",
                Some(_) => "open",
                None => "hidden",
            };
            let text = format!(
                "{} [{location}] {} · {}",
                if index == group.choice { "›" } else { " " },
                view.path,
                view.summary.status
            );
            frame.render_widget(
                Paragraph::new(document::safe(&text)).style(Style::default().fg(
                    if index == group.choice {
                        Color::Cyan
                    } else {
                        Color::Gray
                    },
                )),
                Rect::new(rect.x, rect.y + 4 + (index - first) as u16, rect.width, 1),
            );
        }
        let footer = self.issues.first().map_or(
            " Type to filter · Esc returns · Ctrl-W workspace",
            String::as_str,
        );
        frame.render_widget(
            Paragraph::new(document::safe(footer)).style(Style::default().fg(Color::DarkGray)),
            Rect::new(rect.x, rect.bottom() - 1, rect.width, 1),
        );
    }
    pub fn render(&mut self, frame: &mut Frame) {
        let screen = frame.area();
        self.area = Rect::new(
            screen.x,
            screen.y,
            screen.width,
            screen.height.saturating_sub(1),
        );
        self.compute();
        self.tab_hits.clear();
        if self.cached.area != screen {
            self.cached_views.clear();
        }
        let mut rendered = HashMap::new();
        frame.render_widget(
            Block::default().style(Style::default().bg(self.background()).fg(Color::Gray)),
            screen,
        );
        if !self.zoomed && !self.small {
            for p in self.tile.panes_iter() {
                if p.rect.x > self.area.x {
                    for y in p.rect.y..p.rect.bottom() {
                        frame.render_widget(
                            Paragraph::new("│").style(Style::default().fg(Color::DarkGray)),
                            Rect::new(p.rect.x, y, 1, 1),
                        );
                    }
                }
                if p.rect.y > self.area.y {
                    frame.render_widget(
                        Paragraph::new("─".repeat(p.rect.width as usize))
                            .style(Style::default().fg(Color::DarkGray)),
                        Rect::new(p.rect.x, p.rect.y, p.rect.width, 1),
                    );
                }
            }
        }
        for (pane, rect) in self.visible.clone() {
            if rect.is_empty() {
                continue;
            }
            self.render_tabs(frame, pane, rect);
            if self.is_selecting(pane) {
                self.render_selector(frame, pane, rect);
            } else if let Some(id) = self.groups[&pane].active {
                let area = Rect::new(
                    rect.x,
                    rect.y + 1,
                    rect.width,
                    rect.height.saturating_sub(1),
                );
                if !self.views[id].dirty && self.cached_views.get(&pane) == Some(&(id, area)) {
                    // Ratatui still needs every cell in its new frame; reuse clean pane cells.
                    let buffer = frame.buffer_mut();
                    for y in area.y..area.bottom() {
                        let start = buffer.index_of(area.x, y);
                        let end = start + area.width as usize;
                        buffer.content[start..end]
                            .clone_from_slice(&self.cached.content[start..end]);
                    }
                } else {
                    self.views[id].render_area(frame, area);
                }
                rendered.insert(pane, (id, area));
            }
        }
        // Cache content before drawing global overlays, so closing them restores clean panes.
        self.cached.clone_from(frame.buffer_mut());
        self.cached_views = rendered;
        let hint = if self.prefix {
            " Ctrl-W: o open · c close · h/j/k/l focus · n/p tabs · v/s split · H/J/K/L move · z zoom · r resize · Esc cancel"
        } else if self.resizing {
            " Resize: arrows move dividers · Enter / Esc ends"
        } else if self.zoomed {
            " Maximized · Ctrl-W z restores · Ctrl-W o open · : commands · ? help"
        } else if self.small && self.groups.len() > 1 {
            " Focused pane only: terminal is small · layout restores on resize · Ctrl-W h/j/k/l focus"
        } else {
            " Ctrl-W workspace · : commands · ? help · q quit observer"
        };
        if screen.height > 0 {
            frame.render_widget(
                Paragraph::new(hint).style(Style::default().fg(if self.prefix || self.resizing {
                    Color::Cyan
                } else {
                    Color::DarkGray
                })),
                Rect::new(screen.x, screen.bottom() - 1, screen.width, 1),
            );
        }
        if let Some(Capture::Tab {
            target: Some(target),
            moved: true,
            ..
        }) = &self.capture
        {
            frame.render_widget(
                Block::default()
                    .borders(Borders::ALL)
                    .title(match target.zone {
                        DropZone::Center => " Merge tabs · Esc cancels ",
                        DropZone::Edge(_) => " Split · Esc cancels ",
                        DropZone::Tab(_) => "│",
                    })
                    .style(Style::default().fg(Color::Cyan)),
                target.rect,
            );
        }
        if self.overlay.is_some() {
            self.render_overlay(frame);
        }
        self.dirty = false;
    }
    fn render_overlay(&mut self, frame: &mut Frame) {
        let a = frame.area();
        let width = a.width.saturating_sub(4).min(112);
        let height = a.height.saturating_sub(2).min(38);
        let rect = Rect::new(
            a.x + (a.width - width) / 2,
            a.y + (a.height - height) / 2,
            width,
            height,
        );
        self.overlay_rect = rect;
        frame.render_widget(Clear, rect);
        let (title, body, scroll) = match self.overlay.as_ref().unwrap() {
            Overlay::Help { scroll } => (
                " File / Workspace shortcuts · Esc closes ".to_string(),
                format!("{WORKSPACE_HELP}\n{}", ui::HELP),
                *scroll,
            ),
            Overlay::Commands {
                input, index, file, ..
            } => {
                let rows = height.saturating_sub(4) as usize;
                let first = index.saturating_sub(rows.saturating_sub(1));
                let commands = Self::commands(input)
                    .into_iter()
                    .enumerate()
                    .skip(first)
                    .take(rows)
                    .map(|(i, (name, label, scope))| {
                        format!(
                            "{} {scope:<9} {name:<15} {label}",
                            if i == *index { "›" } else { " " }
                        )
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                let target = file.map_or("No file", |id| self.views[id].path.as_str());
                (
                    format!(" Commands · File target: {} ", document::safe(target)),
                    format!(
                        ":{input}    {}\n\n{commands}",
                        file.map_or_else(String::new, |id| self.views[id].view_settings())
                    ),
                    0,
                )
            }
        };
        frame.render_widget(
            Paragraph::new(document::safe(&body))
                .block(
                    Block::default()
                        .borders(Borders::ALL)
                        .title(title)
                        .border_style(Style::default().fg(Color::Cyan)),
                )
                .style(Style::default().bg(self.background()).fg(Color::White))
                .scroll((scroll, 0)),
            rect,
        );
    }
    pub fn hyperlinks(&self) -> Vec<hyperlinks::Link> {
        if self.overlay.is_some() || matches!(self.capture, Some(Capture::Tab { moved: true, .. }))
        {
            return vec![];
        }
        self.visible
            .iter()
            .filter(|(pane, _)| !self.is_selecting(*pane))
            .filter_map(|(pane, _)| self.groups[pane].active)
            .flat_map(|id| self.views[id].hyperlinks())
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Event as AgentEvent, Model};
    use ratatui::{Terminal, backend::TestBackend};
    use serde_json::json;

    fn key(ws: &mut Workspace, code: KeyCode) {
        ws.handle(Event::Key(KeyEvent::new(code, KeyModifiers::NONE)));
    }
    fn prefix(ws: &mut Workspace, code: KeyCode) {
        ws.handle(Event::Key(KeyEvent::new(
            KeyCode::Char('w'),
            KeyModifiers::CONTROL,
        )));
        key(ws, code);
    }
    fn mouse(ws: &mut Workspace, kind: MouseEventKind, x: u16, y: u16) {
        ws.handle(Event::Mouse(MouseEvent {
            kind,
            column: x,
            row: y,
            modifiers: KeyModifiers::NONE,
        }));
    }
    fn fixture(n: usize) -> (Workspace, Terminal<TestBackend>) {
        let mut ws = Workspace::new();
        for id in 0..n {
            ws.add_file(format!("/sessions/run-{id}/events.jsonl").into());
            let mut model = Model::new();
            model.apply(AgentEvent { kind: "context/append".into(), data: json!({"items":[
                {"type":"message","role":"user","content":[{"type":"input_text","text":"你好 reader"}]},
                {"type":"reasoning","content":[{"type":"reasoning_text","text":"Inspect source"}]},
                {"type":"message","role":"assistant","content":[{"type":"output_text","text":format!("File {id} · [文档 link](https://example.com/{id})\n\n{}", "Some readable words in a long answer. ".repeat(70))}]}
            ]}), elapsed_ms: None, ts: None, seq: None });
            ws.apply(id, model.change());
            ws.views[id].follow = false;
        }
        let mut terminal = Terminal::new(TestBackend::new(140, 48)).unwrap();
        settle(&mut ws, &mut terminal);
        (ws, terminal)
    }
    fn settle(ws: &mut Workspace, terminal: &mut Terminal<TestBackend>) {
        for _ in 0..30 {
            ws.poll_layout();
            terminal.draw(|f| ws.render(f)).unwrap();
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
    }
    fn row(terminal: &Terminal<TestBackend>, y: u16, left: u16, right: u16) -> String {
        (left..right)
            .map(|x| terminal.backend().buffer()[(x, y)].symbol())
            .collect()
    }
    #[test]
    fn startup_background_tabs_close_reopen_and_scoped_palette() {
        let (mut ws, mut terminal) = fixture(3);
        assert_eq!(ws.groups.len(), 1);
        assert_eq!(ws.active(), Some(0));
        ws.command("stats", Some(0));
        assert_eq!(ws.groups[&PaneId::ROOT].tabs, vec![0, 1, 2]);
        key(&mut ws, KeyCode::Esc);
        key(&mut ws, KeyCode::Char('1'));
        assert_eq!(ws.views[0].notice, "View: Collapsed");
        assert!(ws.views[1].notice.is_empty());
        prefix(&mut ws, KeyCode::Char('n'));
        assert_eq!(ws.active(), Some(1));
        let selected = ws.views[1].focus.clone();
        key(&mut ws, KeyCode::Tab);
        assert_ne!(ws.views[1].focus, selected);
        let focus = ws.views[1].focus.clone();
        prefix(&mut ws, KeyCode::Char('v'));
        assert!(ws.is_selecting(ws.focused()));
        key(&mut ws, KeyCode::Down);
        key(&mut ws, KeyCode::Enter);
        assert_eq!(ws.active(), Some(1));
        assert_eq!(ws.views[1].focus, focus);
        key(&mut ws, KeyCode::Char(':'));
        for c in "expand".chars() {
            key(&mut ws, KeyCode::Char(c));
        }
        prefix(&mut ws, KeyCode::Char('h'));
        assert_eq!(ws.active(), Some(2));
        key(&mut ws, KeyCode::Enter);
        assert_eq!(ws.views[1].notice, "View: Expanded");
        assert!(ws.views[2].notice.is_empty());
        prefix(&mut ws, KeyCode::Char('l'));
        prefix(&mut ws, KeyCode::Char('c'));
        assert!(!ws.groups.values().any(|g| g.tabs.contains(&1)));
        assert_eq!(ws.views[1].notice, "View: Expanded");
        prefix(&mut ws, KeyCode::Char('o'));
        for c in "run-1".chars() {
            key(&mut ws, KeyCode::Char(c));
        }
        key(&mut ws, KeyCode::Enter);
        assert_eq!(ws.active(), Some(1));
        assert_eq!(ws.views[1].notice, "View: Expanded");
        ws.add_file("/sessions/new/events.jsonl".into());
        assert_eq!(ws.active(), Some(1));
        settle(&mut ws, &mut terminal);
        assert!(row(&terminal, 0, 0, 140).contains("run-1"));
    }
    #[test]
    fn q_is_text_in_inputs_and_theme_and_quit_are_global() {
        let (mut ws, _) = fixture(2);
        key(&mut ws, KeyCode::Char('/'));
        key(&mut ws, KeyCode::Char('q'));
        assert!(!ws.quit);
        key(&mut ws, KeyCode::Esc);
        key(&mut ws, KeyCode::Char(':'));
        for c in "quit".chars() {
            key(&mut ws, KeyCode::Char(c));
        }
        assert!(!ws.quit);
        key(&mut ws, KeyCode::Esc);
        key(&mut ws, KeyCode::Char('t'));
        assert!(ws.views.iter().all(|v| v.theme == 1));
        prefix(&mut ws, KeyCode::Char('o'));
        key(&mut ws, KeyCode::Char('q'));
        assert!(!ws.quit);
        key(&mut ws, KeyCode::Esc);
        key(&mut ws, KeyCode::Char('s'));
        key(&mut ws, KeyCode::Char('q'));
        assert!(ws.quit);
    }
    #[test]
    fn dragging_reorders_previews_cancels_splits_and_merges_groups() {
        let (mut ws, mut terminal) = fixture(4);
        let first = ws.tab_hits[0].rect;
        let last = ws.tab_hits[3].rect;
        mouse(
            &mut ws,
            MouseEventKind::Down(MouseButton::Left),
            first.x + 1,
            first.y,
        );
        mouse(
            &mut ws,
            MouseEventKind::Drag(MouseButton::Left),
            last.x,
            last.y,
        );
        assert!(matches!(
            ws.capture,
            Some(Capture::Tab {
                target: Some(_),
                moved: true,
                ..
            })
        ));
        key(&mut ws, KeyCode::Esc);
        assert_eq!(ws.groups[&PaneId::ROOT].tabs, vec![0, 1, 2, 3]);
        mouse(
            &mut ws,
            MouseEventKind::Down(MouseButton::Left),
            first.x + 1,
            first.y,
        );
        mouse(
            &mut ws,
            MouseEventKind::Drag(MouseButton::Left),
            last.x,
            last.y,
        );
        mouse(
            &mut ws,
            MouseEventKind::Up(MouseButton::Left),
            last.x,
            last.y,
        );
        assert_eq!(ws.groups[&PaneId::ROOT].tabs, vec![1, 2, 0, 3]);
        settle(&mut ws, &mut terminal);
        let tab = ws.tab_hits.iter().find(|h| h.file == 0).unwrap().rect;
        mouse(
            &mut ws,
            MouseEventKind::Down(MouseButton::Left),
            tab.x + 1,
            tab.y,
        );
        mouse(&mut ws, MouseEventKind::Drag(MouseButton::Left), 138, 20);
        mouse(&mut ws, MouseEventKind::Up(MouseButton::Left), 138, 20);
        assert_eq!(ws.groups.len(), 2);
        let right = ws.focused();
        assert_eq!(ws.active(), Some(0));
        settle(&mut ws, &mut terminal);
        let tab = ws.tab_hits.iter().find(|h| h.file == 1).unwrap().rect;
        mouse(
            &mut ws,
            MouseEventKind::Down(MouseButton::Left),
            tab.x + 1,
            tab.y,
        );
        mouse(&mut ws, MouseEventKind::Drag(MouseButton::Left), 100, 20);
        mouse(&mut ws, MouseEventKind::Up(MouseButton::Left), 100, 20);
        assert_eq!(ws.groups.len(), 1);
        assert_eq!(ws.focused(), right);
        assert_eq!(ws.groups[&right].tabs, vec![0, 1, 2, 3]);
    }
    #[test]
    fn divider_keyboard_zoom_and_small_terminal_preserve_tree_and_reading_state() {
        let (mut ws, mut terminal) = fixture(3);
        prefix(&mut ws, KeyCode::Char('v'));
        ws.show_file(1);
        settle(&mut ws, &mut terminal);
        let right = ws.focused();
        let before = ws.tile.pane_rect(right).unwrap();
        let divider = before.x;
        mouse(
            &mut ws,
            MouseEventKind::Down(MouseButton::Left),
            divider,
            15,
        );
        assert!(matches!(ws.capture, Some(Capture::Divider(_))));
        mouse(
            &mut ws,
            MouseEventKind::Drag(MouseButton::Left),
            divider + 10,
            15,
        );
        mouse(
            &mut ws,
            MouseEventKind::Up(MouseButton::Left),
            divider + 10,
            15,
        );
        assert!(ws.tile.pane_rect(right).unwrap().width < before.width);
        prefix(&mut ws, KeyCode::Char('r'));
        key(&mut ws, KeyCode::Left);
        key(&mut ws, KeyCode::Enter);
        assert!(!ws.resizing);
        key(&mut ws, KeyCode::PageDown);
        let focus = ws.views[1].focus.clone();
        let ratios = format!("{:?}", ws.tile.root());
        prefix(&mut ws, KeyCode::Char('z'));
        settle(&mut ws, &mut terminal);
        assert_eq!(ws.visible.len(), 1);
        prefix(&mut ws, KeyCode::Char('z'));
        terminal.backend_mut().resize(40, 12);
        terminal.autoresize().unwrap();
        settle(&mut ws, &mut terminal);
        assert_eq!(ws.visible.len(), 1);
        assert!(ws.small);
        terminal.backend_mut().resize(140, 48);
        terminal.autoresize().unwrap();
        settle(&mut ws, &mut terminal);
        assert_eq!(ws.visible.len(), 2);
        assert_eq!(format!("{:?}", ws.tile.root()), ratios);
        assert_eq!(ws.views[1].focus, focus);
        assert!(!ws.views[1].follow);
        assert!(ws.views[1].scroll > 0);
    }
    #[test]
    fn mouse_capture_wheel_selection_and_links_use_the_target_pane_coordinates() {
        let (mut ws, mut terminal) = fixture(2);
        prefix(&mut ws, KeyCode::Char('v'));
        ws.show_file(1);
        settle(&mut ws, &mut terminal);
        let pane = ws.focused();
        let rect = ws.visible.iter().find(|(p, _)| *p == pane).unwrap().1;
        let links = ws.hyperlinks();
        let right_link = links.iter().find(|l| l.url.ends_with("/1")).unwrap();
        assert!(right_link.start >= rect.x + 2);
        assert!(right_link.end <= rect.right());
        assert!(row(&terminal, right_link.y, right_link.start, right_link.end).contains('文'));
        let y = rect.y + 4;
        mouse(
            &mut ws,
            MouseEventKind::Down(MouseButton::Left),
            rect.x + 2,
            y,
        );
        mouse(
            &mut ws,
            MouseEventKind::Drag(MouseButton::Left),
            rect.x + 6,
            y,
        );
        assert_eq!(ws.views[1].selected_text().as_deref(), Some("你好"));
        mouse(&mut ws, MouseEventKind::Drag(MouseButton::Left), 10, y);
        assert!(ws.views[0].selected_text().is_none());
        assert_eq!(ws.focused(), pane);
        mouse(
            &mut ws,
            MouseEventKind::Drag(MouseButton::Left),
            rect.x + 6,
            y,
        );
        mouse(
            &mut ws,
            MouseEventKind::Up(MouseButton::Left),
            rect.x + 6,
            y,
        );
        mouse(&mut ws, MouseEventKind::ScrollDown, 10, y);
        assert!(ws.views[0].scroll > 0);
        assert_eq!(ws.focused(), pane);
        assert_eq!(ws.views[1].selected_text().as_deref(), Some("你好"));
        ws.command("move-down", Some(1));
        settle(&mut ws, &mut terminal);
        assert_eq!(ws.views[1].selected_text().as_deref(), Some("你好"));
        assert!(ws.hyperlinks().iter().all(|l| l.end <= 140 && l.y < 48));
    }
    #[test]
    fn shifted_direction_creates_an_empty_source_pane_and_commands_reorder_and_merge() {
        let (mut ws, mut terminal) = fixture(2);
        ws.command("tab-right", Some(0));
        assert_eq!(ws.groups[&PaneId::ROOT].tabs, vec![1, 0]);
        ws.command("tab-left", Some(0));
        assert_eq!(ws.groups[&PaneId::ROOT].tabs, vec![0, 1]);
        prefix(&mut ws, KeyCode::Char('c'));
        ws.handle(Event::Key(KeyEvent::new(
            KeyCode::Char('w'),
            KeyModifiers::CONTROL,
        )));
        ws.handle(Event::Key(KeyEvent::new(
            KeyCode::Left,
            KeyModifiers::SHIFT,
        )));
        assert_eq!(ws.groups.len(), 2);
        assert_eq!(ws.active(), Some(1));
        assert!(ws.is_selecting(PaneId::ROOT));
        settle(&mut ws, &mut terminal);
        ws.command("merge-right", None);
        assert_eq!(ws.groups.len(), 1);
        assert_eq!(ws.active(), Some(1));
        ws.command("resize", None);
        key(&mut ws, KeyCode::Char('q'));
        assert!(ws.quit);
    }

    #[test]
    fn only_active_tabs_render_and_clean_panes_reuse_cells_without_relayout() {
        let (mut ws, mut terminal) = fixture(3);
        assert_eq!(ws.views[1].renders, 0);
        assert_eq!(ws.views[2].renders, 0);
        prefix(&mut ws, KeyCode::Char('v'));
        ws.show_file(1);
        settle(&mut ws, &mut terminal);
        let counts = (ws.views[0].renders, ws.views[1].renders);
        prefix(&mut ws, KeyCode::Char('h'));
        terminal.draw(|f| ws.render(f)).unwrap();
        assert_eq!((ws.views[0].renders, ws.views[1].renders), counts);
        key(&mut ws, KeyCode::Down);
        terminal.draw(|f| ws.render(f)).unwrap();
        assert_eq!(ws.views[0].renders, counts.0 + 1);
        assert_eq!(ws.views[1].renders, counts.1);
        let before = ws.cached.clone();
        key(&mut ws, KeyCode::Char('?'));
        terminal.draw(|f| ws.render(f)).unwrap();
        key(&mut ws, KeyCode::Esc);
        terminal.draw(|f| ws.render(f)).unwrap();
        assert_eq!(ws.cached, before);
        assert_eq!(ws.views[2].renders, 0);
        let mut model = Model::new();
        model.apply(AgentEvent {
            kind: "unknown".into(),
            data: json!({}),
            elapsed_ms: None,
            ts: None,
            seq: None,
        });
        ws.apply(2, model.change());
        assert!(!ws.dirty);
        assert_eq!(ws.views[2].summary.events, 1);
        key(&mut ws, KeyCode::Char('t'));
        terminal.draw(|f| ws.render(f)).unwrap();
        assert_eq!(ws.views[1].renders, counts.1 + 1);
    }

    #[test]
    fn first_late_file_does_not_interrupt_the_session_filter() {
        let mut ws = Workspace::new();
        key(&mut ws, KeyCode::Char('x'));
        ws.discover_file("new.jsonl".into(), false);
        assert!(ws.is_selecting(ws.focused()));
        assert_eq!(ws.groups[&ws.focused()].query, "x");
        assert_eq!(ws.active(), None);
    }

    #[test]
    fn all_tabs_can_be_hidden_without_quitting_and_tiny_frames_are_safe() {
        let (mut ws, mut terminal) = fixture(3);
        for _ in 0..3 {
            prefix(&mut ws, KeyCode::Char('c'));
        }
        assert_eq!(ws.views.len(), 3);
        assert!(ws.is_selecting(ws.focused()));
        assert!(!ws.quit);
        ws.add_file("/new.jsonl".into());
        assert!(ws.is_selecting(ws.focused()));
        for (width, height) in [(0, 0), (1, 1), (8, 3), (30, 6), (140, 48)] {
            terminal.backend_mut().resize(width, height);
            terminal.autoresize().unwrap();
            terminal.draw(|f| ws.render(f)).unwrap();
        }
    }
}
