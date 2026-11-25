//! Agent tool that classifies and stages a CMD proposal; it never executes it.
use crate::{
    executor::{CommandRisk, PolicyEngine, PolicyError},
    storage::{Database, StorageError},
};
use std::{env, path::PathBuf};
use thiserror::Error;

const MAX_PROPOSAL_CHARS: usize = 4096;
const MAX_REASON_CHARS: usize = 1000;
const MAX_CWD_CHARS: usize = 4096;

#[derive(Debug, Error)]
pub enum ProposalError {
    #[error("command is too long to stage")]
    CommandTooLong,
    #[error("command reason or working directory is too long")]
    MetadataTooLong,
    #[error("command policy error: {0}")]
    Policy(#[from] PolicyError),
    #[error("database operation failed: {0}")]
    Storage(#[from] StorageError),
}

/// Safe model tool return value for rendering in the popover.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommandProposal {
    pub proposal_id: String,
    pub status: String,
    pub risk_level: String,
}

/// Classifies every command and stores an audit row without starting a process.
pub struct ProposeCmdTool {
    policy: PolicyEngine,
    db: Database,
}

impl ProposeCmdTool {
    /// Create the proposal-only tool from the live command policy.
    pub fn new(
        policy_path: impl AsRef<std::path::Path>,
        db: Database,
    ) -> Result<Self, ProposalError> {
        Ok(Self {
            policy: PolicyEngine::from_path(policy_path)?,
            db,
        })
    }

    /// Stage a proposal. Only a separate UI decision can make a safe row executable.
    pub fn propose(
        &self,
        command: &str,
        reason: &str,
        working_directory: Option<&str>,
    ) -> Result<CommandProposal, ProposalError> {
        if command.chars().count() > MAX_PROPOSAL_CHARS {
            return Err(ProposalError::CommandTooLong);
        }
        if reason.chars().count() > MAX_REASON_CHARS
            || working_directory.is_some_and(|path| path.chars().count() > MAX_CWD_CHARS)
        {
            return Err(ProposalError::MetadataTooLong);
        }
        let risk = self.policy.classify(command);
        let (risk_level, status) = match risk {
            CommandRisk::ReadOnly => ("read_only", "pending"),
            CommandRisk::Gated => ("gated", "pending"),
            CommandRisk::Blocked => ("blocked", "blocked"),
        };
        let cwd = working_directory
            .map(PathBuf::from)
            .or_else(|| env::var_os("USERPROFILE").map(PathBuf::from))
            .unwrap_or_else(|| env::current_dir().unwrap_or_default())
            .to_string_lossy()
            .into_owned();
        let proposal_id = self.db.stage_command(command, &cwd, reason, risk_level)?;
        if status == "blocked" {
            // The database marks blocked risk as blocked at insert time; this
            // assertion protects the UI contract if that invariant changes.
            let row = self.db.command_record(&proposal_id)?;
            if row.as_ref().is_none_or(|row| row.status != "blocked") {
                return Err(StorageError::InvalidValue(
                    "blocked proposal entered the approval queue".into(),
                )
                .into());
            }
        }
        Ok(CommandProposal {
            proposal_id,
            status: status.into(),
            risk_level: risk_level.into(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proposal_tool_stages_safe_commands_and_never_approves_them() {
        let temp = tempfile::tempdir().unwrap();
        let db = Database::new(temp.path().join("state.sqlite3"));
        db.initialize().unwrap();
        let policy = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../config/cmd_policy.yaml");
        let tool = ProposeCmdTool::new(policy, db.clone()).unwrap();
        let proposal = tool
            .propose("whoami", "show the current account", None)
            .unwrap();
        assert_eq!(proposal.status, "pending");
        assert_eq!(proposal.risk_level, "read_only");
        assert!(
            db.claim_approved_command(&proposal.proposal_id)
                .unwrap()
                .is_none()
        );
        assert_eq!(
            db.command_record(&proposal.proposal_id)
                .unwrap()
                .unwrap()
                .status,
            "pending"
        );
    }

    #[test]
    fn blocked_commands_are_logged_outside_the_approval_queue() {
        let temp = tempfile::tempdir().unwrap();
        let db = Database::new(temp.path().join("state.sqlite3"));
        db.initialize().unwrap();
        let policy = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../config/cmd_policy.yaml");
        let tool = ProposeCmdTool::new(policy, db.clone()).unwrap();
        let proposal = tool.propose("format C:", "format a disk", None).unwrap();
        assert_eq!(proposal.status, "blocked");
        assert!(db.pending_commands().unwrap().is_empty());
        assert_eq!(
            db.command_record(&proposal.proposal_id)
                .unwrap()
                .unwrap()
                .status,
            "blocked"
        );
    }
}
