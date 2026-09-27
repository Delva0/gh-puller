//! Reader lifetimes are independent of open tabs; consume bounded batches in rotation.
use crate::model::Change;
use crate::{
    reader,
    sources::{Discovery, Found, Sources},
    workspace::Workspace,
};
use std::{
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::Receiver,
    },
    time::{Duration, Instant},
};

pub struct FileSession {
    updates: Receiver<Change>,
    stop: Arc<AtomicBool>,
}
impl FileSession {
    pub fn new(path: PathBuf) -> Self {
        let (updates, stop) = reader::spawn(&path);
        Self { updates, stop }
    }
    pub fn poll(&self) -> Option<Change> {
        self.updates.try_recv().ok()
    }
}
impl Drop for FileSession {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

pub struct Sessions {
    discovery: Discovery,
    files: Vec<FileSession>,
    next: usize,
}
impl Sessions {
    pub fn new(sources: Sources) -> Self {
        Self {
            discovery: Discovery::spawn(sources),
            files: vec![],
            next: 0,
        }
    }
    pub fn poll(&mut self, workspace: &mut Workspace) {
        for _ in 0..16 {
            match self.discovery.updates.try_recv() {
                Ok(Found::File { path, initial }) => {
                    workspace.discover_file(path.clone(), initial);
                    self.files.push(FileSession::new(path));
                }
                Ok(Found::Issues(issues)) => {
                    workspace.issues = issues;
                    workspace.dirty = true;
                }
                Err(_) => break,
            }
        }
        let start = Instant::now();
        for _ in 0..self.files.len() {
            let id = self.next;
            self.next = (self.next + 1) % self.files.len();
            // One batch per file per pass. Start the next pass after the last serviced file.
            if let Some(change) = self.files[id].poll() {
                workspace.apply(id, change);
            }
            if start.elapsed() >= Duration::from_millis(4) {
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs::{self, OpenOptions},
        io::Write,
        thread,
    };

    fn poll_until(
        sessions: &mut Sessions,
        ws: &mut Workspace,
        predicate: impl Fn(&Workspace) -> bool,
    ) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !predicate(ws) && Instant::now() < deadline {
            sessions.poll(ws);
            thread::sleep(Duration::from_millis(5));
        }
        assert!(predicate(ws));
    }
    #[test]
    fn hidden_sessions_keep_consuming_and_read_errors_and_end_clocks_are_isolated() {
        let dir = tempfile::tempdir().unwrap();
        let paths: Vec<_> = (0..3)
            .map(|i| dir.path().join(format!("{i}.jsonl")))
            .collect();
        for path in &paths {
            fs::write(path, "{\"type\":\"session/start\"}\n").unwrap();
        }
        let mut sessions = Sessions::new(Sources {
            files: paths.clone(),
            watches: vec![],
        });
        let mut ws = Workspace::new();
        poll_until(&mut sessions, &mut ws, |ws| {
            ws.views.len() == 3 && ws.event_counts() == vec![1, 1, 1]
        });
        ws.command("close-tab", Some(0));
        let mut files: Vec<_> = paths
            .iter()
            .map(|p| OpenOptions::new().append(true).open(p).unwrap())
            .collect();
        writeln!(files[0], "{{\"type\":\"context/append/user\",\"data\":{{\"items\":[{{\"type\":\"message\",\"role\":\"user\",\"content\":[{{\"type\":\"input_text\",\"text\":\"Hidden update\"}}]}}]}}}}").unwrap();
        writeln!(files[1], "not JSON").unwrap();
        writeln!(
            files[2],
            "{{\"type\":\"session/end\",\"data\":{{\"outcome\":\"completed\"}}}}"
        )
        .unwrap();
        poll_until(&mut sessions, &mut ws, |ws| {
            ws.views[0].summary.events == 2
                && ws.views[1]
                    .summary
                    .status
                    .starts_with("Read stopped: line 2")
                && ws.views[2].summary.ended
        });
        let frozen = ws.views[2].summary.status.clone();
        for _ in 0..800 {
            writeln!(files[0], "{{\"type\":\"unknown\"}}").unwrap();
        }
        poll_until(&mut sessions, &mut ws, |ws| {
            ws.views[0].summary.events == 802
        });
        assert_eq!(ws.views[2].summary.status, frozen);
        assert!(
            ws.views[0]
                .cards
                .values()
                .any(|c| c.text == "Hidden update")
        );
        ws.show_file(0);
        assert_eq!(ws.views[0].summary.events, 802);
    }
}
