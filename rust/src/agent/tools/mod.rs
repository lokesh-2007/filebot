//! Narrow, audited tools exposed to the local chat agent.

mod cmd_proposal;

pub use cmd_proposal::{CommandProposal, ProposalError, ProposeCmdTool};
