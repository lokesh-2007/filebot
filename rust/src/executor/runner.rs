//! Explicitly approved, default-deny Windows CMD execution with bounded resources.
use super::{CommandRisk, PolicyEngine};
use crate::storage::{Database, StorageError};
#[cfg(windows)]
use std::{
    collections::VecDeque,
    io::Read,
    path::Path,
    process::{Command, ExitStatus, Stdio},
    thread,
    time::Instant,
};
use std::{io, path::PathBuf, time::Duration};
use thiserror::Error;

const OUTPUT_TAIL_LIMIT: usize = 64 * 1024;
const DISPLAY_TAIL_LIMIT: usize = 4 * 1024;
const WAIT_POLL_INTERVAL: Duration = Duration::from_millis(20);

/// Completed command output returned to the approval UI.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommandResult {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
}

#[derive(Debug, Error)]
pub enum RunnerError {
    #[error("command must have an explicitly approved audit record")]
    NotApproved,
    #[error("database operation failed: {0}")]
    Storage(#[from] StorageError),
    #[error("failed to start or monitor CMD: {0}")]
    Process(#[from] io::Error),
}

/// Executes audit rows only after an explicit UI approval.
pub struct CommandRunner {
    db: Database,
    policy_path: PathBuf,
    timeout: Duration,
}

impl CommandRunner {
    /// Build a runner with a live policy path and bounded process duration.
    pub fn new(
        db: Database,
        policy_path: impl Into<PathBuf>,
        timeout: Duration,
    ) -> Result<Self, RunnerError> {
        if timeout.is_zero() {
            return Err(RunnerError::Process(io::Error::new(
                io::ErrorKind::InvalidInput,
                "command timeout must be positive",
            )));
        }
        Ok(Self {
            db,
            policy_path: policy_path.into(),
            timeout,
        })
    }

    /// Revalidate, claim once, run through the system command processor, and audit.
    pub fn execute(&self, proposal_id: &str) -> Result<CommandResult, RunnerError> {
        let row = self
            .db
            .claim_approved_command(proposal_id)?
            .ok_or(RunnerError::NotApproved)?;
        let policy = match PolicyEngine::from_path(&self.policy_path) {
            Ok(policy) => policy,
            Err(error) => {
                return self.fail_claimed(
                    proposal_id,
                    126,
                    format!("Could not verify current command policy: {error}"),
                );
            }
        };
        let risk = policy.classify(&row.command);
        let expected_risk = match risk {
            CommandRisk::ReadOnly => "read_only",
            CommandRisk::Gated => "gated",
            CommandRisk::Blocked => "blocked",
        };
        if risk == CommandRisk::Blocked || expected_risk != row.risk_level {
            return self.fail_claimed(
                proposal_id,
                126,
                "Command is no longer allowed by the current policy".into(),
            );
        }

        let result = match self.run_child(&row.command, &row.cwd, risk) {
            Ok(result) => result,
            Err(error) => CommandResult {
                exit_code: 127,
                stdout: String::new(),
                stderr: error.to_string(),
                timed_out: false,
            },
        };
        self.db.finish_command(
            proposal_id,
            result.exit_code,
            &result.stdout,
            &result.stderr,
        )?;
        Ok(result)
    }

    fn fail_claimed(
        &self,
        proposal_id: &str,
        exit_code: i32,
        message: String,
    ) -> Result<CommandResult, RunnerError> {
        self.db
            .finish_command(proposal_id, exit_code, "", &message)?;
        Ok(CommandResult {
            exit_code,
            stdout: String::new(),
            stderr: tail_string(&message, DISPLAY_TAIL_LIMIT),
            timed_out: false,
        })
    }

    #[cfg(windows)]
    fn run_child(
        &self,
        command: &str,
        requested_cwd: &str,
        _risk: CommandRisk,
    ) -> io::Result<CommandResult> {
        use std::os::windows::process::CommandExt;

        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let system_root = std::env::var_os("SystemRoot")
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "SystemRoot is unset"))?;
        let system32 = PathBuf::from(&system_root).join("System32");
        let command_processor = system32.join("cmd.exe");
        if !command_processor.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "System32\\cmd.exe is unavailable",
            ));
        }

        let executable_command = command.split_whitespace().next().unwrap_or_default();
        let is_system_query = matches!(
            executable_command.to_ascii_lowercase().as_str(),
            "where" | "whoami" | "systeminfo" | "ipconfig" | "tasklist"
        );
        let cwd = if is_system_query {
            system32.clone()
        } else {
            let requested = if requested_cwd.trim().is_empty() {
                std::env::var_os("USERPROFILE")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| std::env::current_dir().unwrap_or_default())
            } else {
                PathBuf::from(requested_cwd)
            };
            canonical_directory(&requested)?
        };

        // Do not inherit a user-modified PATH or shell variables that could
        // shadow the small set of policy-approved Windows utilities.
        let mut child = Command::new(command_processor)
            .args(["/d", "/s", "/c", command])
            .current_dir(cwd)
            .env_clear()
            .env("SystemRoot", &system_root)
            .env("WINDIR", &system_root)
            .env("PATH", &system32)
            .env("PATHEXT", ".COM;.EXE;.BAT;.CMD")
            .env("COMSPEC", system32.join("cmd.exe"))
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()?;
        let stdout = child.stdout.take().expect("stdout was configured as piped");
        let stderr = child.stderr.take().expect("stderr was configured as piped");
        let stdout_reader = thread::spawn(move || read_tail(stdout, OUTPUT_TAIL_LIMIT));
        let stderr_reader = thread::spawn(move || read_tail(stderr, OUTPUT_TAIL_LIMIT));
        let deadline = Instant::now() + self.timeout;
        let (exit_status, timed_out): (ExitStatus, bool) = loop {
            if let Some(status) = child.try_wait()? {
                break (status, false);
            }
            if Instant::now() >= deadline {
                child.kill()?;
                break (child.wait()?, true);
            }
            thread::sleep(WAIT_POLL_INTERVAL);
        };
        let stdout = join_reader(stdout_reader)?;
        let mut stderr = join_reader(stderr_reader)?;
        let exit_code = if timed_out {
            124
        } else {
            exit_status.code().unwrap_or(125)
        };
        if timed_out {
            let note = format!("Command timed out after {}s", self.timeout.as_secs());
            stderr = append_bounded(stderr, note.as_bytes(), OUTPUT_TAIL_LIMIT);
        }
        Ok(CommandResult {
            exit_code,
            stdout: String::from_utf8_lossy(&stdout).into_owned(),
            stderr: String::from_utf8_lossy(&stderr).into_owned(),
            timed_out,
        })
    }

    #[cfg(not(windows))]
    fn run_child(
        &self,
        _command: &str,
        _requested_cwd: &str,
        _risk: CommandRisk,
    ) -> io::Result<CommandResult> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "FileBot CMD execution is available only on Windows",
        ))
    }
}

#[cfg(windows)]
fn canonical_directory(path: &Path) -> io::Result<PathBuf> {
    let canonical = path.canonicalize()?;
    if !canonical.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::NotADirectory,
            "Command working directory is not an existing directory",
        ));
    }
    Ok(canonical)
}

#[cfg(windows)]
fn read_tail(mut reader: impl Read, limit: usize) -> Vec<u8> {
    let mut tail = VecDeque::with_capacity(limit);
    let mut buffer = [0; 4096];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(count) => {
                for byte in &buffer[..count] {
                    if tail.len() == limit {
                        tail.pop_front();
                    }
                    tail.push_back(*byte);
                }
            }
        }
    }
    tail.into_iter().collect()
}

#[cfg(windows)]
fn join_reader(reader: thread::JoinHandle<Vec<u8>>) -> io::Result<Vec<u8>> {
    reader
        .join()
        .map_err(|_| io::Error::other("CMD output reader stopped unexpectedly"))
}

#[cfg(windows)]
fn append_bounded(existing: Vec<u8>, suffix: &[u8], limit: usize) -> Vec<u8> {
    let mut tail = VecDeque::with_capacity(limit);
    for byte in existing.into_iter().chain(suffix.iter().copied()) {
        if tail.len() == limit {
            tail.pop_front();
        }
        tail.push_back(byte);
    }
    tail.into_iter().collect()
}

fn tail_string(value: &str, limit: usize) -> String {
    value
        .chars()
        .rev()
        .take(limit)
        .collect::<String>()
        .chars()
        .rev()
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{executor::PolicyEngine, storage::Database};
    use std::{fs, time::Duration};

    fn runner(dir: &Path) -> (Database, CommandRunner) {
        let db = Database::new(dir.join("db.sqlite3"));
        db.initialize().unwrap();
        let policy = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../config/cmd_policy.yaml");
        (
            db.clone(),
            CommandRunner::new(db, policy, Duration::from_secs(1)).unwrap(),
        )
    }

    #[test]
    fn runner_rejects_unapproved_proposals_without_launching_cmd() {
        let temp = tempfile::tempdir().unwrap();
        let (db, runner) = runner(temp.path());
        let id = db
            .stage_command("whoami", ".", "show account", "read_only")
            .unwrap();
        assert!(matches!(runner.execute(&id), Err(RunnerError::NotApproved)));
        assert_eq!(db.command_record(&id).unwrap().unwrap().status, "pending");
    }

    #[test]
    fn runner_rechecks_policy_after_approval_and_audits_blocked_change() {
        let temp = tempfile::tempdir().unwrap();
        let db = Database::new(temp.path().join("db.sqlite3"));
        db.initialize().unwrap();
        let policy_path = temp.path().join("policy.yaml");
        fs::write(
            &policy_path,
            "version: 1\ndefault: blocked\nread_only: ['^whoami$']\n",
        )
        .unwrap();
        let runner = CommandRunner::new(db.clone(), &policy_path, Duration::from_secs(1)).unwrap();
        let id = db
            .stage_command("whoami", ".", "show account", "read_only")
            .unwrap();
        assert!(db.decide_command(&id, true).unwrap());
        fs::write(
            &policy_path,
            "version: 1\ndefault: blocked\nblocked: ['whoami']\n",
        )
        .unwrap();
        let result = runner.execute(&id).unwrap();
        assert_eq!(result.exit_code, 126);
        assert!(result.stderr.contains("no longer allowed"));
        let audit = db.command_record(&id).unwrap().unwrap();
        assert_eq!(audit.status, "failed");
        assert_eq!(audit.exit_code, Some(126));
    }

    #[test]
    fn policy_used_by_runner_is_parsed_from_the_live_file() {
        let policy = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../config/cmd_policy.yaml");
        let policy = PolicyEngine::from_path(policy).unwrap();
        assert_eq!(policy.classify("whoami"), CommandRisk::ReadOnly);
    }
}
