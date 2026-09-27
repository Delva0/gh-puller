//! Choose a fixed startup view while sharing the terminal event loop.
use crate::{
    hyperlinks,
    session::{FileSession, Sessions},
    sources::{Sources, normalize},
    ui::App,
    workspace::Workspace,
};
use crossterm::event::{Event, KeyCode, KeyModifiers};
use ratatui::Frame;
use std::io;

pub enum Observer {
    File {
        view: Box<App>,
        session: FileSession,
    },
    Workspace {
        view: Box<Workspace>,
        sessions: Sessions,
    },
}
impl Observer {
    pub fn new(sources: Sources) -> io::Result<Self> {
        Ok(if sources.files.len() == 1 && sources.watches.is_empty() {
            let path = normalize(&sources.files[0])?;
            Self::File {
                view: Box::new(App::new(path.display().to_string())),
                session: FileSession::new(path),
            }
        } else {
            Self::Workspace {
                view: Box::default(),
                sessions: Sessions::new(sources),
            }
        })
    }
    pub fn handle(&mut self, event: Event) {
        match self {
            Self::File { view, .. } => {
                if matches!(event, Event::Key(key) if key.code == KeyCode::Char('w')
                    && key.modifiers.contains(KeyModifiers::CONTROL))
                {
                    return;
                }
                view.handle(event);
            }
            Self::Workspace { view, .. } => view.handle(event),
        }
    }
    pub fn poll(&mut self) {
        match self {
            Self::File { view, session } => {
                if let Some(change) = session.poll() {
                    view.apply(change);
                }
                view.poll_layout();
            }
            Self::Workspace { view, sessions } => {
                sessions.poll(view);
                view.poll_layout();
            }
        }
    }
    pub fn render(&mut self, frame: &mut Frame) {
        match self {
            Self::File { view, .. } => view.render(frame),
            Self::Workspace { view, .. } => view.render(frame),
        }
    }
    pub fn hyperlinks(&self) -> Vec<hyperlinks::Link> {
        match self {
            Self::File { view, .. } => view.hyperlinks(),
            Self::Workspace { view, .. } => view.hyperlinks(),
        }
    }
    pub fn dirty(&self) -> bool {
        match self {
            Self::File { view, .. } => view.dirty,
            Self::Workspace { view, .. } => view.dirty,
        }
    }
    pub fn quit(&self) -> bool {
        match self {
            Self::File { view, .. } => view.quit,
            Self::Workspace { view, .. } => view.quit,
        }
    }
    pub fn event_counts(&self) -> Vec<usize> {
        match self {
            Self::File { view, .. } => vec![view.summary.events],
            Self::Workspace { view, .. } => view.event_counts(),
        }
    }
    pub fn visible_files(&self) -> Vec<usize> {
        match self {
            Self::File { .. } => vec![0],
            Self::Workspace { view, .. } => view.visible_files(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyEvent;
    use ratatui::{Terminal, backend::TestBackend};
    use std::{
        fs, thread,
        time::{Duration, Instant},
    };
    use unicode_width::UnicodeWidthStr;

    #[test]
    fn only_one_explicit_file_without_watches_uses_the_file_view() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("waiting.jsonl");
        for (files, watches, single) in [
            (vec![file.clone()], vec![], true),
            (
                vec![file.clone(), dir.path().join("other.jsonl")],
                vec![],
                false,
            ),
            (vec![file], vec![dir.path().into()], false),
            (vec![], vec![dir.path().into()], false),
        ] {
            let observer = Observer::new(Sources { files, watches }).unwrap();
            assert_eq!(matches!(observer, Observer::File { .. }), single);
        }
    }

    #[test]
    fn file_view_fills_the_terminal_and_uses_file_commands_while_tailing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("events.jsonl");
        let mut observer = Observer::new(Sources {
            files: vec![path.clone()],
            watches: vec![],
        })
        .unwrap();
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|f| observer.render(f)).unwrap();
        let row = |terminal: &Terminal<TestBackend>, y| -> String {
            let mut text = String::new();
            let mut x = 0;
            while x < 80 {
                let symbol = terminal.backend().buffer()[(x, y)].symbol();
                text.push_str(symbol);
                x += symbol.width().max(1) as u16;
            }
            text
        };
        assert!(row(&terminal, 0).contains("Waiting for first query"));
        fs::write(&path, "{\"type\":\"context/append/user\",\"data\":{\"items\":[{\"type\":\"message\",\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"你好 reader\"}]}]}}\n").unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while observer.event_counts() != vec![1] && Instant::now() < deadline {
            observer.poll();
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(observer.event_counts(), vec![1]);
        assert_eq!(observer.visible_files(), vec![0]);
        terminal.draw(|f| observer.render(f)).unwrap();
        let heading = row(&terminal, 0);
        assert!(heading.trim_start().starts_with("你好 reader "));
        assert!(heading.trim_end().ends_with("events.jsonl"));
        assert!(!row(&terminal, 23).contains("Ctrl-W"));
        observer.handle(Event::Key(KeyEvent::new(
            KeyCode::Char('w'),
            KeyModifiers::CONTROL,
        )));
        assert!(!observer.dirty());
        observer.handle(Event::Key(KeyEvent::new(
            KeyCode::Char('?'),
            KeyModifiers::NONE,
        )));
        observer.handle(Event::Key(KeyEvent::new(
            KeyCode::Char('p'),
            KeyModifiers::CONTROL,
        )));
        terminal.draw(|f| observer.render(f)).unwrap();
        let screen = (0..24)
            .map(|y| row(&terminal, y))
            .collect::<Vec<_>>()
            .join("\n");
        assert!(screen.contains("Commands"));
        assert!(!screen.contains("Workspace"));
        observer.handle(Event::Key(KeyEvent::new(
            KeyCode::Char('q'),
            KeyModifiers::NONE,
        )));
        assert!(!observer.quit());
        observer.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        observer.handle(Event::Key(KeyEvent::new(
            KeyCode::Char('q'),
            KeyModifiers::NONE,
        )));
        assert!(observer.quit());
    }
}
