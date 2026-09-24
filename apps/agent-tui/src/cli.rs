//! Observation sources are fixed at startup; offline modes are handled before this parser.
use crate::sources::Sources;
use std::path::PathBuf;

#[derive(Debug)]
pub struct Observation {
    pub sources: Sources,
    pub trace: Option<PathBuf>,
}
impl Observation {
    pub fn parse(args: impl IntoIterator<Item = String>) -> Result<Self, String> {
        let mut result = Self {
            sources: Sources::default(),
            trace: None,
        };
        let mut args = args.into_iter();
        let mut paths_only = false;
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--" if !paths_only => paths_only = true,
                "--watch" | "--trace-input" if !paths_only => {
                    let path = args
                        .next()
                        .filter(|p| !p.starts_with("--"))
                        .ok_or_else(|| format!("missing path after {arg}"))?;
                    if arg == "--watch" {
                        result.sources.watches.push(path.into());
                    } else {
                        result.trace = Some(path.into());
                    }
                }
                flag if !paths_only && flag.starts_with('-') => {
                    return Err(format!("unknown option: {flag}"));
                }
                _ => result.sources.files.push(arg.into()),
            }
        }
        if result.sources.files.is_empty() && result.sources.watches.is_empty() {
            return Err("at least one FILE or --watch DIR is required".into());
        }
        Ok(result)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn parse(args: &[&str]) -> Result<Observation, String> {
        Observation::parse(args.iter().map(|a| a.to_string()))
    }
    #[test]
    fn accepts_single_multiple_mixed_and_watch_only_sources() {
        assert_eq!(parse(&["first.jsonl"]).unwrap().sources.files.len(), 1);
        let mixed = parse(&[
            "first.jsonl",
            "--watch",
            "sessions",
            "second.jsonl",
            "--watch",
            "other",
            "--trace-input",
            "trace",
        ])
        .unwrap();
        assert_eq!(
            mixed.sources.files,
            vec![PathBuf::from("first.jsonl"), PathBuf::from("second.jsonl")]
        );
        assert_eq!(mixed.sources.watches.len(), 2);
        assert_eq!(mixed.trace, Some(PathBuf::from("trace")));
        assert!(
            parse(&["--watch", "future"])
                .unwrap()
                .sources
                .files
                .is_empty()
        );
        assert_eq!(
            parse(&["--", "--watch"]).unwrap().sources.files,
            vec![PathBuf::from("--watch")]
        );
    }
    #[test]
    fn rejects_missing_paths_and_unknown_options() {
        for args in [
            &[][..],
            &["--watch"],
            &["a", "--trace-input"],
            &["--watch", "--trace-input", "trace"],
            &["a", "--typo"],
        ] {
            assert!(parse(args).is_err());
        }
    }
}
