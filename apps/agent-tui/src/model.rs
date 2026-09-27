//! Fold only canonical state facts; activity decorates, but never commits, content.
use crate::stats::{self, Stats};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    sync::Arc,
};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Event {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub data: Value,
    #[serde(default, rename = "elapsedMs")]
    pub elapsed_ms: Option<f64>,
    #[serde(default)]
    pub ts: Option<f64>,
    #[serde(default)]
    pub seq: Option<u64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum Kind {
    User,
    System,
    Think,
    Answer,
    Tool,
    Turn,
    Step,
    Request,
    Notice,
    Error,
}
impl Kind {
    pub fn default_open(self) -> bool {
        matches!(self, Self::User | Self::Answer)
    }
    pub fn heading(self) -> bool {
        matches!(self, Self::Turn | Self::Step | Self::Request)
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::User => "user",
            Self::System => "system",
            Self::Think => "assistant think",
            Self::Answer => "assistant answer",
            Self::Tool => "tool",
            Self::Turn => "turn",
            Self::Step => "step",
            Self::Request => "request",
            Self::Notice => "context",
            Self::Error => "error",
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Card {
    pub id: String,
    pub kind: Kind,
    pub name: String,
    pub text: String,
    pub arguments: String,
    pub result: Option<String>,
    pub recorded_result: Option<Arc<str>>,
    pub tokens: usize,
    pub provisional: bool,
    pub status: String,
    pub duration_ms: Option<f64>,
    pub request_duration_ms: Option<f64>,
    pub group: Vec<String>,
    pub revision: u64,
    pub call_id: String,
    #[serde(skip)]
    pub request: String,
    #[serde(skip)]
    pub token_source: String,
    #[serde(skip)]
    pub result_source: String,
    #[serde(skip)]
    pub images: usize,
    #[serde(skip)]
    pub started: Option<f64>,
    #[serde(skip)]
    output_seen: bool,
}
impl Card {
    fn new(id: String, kind: Kind) -> Self {
        Self {
            id,
            kind,
            name: String::new(),
            text: String::new(),
            arguments: String::new(),
            result: None,
            recorded_result: None,
            tokens: 0,
            provisional: false,
            status: String::new(),
            duration_ms: None,
            request_duration_ms: None,
            group: vec![],
            revision: 0,
            call_id: String::new(),
            request: String::new(),
            token_source: String::new(),
            result_source: String::new(),
            images: 0,
            started: None,
            output_seen: false,
        }
    }
    pub fn body(&self, expanded: bool) -> String {
        if !expanded {
            return String::new();
        }
        if self.kind == Kind::Tool {
            let args = if self.arguments.is_empty() {
                "Arguments not recorded"
            } else {
                &self.arguments
            };
            let results = self
                .result_versions()
                .into_iter()
                .map(|(label, text)| {
                    let body = if serde_json::from_str::<Value>(text).is_ok() {
                        format!("```json\n{text}\n```")
                    } else {
                        text.to_string()
                    };
                    if label.is_empty() {
                        body
                    } else {
                        format!("### {label}\n\n{body}")
                    }
                })
                .collect::<Vec<_>>()
                .join("\n\n");
            format!("```json\n{args}\n```\n\n{results}")
        } else {
            self.text.clone()
        }
    }
    fn result_versions(&self) -> Vec<(&str, &str)> {
        let Some(current) = self.result.as_deref() else {
            return vec![("", "No result in context")];
        };
        if let Some(recorded) = self.recorded_result.as_deref().filter(|r| *r != current) {
            vec![("Recorded result", recorded), ("Current context", current)]
        } else {
            vec![("", current)]
        }
    }
    pub fn result_text(&self) -> String {
        self.result_versions()
            .into_iter()
            .map(|(label, text)| {
                if label.is_empty() {
                    text.to_string()
                } else {
                    format!("{label}\n{text}")
                }
            })
            .collect::<Vec<_>>()
            .join("\n\n")
    }
}

#[derive(Clone, Default)]
pub struct Summary {
    pub title: String,
    pub footer: String,
    pub details: String,
    pub ended: bool,
    pub status: String,
    pub events: usize,
    pub last_ms: f64,
}
pub struct Change {
    pub reset: Option<Vec<String>>,
    pub append: Vec<String>,
    pub cards: Vec<Arc<Card>>,
    pub summary: Summary,
}

pub struct Model {
    pub context: Vec<Value>,
    pub agent: Value,
    pub cards: HashMap<String, Card>,
    pub order: Vec<String>,
    dirty: HashSet<String>,
    added: Vec<String>,
    reset: bool,
    identities: Vec<(String, String)>,
    pub stats: Stats,
    pub summary: Summary,
    sequence: u64,
    revision: u64,
    current_request: String,
    pending: HashMap<String, Vec<String>>,
    current_group: Vec<String>,
    recorded_results: HashMap<String, Arc<str>>,
    origin: Option<f64>,
    turn_started_ms: Option<f64>,
    timed_turns: usize,
    active_ms: f64,
    active_output: HashMap<String, (String, Option<f64>)>,
    request_started: HashMap<String, f64>,
}

pub fn duration(ms: f64) -> String {
    let seconds = ms.max(0.0) / 1000.0;
    if seconds < 59.995 {
        return format!("{seconds:.2}s");
    }
    let seconds = seconds.round() as u64;
    if seconds < 3600 {
        format!("{}m {:02}s", seconds / 60, seconds % 60)
    } else {
        format!(
            "{}h {:02}m {:02}s",
            seconds / 3600,
            seconds / 60 % 60,
            seconds % 60
        )
    }
}

pub fn pretty(v: &Value) -> String {
    if let Some(s) = v.as_str() {
        serde_json::from_str::<Value>(s)
            .ok()
            .map(|v| serde_json::to_string_pretty(&v).unwrap())
            .unwrap_or_else(|| s.into())
    } else {
        serde_json::to_string_pretty(v).unwrap()
    }
}

fn tool_definitions(part: &Value) -> String {
    let tools = match part.get("tools") {
        Some(Value::Array(tools)) => tools.as_slice(),
        None => &[],
        _ => return pretty(part),
    };
    let code = |value: &Value| {
        format!(
            "```json\n{}\n```",
            serde_json::to_string_pretty(value).unwrap()
        )
    };
    let mut sections = vec!["## Tools".to_string()];
    if tools.is_empty() {
        sections.push("No tools".into());
    }
    for tool in tools {
        let Some(fields) = tool.as_object() else {
            sections.push(code(tool));
            continue;
        };
        let mut rest = fields.clone();
        if let Some(name) = tool["name"].as_str() {
            sections.push(format!("### {name}"));
            rest.remove("name");
        }
        if let Some(description) = tool["description"].as_str() {
            sections.push(description.into());
            rest.remove("description");
        }
        if let Some(schema) = rest.remove("inputSchema") {
            sections.push(format!("#### Input schema\n\n{}", code(&schema)));
        }
        if !rest.is_empty() {
            sections.push(code(&Value::Object(rest)));
        }
    }
    let mut rest = part.as_object().unwrap().clone();
    rest.remove("type");
    rest.remove("tools");
    if !rest.is_empty() {
        sections.push(code(&Value::Object(rest)));
    }
    sections.join("\n\n")
}

fn image_meta(p: &Value) -> String {
    let v = p
        .get("image_url")
        .or_else(|| p.get("file_id"))
        .unwrap_or(&Value::Null);
    let url = v.as_str().or_else(|| v["url"].as_str()).unwrap_or("opaque");
    let source = if url.starts_with("data:") {
        format!(
            "{} · {} bytes encoded",
            url.split(',').next().unwrap_or("image"),
            url.len()
        )
    } else {
        url.into()
    };
    format!(
        "[image] {source} · detail={} · size={}×{}",
        p.get("detail")
            .or_else(|| v.get("detail"))
            .and_then(Value::as_str)
            .unwrap_or("unknown"),
        p.get("width")
            .map(Value::to_string)
            .unwrap_or_else(|| "?".into()),
        p.get("height")
            .map(Value::to_string)
            .unwrap_or_else(|| "?".into())
    )
}

fn identity(item: &Value) -> String {
    if let Some(id) = item["id"].as_str() {
        return format!("id:{id}");
    }
    if let Some(call) = item["call_id"].as_str() {
        return format!("{}:{call}", item["type"].as_str().unwrap_or(""));
    }
    let mut v = item.clone();
    if let Some(o) = v.as_object_mut() {
        o.remove("metadata");
    }
    v.to_string()
}

impl Default for Model {
    fn default() -> Self {
        Self::new()
    }
}
impl Model {
    pub fn new() -> Self {
        Self {
            context: vec![],
            agent: Value::Null,
            cards: HashMap::new(),
            order: vec![],
            dirty: HashSet::new(),
            added: vec![],
            reset: false,
            identities: vec![],
            stats: Stats::new(),
            summary: Summary {
                status: "Waiting for file".into(),
                ..Summary::default()
            },
            sequence: 0,
            revision: 0,
            current_request: String::new(),
            pending: HashMap::new(),
            current_group: vec![],
            recorded_results: HashMap::new(),
            origin: None,
            turn_started_ms: None,
            timed_turns: 0,
            active_ms: 0.0,
            active_output: HashMap::new(),
            request_started: HashMap::new(),
        }
    }
    fn key(&mut self) -> String {
        self.sequence += 1;
        format!("item:{}", self.sequence)
    }
    fn put(&mut self, mut card: Card) {
        self.revision += 1;
        card.revision = self.revision;
        if !self.cards.contains_key(&card.id) {
            self.order.push(card.id.clone());
            self.added.push(card.id.clone());
        }
        self.dirty.insert(card.id.clone());
        self.cards.insert(card.id.clone(), card);
    }
    fn group(&mut self, id: String, kind: Kind, name: String, at: f64) {
        let mut c = Card::new(id.clone(), kind);
        c.name = name;
        c.started = Some(at);
        self.put(c);
        self.current_group.push(id);
    }
    fn finish(&mut self, id: &str, at: f64, status: &str) {
        if let Some(mut c) = self.cards.get(id).cloned() {
            c.duration_ms = c.started.map(|s| (at - s).max(0.0));
            c.status = status.into();
            self.put(c);
        }
    }
    fn elapsed(&self, at: Option<f64>) -> Option<f64> {
        if self.stats.turn == 0 {
            None
        } else if self.timed_turns == self.stats.turn {
            Some(self.active_ms)
        } else if !self.summary.ended && self.timed_turns + 1 == self.stats.turn {
            self.turn_started_ms
                .zip(at)
                .filter(|(start, end)| end >= start)
                .map(|(start, end)| self.active_ms + end - start)
        } else {
            None
        }
    }
    fn running_time(&mut self, at: Option<f64>) -> bool {
        let status = match self.elapsed(at) {
            Some(ms) => format!("Running · {}", duration(ms)),
            None => "Running".into(),
        };
        let mut changed = self.summary.status != status;
        self.summary.status = status;
        if let Some((start, end)) = self
            .turn_started_ms
            .zip(at)
            .filter(|(start, end)| end >= start)
            && let Some(mut card) = self
                .cards
                .get(&format!("turn:{}", self.stats.turn))
                .cloned()
            && card.duration_ms != Some(end - start)
        {
            card.duration_ms = Some(end - start);
            self.put(card);
            changed = true;
        }
        changed
    }
    /// Advance display time within an open turn without synthesizing events or usage.
    pub fn tick(&mut self, at: f64) -> bool {
        if self.summary.ended || self.turn_started_ms.is_none() {
            return false;
        }
        self.running_time(Some(at))
    }
    fn advance_output(&mut self, request: &str, at: Option<f64>) {
        let Some((id, previous)) = self.active_output.remove(request) else {
            return;
        };
        if let Some(c) = self.cards.get_mut(&id) {
            c.duration_ms = c
                .duration_ms
                .zip(previous.zip(at))
                .filter(|(_, (start, end))| end >= start)
                .map(|(total, (start, end))| total + end - start);
            self.revision += 1;
            c.revision = self.revision;
            self.dirty.insert(id);
        }
    }
    fn stop_output(&mut self, at: Option<f64>) {
        for request in self.active_output.keys().cloned().collect::<Vec<_>>() {
            self.advance_output(&request, at);
        }
    }
    fn project(&mut self, item: &Value, reuse: Option<String>, group: Vec<String>) -> String {
        let item_type = item["type"].as_str().unwrap_or("");
        let call_id = item["call_id"].as_str().unwrap_or("");
        let kind = match item_type {
            "function_call" | "function_call_output" => Kind::Tool,
            "reasoning" => Kind::Think,
            "message" => match item["role"].as_str().unwrap_or("") {
                "assistant" => Kind::Answer,
                "user" => Kind::User,
                "system" => Kind::System,
                _ => Kind::Notice,
            },
            _ => Kind::Notice,
        };
        let id = if kind == Kind::Tool {
            format!("tool:{call_id}")
        } else {
            reuse.unwrap_or_else(|| self.key())
        };
        let mut c = self
            .cards
            .get(&id)
            .cloned()
            .unwrap_or_else(|| Card::new(id.clone(), kind));
        c.kind = kind;
        c.provisional = false;
        if c.group.is_empty() || kind != Kind::Tool {
            c.group = group;
        }
        match item_type {
            "function_call" => {
                c.name = item["name"].as_str().unwrap_or("").into();
                c.call_id = call_id.into();
                c.arguments = pretty(&item["arguments"]);
                c.token_source = stats::raw(&item["arguments"]);
            }
            "function_call_output" => {
                c.call_id = call_id.into();
                c.result = Some(pretty(&item["output"]));
                c.recorded_result = self.recorded_results.get(call_id).cloned();
                c.result_source = stats::raw(&item["output"]);
                if c.status.is_empty() {
                    c.status = "Result received".into();
                }
            }
            _ => {
                c.images = 0;
                let mut text = vec![];
                let mut source = vec![];
                if let Some(parts) = item["content"].as_array() {
                    for p in parts {
                        if matches!(p["type"].as_str(), Some("input_image" | "image_url")) {
                            text.push(image_meta(p));
                            c.images += 1;
                        } else {
                            let t = p["text"]
                                .as_str()
                                .map(String::from)
                                .unwrap_or_else(|| pretty(p));
                            source.push(t.clone());
                            text.push(if p["type"] == "tool_defs" {
                                tool_definitions(p)
                            } else {
                                t
                            });
                        }
                    }
                } else if item_type == "reasoning" {
                    text.push(format!("[opaque reasoning] {}", pretty(item)));
                } else {
                    text.push(pretty(item));
                    source.push(stats::raw(item));
                }
                c.text = text.join("\n\n");
                c.token_source = source.join("\n\n");
            }
        }
        if kind == Kind::User && self.summary.title.is_empty() {
            self.summary.title = c.text.split_whitespace().collect::<Vec<_>>().join(" ");
            if self.summary.title.is_empty() {
                self.summary.title = "Untitled query".into();
            }
        }
        self.put(c);
        id
    }
    fn staged(&self, item: &Value) -> Option<String> {
        let kind = match item["type"].as_str().unwrap_or("") {
            "reasoning" => Kind::Think,
            "message" if item["role"] == "assistant" => Kind::Answer,
            _ => return None,
        };
        self.pending
            .get(&self.current_request)?
            .iter()
            .find(|id| {
                self.cards
                    .get(*id)
                    .is_some_and(|c| c.provisional && c.kind == kind)
            })
            .cloned()
    }
    fn append(&mut self, items: &[Value]) {
        for item in items {
            if item["type"] == "function_call_output" {
                self.recorded_results.insert(
                    item["call_id"].as_str().unwrap_or("").into(),
                    Arc::from(pretty(&item["output"])),
                );
            }
            let reuse = self.staged(item);
            let id = self.project(item, reuse, self.current_group.clone());
            if let Some(pending) = self.pending.get_mut(&self.current_request) {
                pending.retain(|p| p != &id);
            }
            self.identities.push((identity(item), id));
            self.context.push(item.clone());
        }
    }
    fn replace(&mut self, items: &[Value]) {
        let previous = std::mem::take(&mut self.cards);
        let mut ids: HashMap<String, VecDeque<String>> = HashMap::new();
        for (identity, id) in self.identities.drain(..) {
            ids.entry(identity).or_default().push_back(id);
        }
        self.order.clear();
        self.added.clear();
        self.dirty.clear();
        self.context.clear();
        self.pending.clear();
        let mut ordered = HashSet::new();
        for item in items {
            if item["type"] == "function_call_output" {
                self.recorded_results
                    .entry(item["call_id"].as_str().unwrap_or("").into())
                    .or_insert_with(|| Arc::from(pretty(&item["output"])));
            }
            let ident = identity(item);
            let reuse = ids.get_mut(&ident).and_then(VecDeque::pop_front);
            let old = reuse.as_ref().and_then(|id| previous.get(id));
            let group = old.map(|c| c.group.clone()).unwrap_or_default();
            for key in &group {
                if !self.cards.contains_key(key)
                    && let Some(c) = previous.get(key)
                {
                    self.put(c.clone());
                    ordered.insert(key.clone());
                }
            }
            // Only preserve activity decorations. Removed tool outputs cannot survive a replacement.
            if let Some(old) = old {
                let mut c = Card::new(old.id.clone(), old.kind);
                c.status = old.status.clone();
                c.started = old.started;
                c.duration_ms = old.duration_ms;
                c.request_duration_ms = old.request_duration_ms;
                c.output_seen = old.output_seen;
                if !self.cards.contains_key(&c.id) {
                    self.cards.insert(c.id.clone(), c);
                }
            }
            let id = self.project(item, reuse, group);
            if ordered.insert(id.clone()) && self.order.last() != Some(&id) {
                self.order.push(id.clone());
            }
            self.identities.push((ident, id));
            self.context.push(item.clone());
        }
        self.reset = true;
    }
    pub fn apply(&mut self, event: Event) {
        if self.summary.ended {
            return;
        }
        let kind = event.kind.as_str();
        let d = &event.data;
        let at = event.elapsed_ms.unwrap_or_else(|| {
            event
                .ts
                .map(|ts| {
                    let start = self.origin.get_or_insert(ts);
                    (ts - *start) * 1000.0
                })
                .unwrap_or(self.summary.last_ms)
        });
        self.summary.events += 1;
        self.summary.last_ms = at;
        self.stats.apply(kind, d, at);
        match kind {
            "agent/set" => self.agent = json!({"agent": d["agent"], "config": d["config"]}),
            "context/set" => {
                self.replace(d["items"].as_array().map(Vec::as_slice).unwrap_or_default())
            }
            "context/append"
            | "context/append/system"
            | "context/append/user"
            | "context/append/assistant"
            | "context/append/tool" => {
                self.append(d["items"].as_array().map(Vec::as_slice).unwrap_or_default())
            }
            "turn/start" => {
                self.turn_started_ms = event.elapsed_ms;
                self.current_group.clear();
                self.group(
                    format!("turn:{}", self.stats.turn),
                    Kind::Turn,
                    self.stats.turn.to_string(),
                    at,
                );
            }
            "step/start" => {
                self.current_group.retain(|id| id.starts_with("turn:"));
                self.group(
                    format!("step:{}:{}", self.stats.turn, self.stats.step),
                    Kind::Step,
                    self.stats.step.to_string(),
                    at,
                );
            }
            "turn/end" => {
                self.stop_output(event.elapsed_ms);
                if let Some((start, end)) = self.turn_started_ms.take().zip(event.elapsed_ms)
                    && end >= start
                {
                    self.active_ms += end - start;
                    self.timed_turns += 1;
                }
                self.finish(&format!("turn:{}", self.stats.turn), at, "");
            }
            "step/end" => self.finish(
                &format!("step:{}:{}", self.stats.turn, self.stats.step),
                at,
                "",
            ),
            "model/request" => {
                self.current_request = d["requestId"].as_str().unwrap_or("").into();
                if let Some(at) = event.elapsed_ms {
                    self.request_started
                        .insert(self.current_request.clone(), at);
                }
                self.current_group.retain(|id| !id.starts_with("request:"));
                self.group(
                    format!("request:{}", self.current_request),
                    Kind::Request,
                    format!(
                        "{} {}",
                        self.current_request,
                        d["model"].as_str().unwrap_or("")
                    ),
                    at,
                );
            }
            "model/delta/text" | "model/delta/reasoning" | "model/delta/tool-call" => {
                let request = d["requestId"]
                    .as_str()
                    .unwrap_or(&self.current_request)
                    .to_string();
                let index = d["index"].as_u64().unwrap_or(0);
                let card_kind = match kind {
                    "model/delta/text" => Kind::Answer,
                    "model/delta/reasoning" => Kind::Think,
                    _ => Kind::Tool,
                };
                let call = d["callId"].as_str().unwrap_or("");
                let id = if card_kind == Kind::Tool && !call.is_empty() {
                    format!("tool:{call}")
                } else {
                    format!("stream:{request}:{kind}:{index}")
                };
                let has_output = if card_kind == Kind::Tool {
                    ["argumentsDelta", "name"]
                        .iter()
                        .any(|field| d[field].as_str().is_some_and(|s| !s.is_empty()))
                } else {
                    d["text"].as_str().is_some_and(|s| !s.is_empty())
                };
                if has_output {
                    self.advance_output(&request, event.elapsed_ms);
                }
                if !self.cards.contains_key(&id) {
                    self.pending
                        .entry(request.clone())
                        .or_default()
                        .push(id.clone());
                }
                let mut c = self
                    .cards
                    .get(&id)
                    .cloned()
                    .unwrap_or_else(|| Card::new(id.clone(), card_kind));
                c.provisional = true;
                c.request = request.clone();
                c.group = self.current_group.clone();
                c.call_id = call.into();
                if card_kind == Kind::Tool {
                    if let Some(name) = d["name"].as_str() {
                        c.name = name.into();
                    }
                    c.arguments
                        .push_str(d["argumentsDelta"].as_str().unwrap_or(""));
                    c.token_source.clone_from(&c.arguments);
                } else {
                    if has_output {
                        if !c.output_seen {
                            c.output_seen = true;
                            c.duration_ms = event.elapsed_ms.map(|_| 0.0);
                        }
                        self.active_output.insert(request, (id, event.elapsed_ms));
                    }
                    c.text.push_str(d["text"].as_str().unwrap_or(""));
                    c.token_source.clone_from(&c.text);
                }
                self.put(c);
            }
            "model/response" => {
                let request = d["requestId"].as_str().unwrap_or("").to_string();
                let request_duration = self
                    .request_started
                    .remove(&request)
                    .zip(event.elapsed_ms)
                    .filter(|(start, end)| end >= start)
                    .map(|(start, end)| end - start);
                self.advance_output(&request, event.elapsed_ms);
                self.finish(
                    &format!("request:{request}"),
                    at,
                    d["stopReason"].as_str().unwrap_or(""),
                );
                let mut pending: VecDeque<_> =
                    self.pending.remove(&request).unwrap_or_default().into();
                let mut retained = HashSet::new();
                if let Some(output) = d["output"].as_array() {
                    for item in output {
                        let kind = match item["type"].as_str() {
                            Some("reasoning") => Kind::Think,
                            Some("function_call") => Kind::Tool,
                            _ => Kind::Answer,
                        };
                        let pos = pending.iter().position(|id| self.cards[id].kind == kind);
                        let reuse = pos.and_then(|p| pending.remove(p));
                        let id = self.project(item, reuse, self.current_group.clone());
                        let mut c = self.cards[&id].clone();
                        c.provisional = true;
                        c.request = request.clone();
                        if matches!(c.kind, Kind::Think | Kind::Answer) {
                            c.request_duration_ms = request_duration;
                        }
                        self.put(c);
                        retained.insert(id.clone());
                        self.pending.entry(request.clone()).or_default().push(id);
                    }
                }
                let remove: HashSet<_> = pending
                    .into_iter()
                    .filter(|id| !retained.contains(id))
                    .collect();
                if !remove.is_empty() {
                    self.order.retain(|id| !remove.contains(id));
                    for id in remove {
                        self.cards.remove(&id);
                        self.dirty.remove(&id);
                    }
                    self.reset = true;
                }
            }
            "tool/start" | "tool/end" => {
                let call = d["callId"].as_str().unwrap_or("");
                let id = format!("tool:{call}");
                let mut c = self.cards.get(&id).cloned().unwrap_or_else(|| {
                    let mut c = Card::new(id, Kind::Tool);
                    c.provisional = true;
                    c.group = self.current_group.clone();
                    c
                });
                c.call_id = call.into();
                if kind == "tool/start" {
                    c.started = Some(at);
                    c.name = d["name"].as_str().unwrap_or("").into();
                    c.status = "Running".into();
                    if c.arguments.is_empty() {
                        c.arguments = pretty(d.get("arguments").unwrap_or(&Value::Null));
                        c.token_source = stats::raw(&d["arguments"]);
                    }
                } else {
                    c.duration_ms = d["durationMs"]
                        .as_f64()
                        .or_else(|| c.started.map(|s| (at - s).max(0.0)));
                    c.status = if d.get("error").is_some() {
                        "Failed".into()
                    } else {
                        "Completed".into()
                    };
                }
                self.put(c);
            }
            "session/error" | "model/error" => {
                if kind == "session/error" {
                    self.stop_output(event.elapsed_ms);
                } else {
                    let request = d["requestId"]
                        .as_str()
                        .unwrap_or(&self.current_request)
                        .to_string();
                    self.advance_output(&request, event.elapsed_ms);
                    let elapsed = self
                        .request_started
                        .remove(&request)
                        .zip(event.elapsed_ms)
                        .filter(|(start, end)| end >= start)
                        .map(|(start, end)| end - start);
                    for id in self.pending.get(&request).cloned().unwrap_or_default() {
                        if let Some(mut c) = self.cards.get(&id).cloned()
                            && matches!(c.kind, Kind::Think | Kind::Answer)
                        {
                            c.request_duration_ms = elapsed;
                            self.put(c);
                        }
                    }
                }
                let id = self.key();
                let mut c = Card::new(id, Kind::Error);
                c.text = pretty(&d["error"]);
                c.token_source.clone_from(&c.text);
                self.put(c);
                if kind == "model/error" {
                    self.finish(
                        &format!("request:{}", d["requestId"].as_str().unwrap_or("")),
                        at,
                        "Failed",
                    );
                }
            }
            "session/end" => {
                // A late close cannot establish the end of an unfinished output phase.
                self.stop_output(None);
                self.summary.ended = true;
                let status = match d["reasonCode"].as_str().unwrap_or("") {
                    "cancelled" => "Cancelled",
                    "timeout" => "Timed out",
                    "budget_exhausted" => "Budget exhausted",
                    "error" => "Failed",
                    "" | "completed" => match d["outcome"].as_str() {
                        Some("completed") => "Completed",
                        Some("failed") => "Failed",
                        _ => "Ended",
                    },
                    reason => reason,
                };
                // Session lifetime can include hours spent waiting between completed turns.
                let elapsed = self
                    .elapsed(None)
                    .map(duration)
                    .unwrap_or_else(|| "—".into());
                self.summary.status = format!("{status} · {elapsed}");
                for id in self.current_group.clone() {
                    if self.cards.get(&id).is_some_and(|c| c.duration_ms.is_none()) {
                        self.finish(&id, at, "Stopped");
                    }
                }
            }
            _ => {
                if let Some(facet) = kind
                    .strip_prefix("agent/set/")
                    .filter(|s| !s.is_empty() && !s.contains('/'))
                {
                    if self.agent.is_null() {
                        self.agent = json!({"agent": null, "config": {}});
                    }
                    self.agent["config"][facet] = d[facet].clone();
                }
            }
        }
        if !self.summary.ended {
            self.running_time(event.elapsed_ms);
        }
    }

    pub fn change(&mut self) -> Change {
        let mut changed = vec![];
        for id in self.dirty.drain() {
            if let Some(c) = self.cards.get_mut(&id) {
                c.tokens = stats::count(&c.token_source)
                    + c.images * 1024
                    + stats::count(&c.result_source)
                    + if c.kind == Kind::Tool {
                        stats::count(&c.name)
                    } else {
                        0
                    };
                changed.push(Arc::new(c.clone()));
            }
        }
        self.summary.footer = self.stats.footer();
        self.summary.details = self.stats.details();
        Change {
            reset: if std::mem::take(&mut self.reset) {
                Some(self.order.clone())
            } else {
                None
            },
            append: std::mem::take(&mut self.added),
            cards: changed,
            summary: self.summary.clone(),
        }
    }
    pub fn folded(&self) -> Value {
        json!({"agent": self.agent, "context": self.context})
    }
}
