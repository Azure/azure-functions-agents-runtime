//! `fhs-runtime`: Cross-language Azure Functions Hosted Skills runtime over stdio.
//!
//! Provides:
//! - `.agent.md` and `agents.config.yaml` discovery & frontmatter parsing with `$VAR` / `%VAR%` substitution (`config`, `env`)
//! - `skills/` directory discovery (`SKILL.md` up to depth 2) and per-agent filtering (`skills`)
//! - Provider resolution (`Foundry`, `AzureOpenAI`, `OpenAI`) and `github_copilot_sdk::types::ProviderConfig` construction (`providers`)
//! - Virtualized `SessionFsProvider` backed by Azure Blob Storage (`AzureWebJobsStorage` / `AzureWebJobsStorage__blobServiceUri`) or Local disk (`session_fs`)
//! - Bi-directional JSON-RPC 2.0 over stdio supporting `HostedSkill` invocation, streaming events, and in-app `tool.call` callbacks (`protocol`, `engine`)

pub mod config;
pub mod engine;
pub mod env;
pub mod protocol;
pub mod providers;
pub mod session_fs;
pub mod skills;
