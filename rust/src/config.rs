//! Typed parser and validation for FileBot's existing YAML settings.
use serde::Deserialize;
use std::{
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("cannot read settings {path}: {source}")]
    Read {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("cannot parse settings YAML: {0}")]
    Parse(#[from] serde_yaml_ng::Error),
    #[error("invalid setting: {0}")]
    Invalid(String),
}

/// Validated app settings used by both the sleeping controller and awake runtime.
#[derive(Clone, Debug, Deserialize)]
pub struct Settings {
    pub app: AppSettings,
    pub index: IndexSettings,
    pub models: ModelsSettings,
    pub runtime: RuntimeSettings,
}

#[derive(Clone, Debug, Deserialize)]
pub struct AppSettings {
    pub name: String,
    pub database_path: String,
    #[serde(default = "default_log_level")]
    pub log_level: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct IndexSettings {
    pub roots: Vec<String>,
    pub include_extensions: Vec<String>,
    #[serde(default)]
    pub exclude_dirs: Vec<String>,
    #[serde(default = "default_max_file_bytes")]
    pub max_file_bytes: u64,
    #[serde(default = "default_max_extracted_chars")]
    pub max_extracted_chars: usize,
    #[serde(default = "default_max_docx_uncompressed_bytes")]
    pub max_docx_uncompressed_bytes: u64,
    #[serde(default = "default_chunk_chars")]
    pub chunk_chars: usize,
    #[serde(default = "default_chunk_overlap")]
    pub chunk_overlap: usize,
    #[serde(default = "default_debounce_seconds")]
    pub debounce_seconds: f64,
    #[serde(default = "default_background_batch_size")]
    pub background_batch_size: usize,
    #[serde(default = "default_background_poll_seconds")]
    pub background_poll_seconds: f64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ModelsSettings {
    pub ollama: OllamaSettings,
    pub embeddings: EmbeddingSettings,
    pub vector_store: VectorStoreSettings,
}

#[derive(Clone, Debug, Deserialize)]
pub struct OllamaSettings {
    pub base_url: String,
    #[serde(default)]
    pub api_base: String,
    pub model: String,
    #[serde(default = "default_keep_alive")]
    pub keep_alive: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct EmbeddingSettings {
    pub model: String,
    pub cache_dir: String,
    #[serde(default = "default_embedding_batch_size")]
    pub batch_size: usize,
}

#[derive(Clone, Debug, Deserialize)]
pub struct VectorStoreSettings {
    pub path: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct RuntimeSettings {
    pub hotkey: String,
    #[serde(default = "default_idle_unload_seconds")]
    pub idle_unload_seconds: u64,
}

/// Load a YAML config file and validate path, size, chunk, and timer bounds.
pub fn load_settings(path: impl AsRef<Path>) -> Result<Settings, ConfigError> {
    let path = path.as_ref();
    let source = fs::read_to_string(path).map_err(|source| ConfigError::Read {
        path: path.display().to_string(),
        source,
    })?;
    let settings: Settings = serde_yaml_ng::from_str(&source)?;
    validate(&settings)?;
    Ok(settings)
}

fn validate(settings: &Settings) -> Result<(), ConfigError> {
    let index = &settings.index;
    if index.roots.iter().any(|path| path.trim().is_empty()) || index.roots.is_empty() {
        return Err(ConfigError::Invalid(
            "index.roots must have non-empty paths".into(),
        ));
    }
    if index.include_extensions.is_empty()
        || index
            .include_extensions
            .iter()
            .any(|ext| !ext.starts_with('.'))
    {
        return Err(ConfigError::Invalid(
            "index.include_extensions must be dotted extensions".into(),
        ));
    }
    if index.max_file_bytes == 0
        || index.max_extracted_chars == 0
        || index.max_docx_uncompressed_bytes == 0
    {
        return Err(ConfigError::Invalid(
            "index file and extraction limits must be positive".into(),
        ));
    }
    if index.chunk_chars < 100 || index.chunk_overlap >= index.chunk_chars {
        return Err(ConfigError::Invalid(
            "chunk_overlap must be smaller than chunk_chars (minimum 100)".into(),
        ));
    }
    if index.debounce_seconds < 0.0
        || index.debounce_seconds > 3600.0
        || !index.debounce_seconds.is_finite()
        || index.background_poll_seconds <= 0.0
        || index.background_poll_seconds > 3600.0
        || !index.background_poll_seconds.is_finite()
        || index.background_batch_size == 0
    {
        return Err(ConfigError::Invalid(
            "index debounce, poll, and batch values are invalid".into(),
        ));
    }
    if settings.runtime.hotkey.trim().is_empty() || settings.runtime.idle_unload_seconds == 0 {
        return Err(ConfigError::Invalid(
            "hotkey and idle timer must be configured".into(),
        ));
    }
    if settings.models.ollama.model.trim().is_empty()
        || settings.models.ollama.base_url.trim().is_empty()
    {
        return Err(ConfigError::Invalid(
            "Ollama model and URL must be configured".into(),
        ));
    }
    if settings.models.embeddings.model.trim().is_empty()
        || settings.models.embeddings.batch_size == 0
    {
        return Err(ConfigError::Invalid(
            "embedding model and positive batch size are required".into(),
        ));
    }
    if settings.models.vector_store.path.trim().is_empty()
        || settings.app.database_path.trim().is_empty()
    {
        return Err(ConfigError::Invalid(
            "database and vector store paths must be configured".into(),
        ));
    }
    Ok(())
}

/// Expand leading `~` and `$VAR`/`%VAR%` syntax used by the current Windows config.
pub fn expand_path(value: &str) -> Result<PathBuf, ConfigError> {
    expand_path_with(value, |name| std::env::var_os(name))
}

fn expand_path_with(
    value: &str,
    mut get_env: impl FnMut(&str) -> Option<OsString>,
) -> Result<PathBuf, ConfigError> {
    let mut expanded = value.to_owned();
    if expanded == "~" || expanded.starts_with("~/") || expanded.starts_with("~\\") {
        let home = get_env("USERPROFILE").ok_or_else(|| {
            ConfigError::Invalid("USERPROFILE is unavailable while expanding '~'".into())
        })?;
        expanded.replace_range(..1, &home.to_string_lossy());
    }
    let mut output = String::with_capacity(expanded.len());
    let chars: Vec<char> = expanded.chars().collect();
    let mut index = 0;
    while index < chars.len() {
        if chars[index] == '%'
            && let Some(end) = chars[index + 1..].iter().position(|ch| *ch == '%')
        {
            let var_name: String = chars[index + 1..index + 1 + end].iter().collect();
            if !var_name.is_empty()
                && let Some(value) = get_env(&var_name)
            {
                output.push_str(&value.to_string_lossy());
                index += end + 2;
                continue;
            }
        }
        if chars[index] == '$' {
            let end = chars[index + 1..]
                .iter()
                .position(|ch| !(ch.is_ascii_alphanumeric() || *ch == '_'))
                .map(|offset| index + 1 + offset)
                .unwrap_or(chars.len());
            if end > index + 1 {
                let var_name: String = chars[index + 1..end].iter().collect();
                if let Some(value) = get_env(&var_name) {
                    output.push_str(&value.to_string_lossy());
                    index = end;
                    continue;
                }
            }
        }
        output.push(chars[index]);
        index += 1;
    }
    Ok(PathBuf::from(output))
}

fn default_log_level() -> String {
    "INFO".into()
}
fn default_keep_alive() -> String {
    "0".into()
}
fn default_max_file_bytes() -> u64 {
    50 * 1024 * 1024
}
fn default_max_extracted_chars() -> usize {
    5_000_000
}
fn default_max_docx_uncompressed_bytes() -> u64 {
    100_000_000
}
fn default_chunk_chars() -> usize {
    1200
}
fn default_chunk_overlap() -> usize {
    150
}
fn default_debounce_seconds() -> f64 {
    1.0
}
fn default_background_batch_size() -> usize {
    10
}
fn default_background_poll_seconds() -> f64 {
    1.0
}
fn default_embedding_batch_size() -> usize {
    64
}
fn default_idle_unload_seconds() -> u64 {
    120
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shipped_yaml_loads_with_required_defaults_and_limits() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../config/settings.yaml");
        let settings =
            load_settings(path).expect("current Python configuration remains compatible");
        assert_eq!(settings.models.ollama.model, "qwen2.5-coder:3b");
        assert_eq!(settings.index.max_file_bytes, 50 * 1024 * 1024);
        assert_eq!(settings.runtime.idle_unload_seconds, 120);
    }

    #[test]
    fn rejects_unsafe_chunk_limits() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../config/settings.yaml");
        let mut settings = load_settings(path).unwrap();
        settings.index.chunk_overlap = settings.index.chunk_chars;
        assert!(validate(&settings).is_err());
    }

    #[test]
    fn expands_existing_windows_style_path_macros() {
        let get_env =
            |name: &str| (name == "FILEBOT_TEST_DIR").then(|| OsString::from("C:\\Users\\Test"));
        assert_eq!(
            expand_path_with("%FILEBOT_TEST_DIR%\\Documents", get_env).unwrap(),
            PathBuf::from("C:\\Users\\Test\\Documents")
        );
        let get_env =
            |name: &str| (name == "FILEBOT_TEST_DIR").then(|| OsString::from("C:\\Users\\Test"));
        assert_eq!(
            expand_path_with("$FILEBOT_TEST_DIR/Documents", get_env).unwrap(),
            PathBuf::from("C:\\Users\\Test/Documents")
        );
    }
}
