//! Local-only Ollama chat with per-intent, allowlisted FileBot tools.
use crate::{
    agent::tools::ProposeCmdTool,
    agent::{FastRouter, Intent},
    config::{ConfigError, Settings, expand_path},
    retrieval::hybrid_engine::{HybridEngine, RetrievalError},
    storage::{Database, StorageError},
};
use reqwest::{Url, blocking::Client};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Read},
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};
use thiserror::Error;

const MAX_PROMPT_CHARS: usize = 16_000;
const MAX_RESPONSE_BYTES: usize = 2 * 1024 * 1024;
const MAX_TOOL_ROUNDS: usize = 5;
const MAX_TOOL_CALLS: usize = 5;
const HISTORY_TURNS: usize = 12;
const SYSTEM_PROMPT: &str = "You are FileBot, a local Windows file search and CMD proposal assistant. Answer ordinary questions directly and briefly. You may only use tools offered for this turn. Available actions are searching indexed files, opening an existing indexed file, or staging a CMD proposal for explicit UI approval. You do not have file creation, modification, or deletion tools. Never execute a shell command. A proposed command has not run; the user must separately approve it in the UI. Never claim an action succeeded unless its tool result confirms it.";

#[derive(Debug, Error)]
pub enum AgentError {
    #[error("invalid agent configuration: {0}")]
    Config(#[from] ConfigError),
    #[error("Ollama connection failed: {0}")]
    Http(#[from] reqwest::Error),
    #[error("Ollama returned invalid response: {0}")]
    Response(String),
    #[error("Ollama stream read failed: {0}")]
    StreamIo(#[from] std::io::Error),
    #[error("chat was cancelled")]
    Cancelled,
    #[error("local retrieval failed: {0}")]
    Retrieval(#[from] RetrievalError),
    #[error("local storage failed: {0}")]
    Storage(#[from] StorageError),
    #[error("command proposal failed: {0}")]
    Proposal(String),
    #[error("file operation failed: {0}")]
    FileOperation(String),
}

/// Transport abstraction keeps chat-turn policy testable without a live model.
pub trait OllamaTransport {
    fn check_model(&mut self, model: &str) -> Result<(), AgentError>;
    fn chat(&mut self, request: &Value) -> Result<Value, AgentError>;
    /// Stream user-visible plain-chat text; transports may provide a buffered fallback.
    fn chat_stream(
        &mut self,
        request: &Value,
        stream: bool,
        on_delta: &mut dyn FnMut(&str) -> bool,
    ) -> Result<Value, AgentError> {
        let response = self.chat(request)?;
        if stream
            && let Some(content) = response.pointer("/message/content").and_then(Value::as_str)
            && !content.is_empty()
            && !on_delta(content)
        {
            return Err(AgentError::Cancelled);
        }
        Ok(response)
    }
    fn unload(&mut self, model: &str, keep_alive: &str) -> Result<(), AgentError>;
}

/// Ollama HTTP transport that refuses non-loopback endpoints.
pub struct LocalOllamaClient {
    client: Client,
    base_url: Url,
}

impl LocalOllamaClient {
    /// Create a bounded HTTP client pointed only to a local Ollama service.
    pub fn new(base_url: &str) -> Result<Self, AgentError> {
        let parsed = Url::parse(base_url)
            .map_err(|error| ConfigError::Invalid(format!("invalid Ollama URL: {error}")))?;
        if parsed.scheme() != "http" || !is_loopback_host(parsed.host_str()) {
            return Err(ConfigError::Invalid("Ollama must use plain HTTP on localhost or a loopback IP; remote endpoints are disabled".into()).into());
        }
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(120))
            .build()?;
        Ok(Self {
            client,
            base_url: parsed,
        })
    }

    fn endpoint(&self, path: &str) -> Result<Url, AgentError> {
        self.base_url.join(path).map_err(|error| {
            ConfigError::Invalid(format!("invalid Ollama endpoint: {error}")).into()
        })
    }

    fn checked_json(response: reqwest::blocking::Response) -> Result<Value, AgentError> {
        if !response.status().is_success() {
            let status = response.status();
            let mut bytes = Vec::new();
            let _ = response.take(4096).read_to_end(&mut bytes);
            let body = String::from_utf8_lossy(&bytes);
            return Err(AgentError::Response(format!("HTTP {status}: {body}")));
        }
        if response
            .content_length()
            .is_some_and(|size| size > MAX_RESPONSE_BYTES as u64)
        {
            return Err(AgentError::Response(
                "Ollama response exceeded the 2 MiB limit".into(),
            ));
        }
        let bytes = response.bytes()?;
        if bytes.len() > MAX_RESPONSE_BYTES {
            return Err(AgentError::Response(
                "Ollama response exceeded the 2 MiB limit".into(),
            ));
        }
        serde_json::from_slice(&bytes).map_err(|error| AgentError::Response(error.to_string()))
    }
}

impl OllamaTransport for LocalOllamaClient {
    fn check_model(&mut self, model: &str) -> Result<(), AgentError> {
        let response = self.client.get(self.endpoint("api/tags")?).send()?;
        let payload = Self::checked_json(response)?;
        let models = payload
            .get("models")
            .and_then(Value::as_array)
            .ok_or_else(|| AgentError::Response("Ollama returned no valid model list".into()))?;
        if models.iter().any(|entry| {
            entry
                .get("name")
                .or_else(|| entry.get("model"))
                .and_then(Value::as_str)
                == Some(model)
        }) {
            return Ok(());
        }
        Err(AgentError::Response(format!(
            "Model `{model}` is not installed. Run `ollama pull {model}` and retry."
        )))
    }
    fn chat(&mut self, request: &Value) -> Result<Value, AgentError> {
        let response = self
            .client
            .post(self.endpoint("api/chat")?)
            .json(request)
            .send()?;
        Self::checked_json(response)
    }
    fn chat_stream(
        &mut self,
        request: &Value,
        stream: bool,
        on_delta: &mut dyn FnMut(&str) -> bool,
    ) -> Result<Value, AgentError> {
        if !stream {
            return self.chat(request);
        }
        let mut request = request.clone();
        request["stream"] = Value::Bool(true);
        let response = self
            .client
            .post(self.endpoint("api/chat")?)
            .json(&request)
            .send()?;
        if !response.status().is_success() {
            return Self::checked_json(response);
        }
        if response
            .content_length()
            .is_some_and(|size| size > MAX_RESPONSE_BYTES as u64)
        {
            return Err(AgentError::Response(
                "Ollama response exceeded the 2 MiB limit".into(),
            ));
        }
        let mut reader = BufReader::new(response.take(MAX_RESPONSE_BYTES as u64 + 1));
        let mut message = json!({"role":"assistant","content":""});
        let mut total_bytes = 0usize;
        let mut line = Vec::new();
        loop {
            line.clear();
            let bytes = reader.read_until(b'\n', &mut line)?;
            if bytes == 0 {
                break;
            }
            total_bytes = total_bytes.saturating_add(bytes);
            if total_bytes > MAX_RESPONSE_BYTES {
                return Err(AgentError::Response(
                    "Ollama response exceeded the 2 MiB limit".into(),
                ));
            }
            let frame: Value = serde_json::from_slice(&line)
                .map_err(|error| AgentError::Response(error.to_string()))?;
            if let Some(delta) = frame.pointer("/message/content").and_then(Value::as_str) {
                message["content"] = json!(format!(
                    "{}{}",
                    message["content"].as_str().unwrap_or_default(),
                    delta
                ));
                if !delta.is_empty() && !on_delta(delta) {
                    return Err(AgentError::Cancelled);
                }
            }
            if let Some(calls) = frame
                .pointer("/message/tool_calls")
                .and_then(Value::as_array)
            {
                message["tool_calls"] = Value::Array(calls.clone());
            }
            if frame.get("done").and_then(Value::as_bool) == Some(true) {
                break;
            }
        }
        Ok(json!({"message":message}))
    }
    fn unload(&mut self, model: &str, keep_alive: &str) -> Result<(), AgentError> {
        let request = json!({"model":model,"prompt":"","stream":false,"keep_alive":keep_alive});
        let response = self
            .client
            .post(self.endpoint("api/generate")?)
            .json(&request)
            .send()?;
        let _ = Self::checked_json(response)?;
        Ok(())
    }
}

fn is_loopback_host(host: Option<&str>) -> bool {
    match host.unwrap_or_default().trim_matches(['[', ']']) {
        "localhost" | "localhost." => true,
        value => value
            .parse::<std::net::IpAddr>()
            .is_ok_and(|address| address.is_loopback()),
    }
}

/// Fixed action interface: there is deliberately no shell execution method.
pub trait AgentTools {
    fn execute(
        &mut self,
        intent: Intent,
        name: &str,
        arguments: &Value,
    ) -> Result<String, AgentError>;
    fn deactivate(&mut self) {}
}

/// FileBot tools: hybrid search, indexed/root-contained open, and proposal-only CMD.
pub struct FileBotTools {
    engine: HybridEngine,
    database: Database,
    roots: Vec<PathBuf>,
    proposals: ProposeCmdTool,
}

impl FileBotTools {
    /// Construct the fixed tool surface from current settings and policy.
    pub fn new(
        settings: &Settings,
        database: Database,
        policy_path: impl AsRef<Path>,
    ) -> Result<Self, AgentError> {
        let roots = settings
            .index
            .roots
            .iter()
            .map(|root| expand_path(root))
            .collect::<Result<Vec<_>, _>>()?;
        let engine = HybridEngine::new(settings, database.clone())?;
        let proposals = ProposeCmdTool::new(policy_path, database.clone())
            .map_err(|error| AgentError::Proposal(error.to_string()))?;
        Ok(Self {
            engine,
            database,
            roots,
            proposals,
        })
    }

    fn open_indexed_file(&self, raw_path: &str) -> Result<String, AgentError> {
        if raw_path.chars().count() > 4096 {
            return Err(AgentError::FileOperation("path exceeds the limit".into()));
        }
        let path = PathBuf::from(raw_path)
            .canonicalize()
            .map_err(|error| AgentError::FileOperation(error.to_string()))?;
        if !path.is_file() {
            return Err(AgentError::FileOperation(
                "only existing files can be opened".into(),
            ));
        }
        let roots = self
            .roots
            .iter()
            .filter_map(|root| root.canonicalize().ok())
            .collect::<Vec<_>>();
        if !roots.iter().any(|root| path.starts_with(root)) {
            return Err(AgentError::FileOperation(
                "file is outside FileBot's configured roots".into(),
            ));
        }
        if !self.database.is_indexed_file(&path)? {
            return Err(AgentError::FileOperation(
                "file is not in FileBot's local index".into(),
            ));
        }
        #[cfg(windows)]
        {
            Command::new("explorer.exe")
                .arg(&path)
                .spawn()
                .map_err(|error| AgentError::FileOperation(error.to_string()))?;
            Ok(format!("Opened {}", path.display()))
        }
        #[cfg(not(windows))]
        {
            let _ = path;
            Err(AgentError::FileOperation(
                "opening indexed files is supported only on Windows".into(),
            ))
        }
    }
}

impl AgentTools for FileBotTools {
    fn execute(
        &mut self,
        intent: Intent,
        name: &str,
        arguments: &Value,
    ) -> Result<String, AgentError> {
        let allowed = match intent {
            Intent::Chat => None,
            Intent::Search => Some("search_files"),
            Intent::Open => Some("open_file"),
            Intent::Command => Some("propose_cmd"),
        };
        if allowed != Some(name) {
            return Err(AgentError::Response(
                "That tool is not available for this request".into(),
            ));
        }
        match name {
            "search_files" => {
                let query = required_string(arguments, "query", 8000)?;
                let limit = arguments
                    .get("limit")
                    .and_then(Value::as_u64)
                    .unwrap_or(10)
                    .clamp(1, 50) as usize;
                let roots = self
                    .roots
                    .iter()
                    .filter_map(|root| root.canonicalize().ok())
                    .collect::<Vec<_>>();
                let results = self
                    .engine
                    .search(&query, limit)?
                    .into_iter()
                    .filter(|hit| {
                        Path::new(&hit.path)
                            .canonicalize()
                            .ok()
                            .is_some_and(|path| roots.iter().any(|root| path.starts_with(root)))
                    })
                    .take(limit)
                    .collect::<Vec<_>>();
                serde_json::to_string(&results)
                    .map_err(|error| AgentError::Response(error.to_string()))
            }
            "open_file" => self.open_indexed_file(&required_string(arguments, "path", 4096)?),
            "propose_cmd" => {
                let command = required_string(arguments, "command", 4096)?;
                let reason = required_string(arguments, "reason", 1000)?;
                let cwd = arguments
                    .get("cwd")
                    .and_then(Value::as_str)
                    .filter(|value| value.chars().count() <= 4096);
                let proposal = self
                    .proposals
                    .propose(&command, &reason, cwd)
                    .map_err(|error| AgentError::Proposal(error.to_string()))?;
                serde_json::to_string(&json!({"proposal_id":proposal.proposal_id,"status":proposal.status,"risk_level":proposal.risk_level,"executed":false})).map_err(|error| AgentError::Response(error.to_string()))
            }
            _ => Err(AgentError::Response("unknown tool".into())),
        }
    }
    fn deactivate(&mut self) {
        self.engine.deactivate();
    }
}

/// A bounded local chat runtime with strict tool scope and proposal-only command action.
pub struct BotRuntime<T: OllamaTransport, A: AgentTools> {
    model: String,
    keep_alive: String,
    router: FastRouter,
    transport: T,
    tools: A,
    history: Vec<Value>,
    model_loaded: bool,
}

impl<T: OllamaTransport, A: AgentTools> BotRuntime<T, A> {
    /// Construct without network access or model loading; call `ask` after explicit wake.
    pub fn new(settings: &Settings, transport: T, tools: A) -> Result<Self, AgentError> {
        let model = settings.models.ollama.model.trim().to_owned();
        let keep_alive = settings.models.ollama.keep_alive.trim().to_owned();
        if model.is_empty() || model.chars().count() > 256 {
            return Err(ConfigError::Invalid("Ollama model name is invalid".into()).into());
        }
        if keep_alive.is_empty() || keep_alive.chars().count() > 32 {
            return Err(ConfigError::Invalid("Ollama keep_alive value is invalid".into()).into());
        }
        Ok(Self {
            model,
            keep_alive,
            router: FastRouter,
            transport,
            tools,
            history: Vec::new(),
            model_loaded: false,
        })
    }

    /// Run one local turn. Clear routing enables only that intent's one tool.
    pub fn ask(&mut self, prompt: &str) -> Result<String, AgentError> {
        self.ask_streaming(prompt, |_| true)
    }

    /// Stream ordinary chat deltas and allow the caller to cancel the active HTTP read.
    /// Action-oriented turns stay buffered so untrusted tool-call sketches never render as
    /// assistant prose before the runtime validates and executes the allowlisted tool.
    pub fn ask_streaming(
        &mut self,
        prompt: &str,
        mut on_delta: impl FnMut(&str) -> bool,
    ) -> Result<String, AgentError> {
        let prompt = prompt.trim();
        if prompt.is_empty() || prompt.chars().count() > MAX_PROMPT_CHARS {
            return Err(ConfigError::Invalid(
                "prompt must be between 1 and 16000 characters".into(),
            )
            .into());
        }
        self.transport.check_model(&self.model)?;
        let intent = self.router.route(prompt);
        let schemas = tool_schemas(intent);
        let allowed = offered_tool_names(&schemas);
        let mut messages = vec![json!({"role":"system","content":SYSTEM_PROMPT})];
        messages.extend(self.history.iter().cloned());
        messages.push(json!({"role":"user","content":prompt}));
        let mut proposal_staged = false;
        let mut total_tool_calls: usize = 0;

        for _ in 0..MAX_TOOL_ROUNDS {
            let should_stream = intent == Intent::Chat;
            let request = json!({"model":self.model,"messages":messages,"tools":schemas,"stream":should_stream,"keep_alive":self.keep_alive});
            // Ollama may load a model before a request times out or fails decoding.
            self.model_loaded = true;
            let response = self
                .transport
                .chat_stream(&request, should_stream, &mut on_delta)?;
            let mut message = response
                .get("message")
                .cloned()
                .ok_or_else(|| AgentError::Response("Ollama response has no message".into()))?;
            let mut calls = parse_tool_calls(&message, &allowed);
            if calls.is_empty()
                && let Some((name, arguments)) =
                    parse_text_tool_sketch(message.get("content").and_then(Value::as_str), &allowed)
            {
                calls.push(("filebot-text-call".to_owned(), name, arguments));
                message["content"] = Value::Null;
                message["tool_calls"] = Value::Array(calls.iter().map(|(id,name,args)| json!({"id":id,"type":"function","function":{"name":name,"arguments":args}})).collect());
            }
            total_tool_calls = total_tool_calls.saturating_add(calls.len());
            if total_tool_calls > MAX_TOOL_CALLS {
                return Err(AgentError::Response(
                    "agent exceeded the five tool-call limit".into(),
                ));
            }
            if calls.is_empty() {
                let content = message
                    .get("content")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .trim()
                    .to_owned();
                if content.is_empty() {
                    return Err(AgentError::Response(
                        "local model returned an empty answer".into(),
                    ));
                }
                let answer = if intent == Intent::Command && !proposal_staged {
                    format!(
                        "No command was executed. The local model did not prepare an approval proposal.\n\n{content}"
                    )
                } else {
                    content
                };
                self.remember(json!({"role":"user","content":prompt}));
                self.remember(json!({"role":"assistant","content":answer}));
                self.model_loaded = true;
                return Ok(answer);
            }
            messages.push(message);
            for (id, name, arguments) in calls {
                let result = match self.tools.execute(intent, &name, &arguments) {
                    Ok(output) => {
                        if name == "propose_cmd" {
                            proposal_staged = true;
                        }
                        output
                    }
                    Err(error) => json!({"error":error.to_string()}).to_string(),
                };
                messages.push(
                    json!({"role":"tool","tool_name":name,"content":result,"tool_call_id":id}),
                );
            }
        }
        Err(AgentError::Response(
            "agent exceeded the five tool-round limit".into(),
        ))
    }

    fn remember(&mut self, message: Value) {
        self.history.push(message);
        let max_messages = HISTORY_TURNS * 2;
        if self.history.len() > max_messages {
            self.history.drain(..self.history.len() - max_messages);
        }
    }

    /// Release local embedding inference and request the configured Ollama idle policy.
    pub fn sleep(&mut self) {
        self.tools.deactivate();
        if self.model_loaded {
            let _ = self.transport.unload(&self.model, &self.keep_alive);
        }
        self.model_loaded = false;
    }
}

/// Generate the exact JSON function surface for the selected request intent.
pub fn tool_schemas(intent: Intent) -> Vec<Value> {
    let definition = match intent {
        Intent::Chat => return Vec::new(),
        Intent::Search => {
            json!({"type":"function","function":{"name":"search_files","description":"Search local indexed files and return matching paths and snippets.","parameters":{"type":"object","properties":{"query":{"type":"string","maxLength":8000},"limit":{"type":"integer","minimum":1,"maximum":50}},"required":["query"],"additionalProperties":false}}})
        }
        Intent::Open => {
            json!({"type":"function","function":{"name":"open_file","description":"Open an existing indexed file from configured roots.","parameters":{"type":"object","properties":{"path":{"type":"string","maxLength":4096}},"required":["path"],"additionalProperties":false}}})
        }
        Intent::Command => {
            json!({"type":"function","function":{"name":"propose_cmd","description":"Stage a Windows CMD proposal for separate user approval. This never executes it.","parameters":{"type":"object","properties":{"command":{"type":"string","maxLength":4096},"reason":{"type":"string","maxLength":1000},"cwd":{"type":"string","maxLength":4096}},"required":["command","reason"],"additionalProperties":false}}})
        }
    };
    vec![definition]
}

fn offered_tool_names(schemas: &[Value]) -> Vec<String> {
    schemas
        .iter()
        .filter_map(|schema| {
            schema
                .pointer("/function/name")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .collect()
}

fn parse_tool_calls(message: &Value, allowed: &[String]) -> Vec<(String, String, Value)> {
    message
        .get("tool_calls")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|call| {
            let function = call.get("function")?;
            let name = function.get("name")?.as_str()?;
            if !allowed.iter().any(|allowed| allowed == name) {
                return None;
            }
            let arguments = normalize_arguments(function.get("arguments")?)?;
            if !arguments.is_object() {
                return None;
            }
            let id = call
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("filebot-tool-call")
                .chars()
                .take(128)
                .collect();
            Some((id, name.to_owned(), arguments))
        })
        .collect()
}

fn parse_text_tool_sketch(content: Option<&str>, allowed: &[String]) -> Option<(String, Value)> {
    let content = content?.trim();
    let value = if let Some(rest) = content.strip_prefix("```") {
        let body = rest.split_once('\n').map_or(rest, |(_, body)| body).trim();
        body.strip_suffix("```").unwrap_or(body).trim()
    } else {
        content
    };
    let payload: Value = serde_json::from_str(value).ok()?;
    let name = payload.get("name")?.as_str()?;
    if !allowed.iter().any(|allowed| allowed == name) {
        return None;
    }
    let arguments = normalize_arguments(payload.get("arguments")?)?;
    arguments.is_object().then(|| (name.to_owned(), arguments))
}

fn normalize_arguments(value: &Value) -> Option<Value> {
    match value {
        Value::Object(object)
            if object.len() == 2 && object.contains_key("type") && object.contains_key("value") =>
        {
            let value_type = object.get("type")?.as_str()?;
            if ![
                "string", "integer", "number", "boolean", "object", "array", "null",
            ]
            .contains(&value_type)
            {
                return None;
            }
            normalize_arguments(object.get("value")?)
        }
        Value::Object(object) => {
            let mut normalized = serde_json::Map::new();
            for (key, value) in object {
                normalized.insert(key.clone(), normalize_arguments(value)?);
            }
            Some(Value::Object(normalized))
        }
        Value::Array(values) => values
            .iter()
            .map(normalize_arguments)
            .collect::<Option<Vec<_>>>()
            .map(Value::Array),
        value => Some(value.clone()),
    }
}

fn required_string(arguments: &Value, name: &str, max_chars: usize) -> Result<String, AgentError> {
    let value = arguments
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| AgentError::Response(format!("tool argument `{name}` must be text")))?;
    if value.trim().is_empty() || value.chars().count() > max_chars {
        return Err(AgentError::Response(format!(
            "tool argument `{name}` is empty or too long"
        )));
    }
    Ok(value.to_owned())
}

/// Build the concrete runtime from validated settings, database, and CMD policy.
pub fn build_runtime(
    settings: &Settings,
    database: Database,
    policy_path: impl AsRef<Path>,
) -> Result<BotRuntime<LocalOllamaClient, FileBotTools>, AgentError> {
    let transport = LocalOllamaClient::new(&settings.models.ollama.base_url)?;
    let tools = FileBotTools::new(settings, database, policy_path)?;
    BotRuntime::new(settings, transport, tools)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    };

    #[derive(Clone, Default)]
    struct MockTransport {
        responses: Arc<Mutex<Vec<Value>>>,
        requests: Arc<Mutex<Vec<Value>>>,
        unloads: Arc<AtomicUsize>,
    }

    impl OllamaTransport for MockTransport {
        fn check_model(&mut self, _model: &str) -> Result<(), AgentError> {
            Ok(())
        }
        fn chat(&mut self, request: &Value) -> Result<Value, AgentError> {
            self.requests.lock().unwrap().push(request.clone());
            if self.responses.lock().unwrap().is_empty() {
                return Err(AgentError::Response("no mock response".into()));
            }
            Ok(self.responses.lock().unwrap().remove(0))
        }
        fn unload(&mut self, _model: &str, _keep_alive: &str) -> Result<(), AgentError> {
            self.unloads.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
    }

    #[derive(Default)]
    struct MockTools {
        calls: Arc<Mutex<Vec<String>>>,
        deactivations: usize,
    }
    impl AgentTools for MockTools {
        fn execute(
            &mut self,
            _intent: Intent,
            name: &str,
            _arguments: &Value,
        ) -> Result<String, AgentError> {
            self.calls.lock().unwrap().push(name.to_owned());
            Ok(r#"{"status":"pending","executed":false}"#.into())
        }
        fn deactivate(&mut self) {
            self.deactivations += 1;
        }
    }

    fn settings() -> Settings {
        crate::config::load_settings(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../config/settings.yaml"
        ))
        .unwrap()
    }

    fn final_response(content: &str) -> Value {
        json!({"message":{"role":"assistant","content":content}})
    }

    #[test]
    fn local_client_rejects_remote_or_non_http_endpoints() {
        assert!(LocalOllamaClient::new("https://127.0.0.1:11434").is_err());
        assert!(LocalOllamaClient::new("http://192.0.2.1:11434").is_err());
        assert!(LocalOllamaClient::new("http://localhost:11434").is_ok());
        assert!(LocalOllamaClient::new("http://[::1]:11434").is_ok());
    }

    #[test]
    fn ordinary_chat_offers_no_tools_and_remains_lazy() {
        let transport = MockTransport {
            responses: Arc::new(Mutex::new(vec![final_response("Hello locally")])),
            ..Default::default()
        };
        let requests = Arc::clone(&transport.requests);
        let mut runtime = BotRuntime::new(&settings(), transport, MockTools::default()).unwrap();
        assert_eq!(
            runtime.ask("Explain local indexing").unwrap(),
            "Hello locally"
        );
        assert!(
            requests.lock().unwrap()[0]["tools"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert_eq!(runtime.history.len(), 2);
        assert!(runtime.model_loaded);
    }

    #[test]
    fn ordinary_chat_streams_text_deltas_as_the_model_responds() {
        let transport = MockTransport {
            responses: Arc::new(Mutex::new(vec![final_response("Hello locally")])),
            ..Default::default()
        };
        let requests = Arc::clone(&transport.requests);
        let mut runtime = BotRuntime::new(&settings(), transport, MockTools::default()).unwrap();
        let mut deltas = Vec::new();
        let answer = runtime
            .ask_streaming("Explain local indexing", |delta| {
                deltas.push(delta.to_owned());
                true
            })
            .unwrap();
        assert_eq!(answer, "Hello locally");
        assert_eq!(deltas, ["Hello locally"]);
        assert_eq!(requests.lock().unwrap()[0]["stream"], true);
    }

    #[test]
    fn cancelling_stream_does_not_store_a_partial_turn_in_history() {
        let transport = MockTransport {
            responses: Arc::new(Mutex::new(vec![final_response("partial")])),
            ..Default::default()
        };
        let mut runtime = BotRuntime::new(&settings(), transport, MockTools::default()).unwrap();
        let result = runtime.ask_streaming("Explain local indexing", |_| false);
        assert!(matches!(result, Err(AgentError::Cancelled)));
        assert!(runtime.history.is_empty());
    }

    #[test]
    fn a_command_turn_can_only_stage_a_proposal_and_never_claims_execution() {
        let transport = MockTransport {
            responses: Arc::new(Mutex::new(vec![
                json!({"message":{"role":"assistant","content":null,"tool_calls":[{"id":"c1","function":{"name":"propose_cmd","arguments":{"command":"whoami","reason":"show user"}}}]}}),
                final_response("A proposal is pending approval. It has not run."),
            ])),
            ..Default::default()
        };
        let requests = Arc::clone(&transport.requests);
        let tools = MockTools::default();
        let calls = Arc::clone(&tools.calls);
        let mut runtime = BotRuntime::new(&settings(), transport, tools).unwrap();
        let answer = runtime.ask("Run whoami").unwrap();
        assert!(answer.contains("pending approval"));
        let captured = requests.lock().unwrap();
        assert_eq!(captured[0]["tools"].as_array().unwrap().len(), 1);
        assert_eq!(captured[0]["tools"][0]["function"]["name"], "propose_cmd");
        assert_eq!(captured[0]["stream"], false);
        assert_eq!(calls.lock().unwrap().as_slice(), &["propose_cmd"]);
    }

    #[test]
    fn qwen_text_tool_sketch_is_unwrapped_only_for_offered_tools() {
        let allowed = vec!["search_files".to_owned()];
        let parsed = parse_text_tool_sketch(Some(r#"{"name":"search_files","arguments":{"query":{"type":"string","value":"budget"}}}"#), &allowed).unwrap();
        assert_eq!(parsed.0, "search_files");
        assert_eq!(parsed.1["query"], "budget");
        assert!(
            parse_text_tool_sketch(
                Some(r#"{"name":"propose_cmd","arguments":{"command":"del","reason":"x"}}"#),
                &allowed
            )
            .is_none()
        );
        assert!(
            parse_text_tool_sketch(Some(r#"{"name":"search_files","arguments":[]}"#), &allowed)
                .is_none()
        );
    }

    #[test]
    fn command_turn_that_stages_nothing_states_no_execution_and_sleep_unloads() {
        let transport = MockTransport {
            responses: Arc::new(Mutex::new(vec![final_response("I ran whoami.")])),
            ..Default::default()
        };
        let unload_count = Arc::clone(&transport.unloads);
        let mut runtime = BotRuntime::new(&settings(), transport, MockTools::default()).unwrap();
        let answer = runtime.ask("Run whoami").unwrap();
        assert!(answer.starts_with("No command was executed."));
        runtime.sleep();
        assert_eq!(unload_count.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn tool_schemas_are_narrow_by_intent() {
        assert!(tool_schemas(Intent::Chat).is_empty());
        assert_eq!(
            offered_tool_names(&tool_schemas(Intent::Search)).as_slice(),
            &["search_files"]
        );
        assert_eq!(
            offered_tool_names(&tool_schemas(Intent::Open)).as_slice(),
            &["open_file"]
        );
        assert_eq!(
            offered_tool_names(&tool_schemas(Intent::Command)).as_slice(),
            &["propose_cmd"]
        );
    }
}
