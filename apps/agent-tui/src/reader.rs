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
    time::{Duration, Instant},
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
    pub fn partial(&self) -> bool {
        self.bytes.len() > self.cursor
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

pub fn spawn(path: &Path) -> (Receiver<Change>, Arc<AtomicBool>) {
    let path = path.to_owned();
    let (tx, rx) = mpsc::sync_channel(2);
    let stop = Arc::new(AtomicBool::new(false));
    let stopping = stop.clone();
    thread::spawn(move || {
        let mut tail = Tail::new(path);
        let mut model = Model::new();
        let mut sent = false;
        let mut partial = false;
        while !stopping.load(Ordering::Relaxed) {
            let start = Instant::now();
            let mut changed = false;
            for _ in 0..256 {
                match tail.next_event() {
                    Ok(Some(event)) => {
                        model.apply(event);
                        changed = true;
                    }
                    Ok(None) => break,
                    Err(e) => {
                        model.summary.status = format!("读取停止 · {e} · 上下文仅覆盖有效前缀");
                        let _ = tx.send(model.change());
                        return;
                    }
                }
                if start.elapsed() >= Duration::from_millis(12) {
                    break;
                }
            }
            if partial != tail.partial() {
                partial = tail.partial();
                changed = true;
            }
            if changed || !sent {
                if partial && !model.summary.ended {
                    model.summary.status = "等待完整 JSONL 行 · 尚未确认结束".into();
                }
                if tx.send(model.change()).is_err() {
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
