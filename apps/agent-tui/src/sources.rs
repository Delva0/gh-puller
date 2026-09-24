//! Startup-only sources and cancellable, recursive discovery. No directory symlinks.
use std::{
    collections::HashSet,
    fs, io,
    path::{Component, Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender, TrySendError},
    },
    thread,
    time::{Duration, Instant},
};

#[derive(Default, Debug)]
pub struct Sources {
    pub files: Vec<PathBuf>,
    pub watches: Vec<PathBuf>,
}

/// Resolve existing ancestors too, so a missing file has the same identity once created.
pub fn normalize(path: &Path) -> io::Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut result = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => (),
            Component::ParentDir => {
                result.pop();
            }
            other => {
                result.push(other.as_os_str());
                if let Ok(real) = fs::canonicalize(&result) {
                    result = real;
                }
            }
        }
    }
    Ok(result)
}

pub(crate) fn send<T>(tx: &SyncSender<T>, mut value: T, stop: &AtomicBool) -> bool {
    while !stop.load(Ordering::Relaxed) {
        match tx.try_send(value) {
            Ok(()) => return true,
            Err(TrySendError::Disconnected(_)) => return false,
            Err(TrySendError::Full(pending)) => value = pending,
        }
        thread::sleep(Duration::from_millis(2));
    }
    false
}

#[derive(Debug, PartialEq, Eq)]
pub enum Found {
    File { path: PathBuf, initial: bool },
    Issues(Vec<String>),
}

pub struct Discovery {
    pub updates: Receiver<Found>,
    stop: Arc<AtomicBool>,
}
impl Discovery {
    pub fn spawn(sources: Sources) -> Self {
        let (tx, updates) = mpsc::sync_channel(64);
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        thread::spawn(move || {
            let mut seen = HashSet::new();
            for file in sources.files {
                if let Ok(path) = normalize(&file)
                    && seen.insert(path.clone())
                    && !send(
                        &tx,
                        Found::File {
                            path,
                            initial: true,
                        },
                        &stopping,
                    )
                {
                    return;
                }
            }
            let mut previous_issues = vec![];
            let mut initial = true;
            while !stopping.load(Ordering::Relaxed) {
                let started = Instant::now();
                // Keep aliases for already opened paths even if they become symlinks later.
                seen.extend(
                    seen.iter()
                        .filter_map(|p| normalize(p).ok())
                        .collect::<Vec<_>>(),
                );
                let mut stack = sources.watches.clone();
                let mut visited = HashSet::new();
                let mut issues = vec![];
                while let Some(path) = stack.pop() {
                    if stopping.load(Ordering::Relaxed) {
                        return;
                    }
                    let metadata = match fs::symlink_metadata(&path) {
                        Ok(metadata) => metadata,
                        Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
                        Err(e) => {
                            issues.push(format!("{}: {e}", path.display()));
                            continue;
                        }
                    };
                    if metadata.is_dir() {
                        let Ok(real) = normalize(&path) else {
                            continue;
                        };
                        if !visited.insert(real) {
                            continue;
                        }
                        match fs::read_dir(&path) {
                            Ok(entries) => {
                                let mut paths = vec![];
                                for entry in entries {
                                    match entry {
                                        Ok(entry) => paths.push(entry.path()),
                                        Err(e) => issues.push(format!("{}: {e}", path.display())),
                                    }
                                }
                                paths.sort();
                                stack.extend(paths.into_iter().rev());
                            }
                            Err(e) => issues.push(format!("{}: {e}", path.display())),
                        }
                    } else if path.extension().is_some_and(|ext| ext == "jsonl") && path.is_file() {
                        match normalize(&path) {
                            Ok(real) if seen.insert(real.clone()) => {
                                if !send(
                                    &tx,
                                    Found::File {
                                        path: real,
                                        initial,
                                    },
                                    &stopping,
                                ) {
                                    return;
                                }
                            }
                            Err(e) => issues.push(format!("{}: {e}", path.display())),
                            _ => (),
                        }
                    }
                }
                initial = false;
                issues.sort();
                issues.dedup();
                if issues != previous_issues {
                    previous_issues.clone_from(&issues);
                    if !send(&tx, Found::Issues(issues), &stopping) {
                        return;
                    }
                }
                while started.elapsed() < Duration::from_secs(1)
                    && !stopping.load(Ordering::Relaxed)
                {
                    thread::sleep(Duration::from_millis(20));
                }
            }
        });
        Self { updates, stop }
    }
}
impl Drop for Discovery {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deduplicates_overlaps_and_waits_for_late_recursive_files() {
        let root = tempfile::tempdir().unwrap();
        let nested = root.path().join("nested");
        fs::create_dir(&nested).unwrap();
        let file = nested.join("first.jsonl");
        fs::write(&file, "").unwrap();
        let missing = root.path().join("later/new.jsonl");
        let d = Discovery::spawn(Sources {
            files: vec![
                file.clone(),
                nested.join("../nested/first.jsonl"),
                missing.clone(),
            ],
            watches: vec![root.path().into(), nested.clone()],
        });
        assert_eq!(
            d.updates.recv_timeout(Duration::from_secs(3)).unwrap(),
            Found::File {
                path: file,
                initial: true
            }
        );
        assert_eq!(
            d.updates.recv_timeout(Duration::from_secs(3)).unwrap(),
            Found::File {
                path: missing.clone(),
                initial: true
            }
        );
        thread::sleep(Duration::from_millis(80));
        fs::create_dir(missing.parent().unwrap()).unwrap();
        fs::write(missing, "").unwrap();
        fs::write(nested.join("ignored.txt"), "").unwrap();
        let new = nested.join("new.jsonl");
        fs::write(&new, "").unwrap();
        assert_eq!(
            d.updates.recv_timeout(Duration::from_secs(3)).unwrap(),
            Found::File {
                path: new,
                initial: false
            }
        );
        assert!(d.updates.recv_timeout(Duration::from_millis(1100)).is_err());
    }

    #[test]
    fn missing_watch_directory_is_discovered_later() {
        let root = tempfile::tempdir().unwrap();
        let watch = root.path().join("future");
        let d = Discovery::spawn(Sources {
            files: vec![],
            watches: vec![watch.clone()],
        });
        assert!(d.updates.recv_timeout(Duration::from_millis(80)).is_err());
        fs::create_dir_all(watch.join("child")).unwrap();
        let file = watch.join("child/events.jsonl");
        fs::write(&file, "").unwrap();
        assert_eq!(
            d.updates.recv_timeout(Duration::from_secs(3)).unwrap(),
            Found::File {
                path: file,
                initial: false
            }
        );
    }

    #[cfg(unix)]
    #[test]
    fn follows_file_aliases_once_but_never_directory_symlinks() {
        use std::os::unix::fs::symlink;
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let file = root.path().join("events.jsonl");
        fs::write(&file, "").unwrap();
        fs::write(outside.path().join("excluded.jsonl"), "").unwrap();
        symlink(outside.path(), root.path().join("dir")).unwrap();
        symlink(root.path(), root.path().join("loop")).unwrap();
        symlink(&file, root.path().join("alias.jsonl")).unwrap();
        let d = Discovery::spawn(Sources {
            files: vec![file.clone()],
            watches: vec![root.path().into()],
        });
        assert_eq!(
            d.updates.recv_timeout(Duration::from_secs(3)).unwrap(),
            Found::File {
                path: file,
                initial: true
            }
        );
        assert!(d.updates.recv_timeout(Duration::from_millis(1150)).is_err());
        assert_eq!(
            normalize(&root.path().join("dir/future/new.jsonl")).unwrap(),
            outside.path().join("future/new.jsonl")
        );
    }

    #[test]
    fn full_discovery_queue_can_be_cancelled() {
        let root = tempfile::tempdir().unwrap();
        let d = Discovery::spawn(Sources {
            files: (0..200)
                .map(|i| root.path().join(format!("{i}.jsonl")))
                .collect(),
            watches: vec![],
        });
        let stop = d.stop.clone();
        thread::sleep(Duration::from_millis(50));
        stop.store(true, Ordering::Relaxed);
        let deadline = Instant::now() + Duration::from_secs(2);
        while Arc::strong_count(&stop) > 2 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(Arc::strong_count(&stop), 2);
    }
}
