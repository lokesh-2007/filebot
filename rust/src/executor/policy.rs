//! Default-deny CMD policy shared by proposal and execution paths.
use regex::Regex;
use serde::Deserialize;
use std::{fs, path::Path};
use thiserror::Error;

const MAX_COMMAND_BYTES: usize = 16 * 1024;
const MAX_COMMAND_CHARS: usize = 4096;

/// Policy result for a proposed Windows command.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CommandRisk {
    /// A small allowlisted query; it still requires explicit user approval.
    ReadOnly,
    /// An allowlisted mutation that always requires approval.
    Gated,
    /// Unrecognized, malformed, or explicitly prohibited command.
    Blocked,
}

#[derive(Debug, Error)]
pub enum PolicyError {
    #[error("cannot read command policy {path}: {source}")]
    Read {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("cannot parse command policy: {0}")]
    Parse(#[from] serde_yaml_ng::Error),
    #[error("command policy default must be 'blocked'")]
    DefaultMustBlock,
    #[error("command policy version must be 1")]
    UnsupportedVersion,
    #[error("invalid regular expression in {section}: {source}")]
    Regex {
        section: &'static str,
        #[source]
        source: regex::Error,
    },
}

#[derive(Deserialize)]
struct PolicyDocument {
    version: u32,
    default: String,
    #[serde(default)]
    blocked: Vec<String>,
    #[serde(default)]
    read_only: Vec<String>,
    #[serde(default)]
    gated: Vec<String>,
}

/// Compiled, immutable command policy. Patterns are read from the same YAML file
/// used by the current app and are reloaded immediately before each execution.
pub struct PolicyEngine {
    blocked: Vec<Regex>,
    read_only: Vec<Regex>,
    gated: Vec<Regex>,
}

impl PolicyEngine {
    /// Load and validate a policy file. A malformed or permissive policy fails closed.
    pub fn from_path(path: impl AsRef<Path>) -> Result<Self, PolicyError> {
        let path = path.as_ref();
        let source = fs::read_to_string(path).map_err(|source| PolicyError::Read {
            path: path.display().to_string(),
            source,
        })?;
        Self::from_yaml(&source)
    }

    /// Parse policy YAML and compile every pattern before it can classify commands.
    pub fn from_yaml(source: &str) -> Result<Self, PolicyError> {
        let doc: PolicyDocument = serde_yaml_ng::from_str(source)?;
        if doc.version != 1 {
            return Err(PolicyError::UnsupportedVersion);
        }
        if doc.default != "blocked" {
            return Err(PolicyError::DefaultMustBlock);
        }
        Ok(Self {
            blocked: compile_patterns("blocked", doc.blocked)?,
            read_only: compile_patterns("read_only", doc.read_only)?,
            gated: compile_patterns("gated", doc.gated)?,
        })
    }

    /// Classify a complete command string. Control bytes, multiline input,
    /// excessive commands, blocks, and unknown commands are denied.
    pub fn classify(&self, command: &str) -> CommandRisk {
        if command.len() > MAX_COMMAND_BYTES
            || command.chars().count() > MAX_COMMAND_CHARS
            || command.is_empty()
            || command.chars().any(|ch| ch.is_control())
        {
            return CommandRisk::Blocked;
        }

        let command = command.trim();
        if command.is_empty() || self.blocked.iter().any(|pattern| pattern.is_match(command)) {
            return CommandRisk::Blocked;
        }
        if self
            .read_only
            .iter()
            .any(|pattern| matches_full_command(pattern, command))
        {
            return CommandRisk::ReadOnly;
        }
        if self
            .gated
            .iter()
            .any(|pattern| matches_full_command(pattern, command))
        {
            return CommandRisk::Gated;
        }
        CommandRisk::Blocked
    }
}

fn matches_full_command(pattern: &Regex, command: &str) -> bool {
    pattern
        .find(command)
        .is_some_and(|matched| matched.start() == 0 && matched.end() == command.len())
}

fn compile_patterns(
    section: &'static str,
    sources: Vec<String>,
) -> Result<Vec<Regex>, PolicyError> {
    sources
        .into_iter()
        .map(|source| {
            Regex::new(&source).map_err(|error| PolicyError::Regex {
                section,
                source: error,
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy() -> PolicyEngine {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../config/cmd_policy.yaml");
        PolicyEngine::from_path(path).expect("shipped policy parses")
    }

    #[test]
    fn shipped_policy_classifies_allowlisted_commands_and_denies_unknowns() {
        let policy = policy();
        assert_eq!(policy.classify("whoami"), CommandRisk::ReadOnly);
        assert_eq!(policy.classify("mkdir test-folder"), CommandRisk::Gated);
        assert_eq!(policy.classify("format C:"), CommandRisk::Blocked);
        assert_eq!(policy.classify("anything else"), CommandRisk::Blocked);
    }

    #[test]
    fn blocked_patterns_take_priority_over_permissive_patterns() {
        let policy = PolicyEngine::from_yaml(
            "version: 1\ndefault: blocked\nread_only: ['^whoami.*$']\nblocked: ['admin']\n",
        )
        .expect("policy is valid");
        assert_eq!(policy.classify("whoami admin"), CommandRisk::Blocked);
    }

    #[test]
    fn allow_patterns_must_match_the_entire_command() {
        let policy =
            PolicyEngine::from_yaml("version: 1\ndefault: blocked\nread_only: ['whoami']\n")
                .expect("policy is valid");
        assert_eq!(policy.classify("whoami"), CommandRisk::ReadOnly);
        assert_eq!(policy.classify("xwhoami"), CommandRisk::Blocked);
        assert_eq!(policy.classify("whoami extra"), CommandRisk::Blocked);
    }

    #[test]
    fn rejects_chaining_control_characters_and_long_commands() {
        let policy = policy();
        assert_eq!(
            policy.classify("whoami\r\ndel important.txt"),
            CommandRisk::Blocked
        );
        assert_eq!(
            policy.classify("whoami & del important.txt"),
            CommandRisk::Blocked
        );
        assert_eq!(
            policy.classify(&"x".repeat(MAX_COMMAND_CHARS + 1)),
            CommandRisk::Blocked
        );
    }

    #[test]
    fn permissive_defaults_and_invalid_regexes_fail_to_load() {
        assert!(matches!(
            PolicyEngine::from_yaml("version: 1\ndefault: gated\n"),
            Err(PolicyError::DefaultMustBlock)
        ));
        assert!(matches!(
            PolicyEngine::from_yaml("version: 1\ndefault: blocked\ngated: ['[']"),
            Err(PolicyError::Regex { .. })
        ));
    }
}
