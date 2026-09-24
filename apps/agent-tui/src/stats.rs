//! Local o200k counts and request-anchored estimates. API usage is authoritative.
use serde_json::Value;
use std::{collections::BTreeMap, sync::OnceLock};

pub fn count(text: &str) -> usize {
    static BPE: OnceLock<tiktoken_rs::CoreBPE> = OnceLock::new();
    BPE.get_or_init(|| tiktoken_rs::o200k_base().expect("bundled o200k vocabulary"))
        .encode_ordinary(text)
        .len()
}

pub fn number(n: usize) -> String {
    if n >= 1000 {
        format!("{:.2}K", n as f64 / 1000.0)
    } else {
        n.to_string()
    }
}

/// Match Python json.dumps(ensure_ascii=False), including framing spaces.
pub fn raw(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        _ => json_text(value),
    }
}

fn json_text(v: &Value) -> String {
    match v {
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(json_text).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", serde_json::to_string(k).unwrap(), json_text(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        _ => v.to_string(),
    }
}

pub fn item_units(item: &Value) -> usize {
    4 + match item["type"].as_str().unwrap_or("") {
        "function_call" => count(&raw(&item["name"])) + count(&raw(&item["arguments"])),
        "function_call_output" => count(&raw(&item["output"])),
        _ => item["content"]
            .as_array()
            .map(|parts| {
                parts
                    .iter()
                    .map(|p| {
                        if matches!(p["type"].as_str(), Some("input_image" | "image_url")) {
                            1024
                        } else {
                            count(&raw(p.get("text").unwrap_or(p)))
                        }
                    })
                    .sum()
            })
            .unwrap_or(0),
    }
}

#[derive(Clone, Default)]
pub struct Generation {
    pub first: Option<f64>,
    pub end: Option<f64>,
    pub output: Option<usize>,
    pub input: Option<usize>,
    pub cached: Option<usize>,
    pub reasoning: Option<usize>,
    pub parts: BTreeMap<String, String>,
    units: usize,
    dirty: bool,
    pub request_units: f64,
}

impl Generation {
    pub fn units(&mut self) -> usize {
        if self.dirty {
            self.units = self.parts.values().map(|s| count(s)).sum();
            self.dirty = false;
        }
        self.units
    }
    fn delta(&mut self, kind: &str, d: &Value, at: f64) {
        let index = d["index"].as_u64().unwrap_or(0);
        let key = format!("{kind}:{index}");
        let text = if kind == "model/delta/tool-call" {
            d["argumentsDelta"].as_str()
        } else {
            d["text"].as_str()
        }
        .unwrap_or("");
        let mut changed = !text.is_empty();
        if kind == "model/delta/tool-call" {
            let name_key = format!("name:{index}");
            if let Some(name) = d["name"].as_str() {
                changed |= self.parts.get(&name_key).map(String::as_str) != Some(name);
                self.parts.insert(name_key, name.to_owned());
            }
        }
        if changed {
            self.first.get_or_insert(at);
            self.parts.entry(key).or_default().push_str(text);
            self.dirty = true;
        }
    }
}

#[derive(Default)]
pub struct Stats {
    pub turn: usize,
    pub step: usize,
    pub units: f64,
    anchor_units: f64,
    anchor_tokens: f64,
    scale: f64,
    pub turn_input: usize,
    capture: bool,
    current: String,
    pending: Option<String>,
    pub requests: BTreeMap<String, Generation>,
    turn_requests: Vec<String>,
    pub last_ms: f64,
    pub composition: BTreeMap<String, usize>,
}

impl Stats {
    pub fn new() -> Self {
        Self {
            scale: 1.0,
            capture: true,
            ..Self::default()
        }
    }
    fn context_count(&mut self) -> f64 {
        let mut n = self.anchor_tokens + (self.units - self.anchor_units) * self.scale;
        if let Some(g) = self
            .pending
            .as_ref()
            .and_then(|id| self.requests.get_mut(id))
        {
            n += g.output.unwrap_or_else(|| g.units()) as f64;
        }
        n.max(0.0)
    }
    pub fn apply(&mut self, kind: &str, data: &Value, at: f64) {
        self.last_ms = at;
        match kind {
            "turn/start" => {
                self.turn += 1;
                self.step = 0;
                self.capture = true;
                self.turn_requests.clear();
            }
            "step/start" => self.step += 1,
            "context/set"
            | "context/append"
            | "context/append/system"
            | "context/append/user"
            | "context/append/assistant"
            | "context/append/tool" => {
                let items = data["items"]
                    .as_array()
                    .map(Vec::as_slice)
                    .unwrap_or_default();
                let added: usize = items.iter().map(item_units).sum();
                if kind == "context/set" {
                    self.units = 0.0;
                    self.pending = None;
                    self.composition.clear();
                    if items.iter().all(|i| i["role"] == "system") {
                        self.anchor_tokens = 0.0;
                        self.anchor_units = 0.0;
                        self.scale = 1.0;
                    }
                } else if items.iter().any(|i| {
                    i["role"] == "assistant"
                        || matches!(i["type"].as_str(), Some("reasoning" | "function_call"))
                }) && self.pending.is_some()
                {
                    self.anchor_tokens = self.context_count();
                    self.anchor_units = self.units + added as f64;
                    self.pending = None;
                }
                self.units += added as f64;
                for item in items {
                    let kind = item["role"]
                        .as_str()
                        .or_else(|| item["type"].as_str())
                        .unwrap_or("unknown");
                    *self.composition.entry(kind.into()).or_default() += item_units(item);
                }
            }
            "model/request" => {
                if self.capture {
                    self.turn_input = self.context_count().round() as usize;
                    self.capture = false;
                }
                self.current = data["requestId"].as_str().unwrap_or("").into();
                if !self.requests.contains_key(&self.current) {
                    self.turn_requests.push(self.current.clone());
                    self.requests.insert(
                        self.current.clone(),
                        Generation {
                            request_units: self.units,
                            ..Generation::default()
                        },
                    );
                }
                self.pending = Some(self.current.clone());
            }
            "model/delta/text" | "model/delta/reasoning" | "model/delta/tool-call" => {
                let id = data["requestId"].as_str().unwrap_or(&self.current);
                self.requests
                    .entry(id.into())
                    .or_default()
                    .delta(kind, data, at);
            }
            "model/response" | "model/error" => {
                let id = data["requestId"].as_str().unwrap_or(&self.current);
                let g = self.requests.entry(id.into()).or_default();
                g.end = Some(at);
                g.output = data["usage"]["output"].as_u64().map(|n| n as usize);
                g.input = data["usage"]["input"].as_u64().map(|n| n as usize);
                g.cached = data["usage"]["cacheRead"].as_u64().map(|n| n as usize);
                g.reasoning = data["usage"]["reasoning"].as_u64().map(|n| n as usize);
                if kind == "model/response"
                    && let Some(output) = data["output"].as_array()
                {
                    g.parts.clear();
                    for (i, item) in output.iter().enumerate() {
                        if item["type"] == "function_call" {
                            g.parts.insert(format!("name:{i}"), raw(&item["name"]));
                            g.parts
                                .insert(format!("arguments:{i}"), raw(&item["arguments"]));
                        } else if let Some(parts) = item["content"].as_array() {
                            for (j, part) in parts.iter().enumerate() {
                                if let Some(text) = part["text"].as_str() {
                                    g.parts.insert(format!("text:{i}:{j}"), text.into());
                                }
                            }
                        }
                    }
                    g.dirty = true;
                }
                if let Some(input) = g.input {
                    self.anchor_tokens = input as f64;
                    self.anchor_units = g.request_units;
                    if g.request_units > 0.0 {
                        self.scale = input as f64 / g.request_units;
                    }
                }
                if kind == "model/error" {
                    self.pending = None;
                }
            }
            "session/error" | "turn/end" | "session/end" => {
                self.pending = None;
                for id in &self.turn_requests {
                    if let Some(g) = self.requests.get_mut(id) {
                        g.end.get_or_insert(at);
                    }
                }
            }
            _ => (),
        }
    }

    pub fn footer(&mut self) -> String {
        let (mut output, mut seconds, mut known, mut timed) = (0, 0.0, true, true);
        for id in &self.turn_requests {
            let g = self.requests.get_mut(id).unwrap();
            let n = g.output.unwrap_or_else(|| g.units());
            if g.end.is_some() && g.output.is_none() && n == 0 {
                known = false;
            }
            output += n;
            if let Some(first) = g.first {
                seconds += (g.end.unwrap_or(self.last_ms) - first).max(0.0) / 1000.0;
            } else if n > 0 {
                timed = false;
            }
        }
        let rate = if known && timed && seconds > 0.0 {
            let n = output as f64 / seconds;
            if n >= 1000.0 {
                number(n.round() as usize)
            } else {
                format!("{n:.1}")
            }
        } else {
            "—".into()
        };
        format!(
            "{}/{} {}/{} {}/s",
            self.turn,
            self.step,
            number(self.turn_input),
            if known { number(output) } else { "—".into() },
            rate
        )
    }

    pub fn details(&mut self) -> String {
        let context = self.context_count().round() as usize;
        let usage = |ids: Vec<&Generation>| {
            let known = ids
                .iter()
                .filter(|g| g.input.is_some() && g.output.is_some())
                .count();
            let input: usize = ids.iter().filter_map(|g| g.input).sum();
            let output: usize = ids.iter().filter_map(|g| g.output).sum();
            let cache: usize = ids.iter().filter_map(|g| g.cached).sum();
            let cache_known = ids.iter().filter(|g| g.cached.is_some()).count();
            format!(
                "input {} / output {} · usage {known}/{} · cacheRead {} ({cache_known}/{} known)",
                number(input),
                number(output),
                ids.len(),
                number(cache),
                ids.len()
            )
        };
        let mut text = format!(
            "Current context ≈{} tokens\nTurn input {} tokens (frozen at first request)\n\nLatest request {}\n{}\n\nTurn {}\nSession {}\n\nContext composition (local counts)\n",
            number(context),
            number(self.turn_input),
            self.current,
            usage(self.requests.get(&self.current).into_iter().collect()),
            usage(
                self.turn_requests
                    .iter()
                    .filter_map(|id| self.requests.get(id))
                    .collect()
            ),
            usage(self.requests.values().collect())
        );
        for (kind, n) in &self.composition {
            text.push_str(&format!("  {kind}: {}\n", number(*n)));
        }
        text.push_str("\nEstimates use o200k_base encode_ordinary, plus 4 framing tokens\nper Item and 1024 tokens per image. API input usage calibrates\nsubsequent context estimates; turn input stays frozen.\nOutput includes reasoning. Usage replaces stream estimates.\nSpeed covers the first nonempty delta through response, excluding\ntool waits and first-output waits. Without deltas, speed is —.\nUnknown usage is not counted as zero. Card counts estimate\ncontent size, not provider billing.\n");
        text.push_str("Tool card counts follow current context; earlier recorded\nresults may also be shown. Tool time covers execution. Think and\nanswer time covers each observed output phase, from its first\nnonempty delta to the next part or response/error/turn end.\nRepeated phases add up; first-output waits are excluded. These\nare receipt intervals, not server timing. User/system time and\noutput time without deltas are —. Missing timing is not zero.\nSession time sums turn/start to turn/end using elapsedMs,\nincluding model and tool waits. Idle time outside turns is\nexcluded. Missing or incomplete turn timing is —.\n");
        text
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn counts_and_format() {
        assert_eq!(count("hello world"), 2);
        assert_eq!(count("你好，世界"), 3);
        assert_eq!(count("<|endoftext|>"), 7);
        assert_eq!(count("```rust\nlet x = 42;\n```"), 10);
        assert_eq!(count("{\"path\": \"源代码.py\"}"), 8);
        assert_eq!(count("👨‍👩‍👧‍👦"), 11);
        assert_eq!(number(1_000_000), "1000.00K");
        assert_eq!(
            item_units(&json!({"content":[{"type":"input_image", "image_url":"data:large"}]})),
            1028
        );
    }
    #[test]
    fn frozen_input_output_calibration_and_compact_speed() {
        let mut s = Stats::new();
        s.apply("turn/start", &json!({}), 0.0);
        s.apply("context/append/user", &json!({"items":[{"role":"user","content":[{"type":"input_text","text":"hello world"}]}]}), 0.0);
        s.apply("model/request", &json!({"requestId":"r"}), 100.0);
        assert_eq!(s.turn_input, 6);
        s.apply(
            "model/response",
            &json!({"requestId":"r","usage":{"input":200,"output":30}}),
            500.0,
        );
        assert_eq!(s.footer(), "1/0 6/30 —/s");
        s.apply("turn/start", &json!({}), 600.0);
        s.apply("model/request", &json!({"requestId":"r2"}), 600.0);
        s.apply(
            "model/delta/reasoning",
            &json!({"requestId":"r2","text":"abc","index":0}),
            800.0,
        );
        s.apply(
            "model/response",
            &json!({"requestId":"r2","usage":{"output":20}}),
            1800.0,
        );
        assert!(s.footer().ends_with("/20 20.0/s"));
    }
}
