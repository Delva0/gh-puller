//! A held file handle survives FileSink's atomic replacement. Decode only full lines.
use crate::model::{Change, Event, Model};
use std::{
    fs::File,
    io::{self, Read},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver},
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

pub struct Tail {
    path: PathBuf,
    file: Option<File>,
    bytes: Vec<u8>,
    cursor: usize,
    pub lines: usize,
    pub ended: bool,
}
impl Tail {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            file: None,
            bytes: vec![],
            cursor: 0,
            lines: 0,
            ended: false,
        }
    }
    pub fn next_event(&mut self) -> io::Result<Option<Event>> {
        if self.ended {
            return Ok(None);
        }
        if self.file.is_none() {
            match File::open(&self.path) {
                Ok(f) => self.file = Some(f),
                Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
                Err(e) => return Err(e),
            }
        }
        if !self.bytes[self.cursor..].contains(&b'\n') {
            self.bytes.drain(..self.cursor);
            self.cursor = 0;
            let mut chunk = [0; 65536];
            let n = self.file.as_mut().unwrap().read(&mut chunk)?;
            self.bytes.extend_from_slice(&chunk[..n]);
        }
        let Some(end) = self.bytes[self.cursor..]
            .iter()
            .position(|b| *b == b'\n')
            .map(|p| p + self.cursor)
        else {
            return Ok(None);
        };
        self.lines += 1;
        let slice = &self.bytes[self.cursor..end];
        self.cursor = end + 1;
        let event: Event = serde_json::from_slice(slice).map_err(|e| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("line {}: {e}", self.lines),
            )
        })?;
        self.ended = event.kind == "session/end";
        Ok(Some(event))
    }
}

struct Clock {
    at: f64,
    observed: Instant,
}
impl Clock {
    fn from_event(event: &Event) -> Option<Self> {
        let at = event.elapsed_ms?;
        // Bridge the recorder's clock once; subsequent waiting uses a monotonic clock.
        let age = event
            .ts
            .and_then(|ts| {
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .ok()
                    .map(|now| (now.as_secs_f64() - ts).max(0.0) * 1000.0)
            })
            .unwrap_or(0.0);
        Some(Self {
            at: at + age,
            observed: Instant::now(),
        })
    }
    fn elapsed(&self) -> f64 {
        self.at + self.observed.elapsed().as_secs_f64() * 1000.0
    }
}

pub fn spawn(path: &Path) -> (Receiver<Change>, Arc<AtomicBool>) {
    let path = path.to_owned();
    let (tx, rx) = mpsc::sync_channel(2);
    let stop = Arc::new(AtomicBool::new(false));
    let stopping = stop.clone();
    thread::spawn(move || {
        let mut tail = Tail::new(path);
        let mut model = Model::new();
        let mut sent = false;
        let mut clock: Option<Clock> = None;
        let mut ticked = Instant::now();
        while !stopping.load(Ordering::Relaxed) {
            let start = Instant::now();
            let mut changed = false;
            let opened = tail.file.is_some();
            for _ in 0..256 {
                match tail.next_event() {
                    Ok(Some(event)) => {
                        clock = Clock::from_event(&event);
                        model.apply(event);
                        changed = true;
                    }
                    Ok(None) => break,
                    Err(e) => {
                        model.summary.status = format!("Read stopped: {e}");
                        let _ = crate::sources::send(&tx, model.change(), &stopping);
                        return;
                    }
                }
                if start.elapsed() >= Duration::from_millis(12) {
                    break;
                }
            }
            if !opened && tail.file.is_some() {
                if model.summary.events == 0 {
                    model.summary.status = "Waiting for session".into();
                }
                changed = true;
            }
            if !changed && ticked.elapsed() >= Duration::from_millis(100) {
                if let Some(clock) = &clock {
                    changed = model.tick(clock.elapsed());
                }
                ticked = Instant::now();
            }
            if changed || !sent {
                if !crate::sources::send(&tx, model.change(), &stopping) {
                    return;
                }
                sent = true;
            }
            if tail.ended {
                return;
            }
            if !changed {
                thread::sleep(Duration::from_millis(16));
            }
        }
    });
    (rx, stop)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn receive(rx: &Receiver<Change>, accept: impl Fn(&Change) -> bool) -> Change {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let change = rx
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .unwrap();
            if accept(&change) {
                return change;
            }
        }
    }

    #[test]
    fn session_state_and_live_clock_follow_events_and_pause_between_turns() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        let mut writer = File::create(&path).unwrap();
        let (rx, stop) = spawn(&path);
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(3))
                .unwrap()
                .summary
                .status,
            "Waiting for session"
        );
        let mut send = |kind, elapsed, data| {
            let event = serde_json::json!({"type":kind, "elapsedMs":elapsed, "data":data,
                "ts":SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()});
            writeln!(writer, "{event}").unwrap();
            writer.flush().unwrap();
        };
        send("session/start", 0.0, serde_json::json!({}));
        receive(&rx, |c| c.summary.status == "Running");
        send("turn/start", 0.0, serde_json::json!({}));
        send(
            "context/append/assistant",
            50.0,
            serde_json::json!({"items":[
                {"type":"message", "role":"assistant", "content":[{"type":"output_text", "text":"Visible now"}]}
            ]}),
        );
        let current = receive(&rx, |c| {
            c.cards.iter().any(|card| card.text == "Visible now")
        });
        assert_eq!(current.summary.events, 3);
        assert!(!current.summary.ended);
        let tick = receive(&rx, |c| {
            c.cards
                .iter()
                .any(|card| card.id == "turn:1" && card.duration_ms > Some(50.0))
        });
        assert!(tick.summary.status.starts_with("Running · "));
        assert_eq!(tick.summary.events, 3);
        assert_eq!(tick.summary.last_ms, 50.0);
        send("turn/end", 400.0, serde_json::json!({}));
        receive(&rx, |c| c.summary.status == "Running · 0.40s");
        assert!(matches!(
            rx.recv_timeout(Duration::from_millis(220)),
            Err(mpsc::RecvTimeoutError::Timeout)
        ));
        send("turn/start", 100_000.0, serde_json::json!({}));
        send("turn/end", 100_300.0, serde_json::json!({}));
        send(
            "session/end",
            22_000_000.0,
            serde_json::json!({"outcome":"completed"}),
        );
        let ended = receive(&rx, |c| c.summary.ended);
        assert_eq!(ended.summary.status, "Completed · 0.70s");
        assert!(rx.recv_timeout(Duration::from_millis(220)).is_err());
        stop.store(true, Ordering::Relaxed);
    }

    #[test]
    fn a_full_reader_queue_does_not_prevent_shutdown() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        std::fs::write(&path, "{\"type\":\"unknown\"}\n".repeat(10000)).unwrap();
        let (_rx, stop) = spawn(&path);
        std::thread::sleep(Duration::from_millis(150));
        stop.store(true, Ordering::Relaxed);
        let deadline = Instant::now() + Duration::from_secs(3);
        while Arc::strong_count(&stop) > 1 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(Arc::strong_count(&stop), 1);
    }

    #[test]
    fn waiting_partial_utf8_and_atomic_replace() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        let mut tail = Tail::new(&path);
        assert!(tail.next_event().unwrap().is_none());
        let mut writer = File::create(&path).unwrap();
        let line = "{\"type\":\"model/delta/text\",\"data\":{\"text\":\"你\"}}\n".as_bytes();
        let cut = line.iter().position(|b| *b > 127).unwrap() + 1;
        writer.write_all(&line[..cut]).unwrap();
        assert!(tail.next_event().unwrap().is_none());
        writer.write_all(&line[cut..]).unwrap();
        assert_eq!(tail.next_event().unwrap().unwrap().data["text"], "你");
        writer
            .write_all(b"{\"type\":\"session/end\",\"data\":{\"outcome\":\"completed\"}}\n")
            .unwrap();
        let compact = dir.path().join("compact");
        std::fs::write(&compact, b"{\"type\":\"session/end\"}\n").unwrap();
        std::fs::rename(compact, &path).unwrap();
        assert_eq!(
            tail.next_event().unwrap().unwrap().data["outcome"],
            "completed"
        );
        let mut new_reader = Tail::new(path);
        assert_eq!(
            new_reader.next_event().unwrap().unwrap().kind,
            "session/end"
        );
        assert!(tail.next_event().unwrap().is_none());
    }
}
