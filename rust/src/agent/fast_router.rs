//! Cheap intent routing before local model work is loaded.
use regex::Regex;
use std::sync::OnceLock;

/// Intent category used to restrict which agent tools are available per turn.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Intent {
    Chat,
    Search,
    Open,
    Command,
}

/// Deterministic route that runs without embeddings or an LLM.
#[derive(Clone, Copy, Debug, Default)]
pub struct FastRouter;

impl FastRouter {
    /// Route clear requests to their narrow tool surface.
    pub fn route(&self, prompt: &str) -> Intent {
        static COMMAND: OnceLock<Regex> = OnceLock::new();
        static SEARCH: OnceLock<Regex> = OnceLock::new();
        static OPEN: OnceLock<Regex> = OnceLock::new();
        let command = COMMAND.get_or_init(|| {
            Regex::new(r"(?i)\b(run|execute|use cmd|command prompt|terminal command)\b")
                .expect("static command route regex is valid")
        });
        let search = SEARCH.get_or_init(|| {
            Regex::new(r"(?i)\b(find|search|locate|where is|look for)\b")
                .expect("static search route regex is valid")
        });
        let open = OPEN.get_or_init(|| Regex::new(
            r#"(?i)\b(open|view|show)\b.{0,48}\b(file|document|pdf|docx|folder|notes?)\b|\b(open|view)\s+["']?[\w./\\:-]+\.(txt|md|pdf|docx)\b"#,
        ).expect("static open route regex is valid"));

        if command.is_match(prompt) {
            return Intent::Command;
        }
        if search.is_match(prompt) {
            return Intent::Search;
        }
        if open.is_match(prompt) {
            return Intent::Open;
        }
        Intent::Chat
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_command_before_search_and_open_intents() {
        let router = FastRouter;
        assert_eq!(router.route("Run whoami"), Intent::Command);
        assert_eq!(router.route("Search for the PDF"), Intent::Search);
        assert_eq!(router.route("Open report.pdf"), Intent::Open);
        assert_eq!(
            router.route("Explain how local indexing works"),
            Intent::Chat
        );
    }
}
