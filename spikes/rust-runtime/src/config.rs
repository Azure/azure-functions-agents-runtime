//! `.agent.md` and `agents.config.yaml` discovery, frontmatter parsing, and merging.
//!
//! Mirrors `src/azure_functions_agents/config/loader.py`, `schema.py`, and `merge.py`.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_yml::Value;
use thiserror::Error;

use crate::env::{EnvMap, resolve_env_vars_in_yaml, substitute_env_vars_in_text};

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("I/O error reading {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("{path}: invalid YAML frontmatter or config: {message}")]
    Yaml { path: PathBuf, message: String },
    #[error("duplicate agent slug '{slug}' discovered in {first} and {second}")]
    DuplicateSlug {
        slug: String,
        first: PathBuf,
        second: PathBuf,
    },
}

/// Filter controlling which discovered skills or tools are enabled for an agent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum CapabilityFilter {
    /// `true` enables all discovered items; `false` disables all.
    All(bool),
    /// Explicit allowlist of item names.
    Selected(Vec<String>),
}

impl Default for CapabilityFilter {
    fn default() -> Self {
        Self::All(true)
    }
}

impl CapabilityFilter {
    pub fn is_enabled(&self, name: &str) -> bool {
        match self {
            Self::All(enabled) => *enabled,
            Self::Selected(names) => names.iter().any(|item| item == name),
        }
    }
}

/// Trigger configuration inside `.agent.md` frontmatter.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TriggerSpec {
    #[serde(rename = "type", default = "default_trigger_type")]
    pub trigger_type: String,
    #[serde(default)]
    pub route: Option<String>,
    #[serde(default)]
    pub methods: Option<Vec<String>>,
    #[serde(default)]
    pub auth_level: Option<String>,
    #[serde(default)]
    pub queue_name: Option<String>,
    #[serde(default)]
    pub schedule: Option<String>,
}

fn default_trigger_type() -> String {
    "http".to_string()
}

impl Default for TriggerSpec {
    fn default() -> Self {
        Self {
            trigger_type: default_trigger_type(),
            route: None,
            methods: None,
            auth_level: None,
            queue_name: None,
            schedule: None,
        }
    }
}

/// Global defaults loaded from `agents.config.yaml`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GlobalConfig {
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub timeout: Option<u64>,
    #[serde(default)]
    pub tools: Option<CapabilityFilter>,
    #[serde(default)]
    pub skills: Option<CapabilityFilter>,
    #[serde(default)]
    pub mcp_servers: Option<CapabilityFilter>,
}

/// Raw frontmatter + body loaded from a single `*.agent.md` file.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentFrontmatter {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub timeout: Option<u64>,
    #[serde(default)]
    pub trigger: Option<TriggerSpec>,
    #[serde(default)]
    pub tools: Option<CapabilityFilter>,
    #[serde(default)]
    pub skills: Option<CapabilityFilter>,
    #[serde(default)]
    pub mcp_servers: Option<CapabilityFilter>,
    #[serde(default)]
    pub response_schema: Option<serde_json::Value>,
    #[serde(default)]
    pub response_example: Option<serde_json::Value>,
    #[serde(default = "default_true")]
    pub substitute_variables: bool,
}

fn default_true() -> bool {
    true
}

impl Default for AgentFrontmatter {
    fn default() -> Self {
        Self {
            name: None,
            description: None,
            model: None,
            timeout: None,
            trigger: None,
            tools: None,
            skills: None,
            mcp_servers: None,
            response_schema: None,
            response_example: None,
            substitute_variables: true,
        }
    }
}

/// Resolved agent definition ready for registration and execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResolvedAgent {
    pub slug: String,
    pub name: String,
    pub description: String,
    pub instructions: String,
    pub model: Option<String>,
    pub timeout_seconds: u64,
    pub trigger: TriggerSpec,
    pub tools: CapabilityFilter,
    pub skills: CapabilityFilter,
    pub mcp_servers: CapabilityFilter,
    pub response_schema: Option<serde_json::Value>,
    pub response_example: Option<serde_json::Value>,
    pub source_file: PathBuf,
    pub is_main: bool,
}

/// Split markdown text into optional YAML frontmatter (`--- ... ---`) and markdown body.
pub fn split_frontmatter(raw: &str) -> (Option<&str>, &str) {
    let trimmed = raw.strip_prefix('\u{feff}').unwrap_or(raw);
    let Some(after_open) = trimmed
        .strip_prefix("---\r\n")
        .or_else(|| trimmed.strip_prefix("---\n"))
    else {
        return (None, trimmed);
    };

    // Find closing `---` on its own line.
    let mut offset = 0usize;
    for line in after_open.split_inclusive('\n') {
        let stripped = line.trim_end_matches(['\r', '\n']);
        if stripped == "---" {
            let yaml_part = &after_open[..offset];
            let body_part = &after_open[offset + line.len()..];
            let body_trimmed = body_part
                .strip_prefix("\r\n")
                .or_else(|| body_part.strip_prefix('\n'))
                .unwrap_or(body_part);
            return (Some(yaml_part), body_trimmed);
        }
        offset += line.len();
    }

    (None, trimmed)
}

/// Determine whether a filename is a recognized agent file and return `(slug, is_main)`.
pub fn classify_agent_filename(filename: &str) -> Option<(String, bool)> {
    if filename.starts_with('.') {
        return None;
    }
    let lower = filename.to_ascii_lowercase();
    if lower == "agent.md" || lower == "claude.md" {
        return Some(("main".to_string(), true));
    }
    for suffix in [".agent.md", ".claude.md"] {
        if lower.ends_with(suffix) && filename.len() > suffix.len() {
            let prefix = &filename[..filename.len() - suffix.len()];
            let is_main = prefix.eq_ignore_ascii_case("main");
            let slug = slugify(prefix);
            if !slug.is_empty() {
                return Some((slug, is_main));
            }
        }
    }
    None
}

/// Normalize an identifier into a URL/function-safe slug (`[a-z0-9_-]`).
pub fn slugify(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.trim().chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch.to_ascii_lowercase());
        } else if (ch == '-' || ch == '_' || ch == ' ') && !out.ends_with('-') && !out.is_empty() {
            out.push(if ch == '_' { '_' } else { '-' });
        }
    }
    out.trim_matches(['-', '_']).to_string()
}

/// Load `agents.config.yaml` from `app_root` if present.
pub fn load_global_config(app_root: &Path, env: &EnvMap) -> Result<GlobalConfig, ConfigError> {
    let path = app_root.join("agents.config.yaml");
    if !path.is_file() {
        return Ok(GlobalConfig::default());
    }
    let raw = fs::read_to_string(&path).map_err(|source| ConfigError::Io {
        path: path.clone(),
        source,
    })?;
    if raw.trim().is_empty() {
        return Ok(GlobalConfig::default());
    }
    let value: Value = serde_yml::from_str(&raw).map_err(|err| ConfigError::Yaml {
        path: path.clone(),
        message: err.to_string(),
    })?;
    let resolved = resolve_env_vars_in_yaml(value, env);
    serde_yml::from_value(resolved).map_err(|err| ConfigError::Yaml {
        path,
        message: err.to_string(),
    })
}

/// Parse a single `.agent.md` file and merge it with `GlobalConfig`.
pub fn load_agent_file(
    source_file: &Path,
    global: &GlobalConfig,
    env: &EnvMap,
) -> Result<ResolvedAgent, ConfigError> {
    let filename = source_file
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("agent.md");
    let (file_slug, is_main) =
        classify_agent_filename(filename).unwrap_or_else(|| ("main".to_string(), true));

    let raw = fs::read_to_string(source_file).map_err(|source| ConfigError::Io {
        path: source_file.to_path_buf(),
        source,
    })?;

    let (frontmatter_str, body_str) = split_frontmatter(&raw);

    let (frontmatter, substitute_vars) = if let Some(yaml_text) = frontmatter_str {
        if yaml_text.trim().is_empty() {
            (AgentFrontmatter::default(), true)
        } else {
            let value: Value =
                serde_yml::from_str(yaml_text).map_err(|err| ConfigError::Yaml {
                    path: source_file.to_path_buf(),
                    message: err.to_string(),
                })?;
            let sub_vars = extract_substitute_variables(&value);
            let normalized = if sub_vars {
                resolve_env_vars_in_yaml(value, env)
            } else {
                value
            };
            let parsed: AgentFrontmatter =
                serde_yml::from_value(normalized).map_err(|err| ConfigError::Yaml {
                    path: source_file.to_path_buf(),
                    message: err.to_string(),
                })?;
            (parsed, sub_vars)
        }
    } else {
        (AgentFrontmatter::default(), true)
    };

    let instructions = if substitute_vars {
        substitute_env_vars_in_text(body_str, env)
    } else {
        body_str.to_string()
    }
    .trim()
    .to_string();

    let display_name = frontmatter
        .name
        .clone()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| file_slug.clone());

    let description = frontmatter.description.unwrap_or_default();
    let model = frontmatter.model.or_else(|| global.model.clone());
    let timeout_seconds = frontmatter.timeout.or(global.timeout).unwrap_or(300);
    let trigger = frontmatter.trigger.unwrap_or_default();
    let tools = frontmatter
        .tools
        .or_else(|| global.tools.clone())
        .unwrap_or_default();
    let skills = frontmatter
        .skills
        .or_else(|| global.skills.clone())
        .unwrap_or_default();
    let mcp_servers = frontmatter
        .mcp_servers
        .or_else(|| global.mcp_servers.clone())
        .unwrap_or_default();

    let canonical_source = source_file
        .canonicalize()
        .unwrap_or_else(|_| source_file.to_path_buf());

    Ok(ResolvedAgent {
        slug: file_slug,
        name: display_name,
        description,
        instructions,
        model,
        timeout_seconds,
        trigger,
        tools,
        skills,
        mcp_servers,
        response_schema: frontmatter.response_schema,
        response_example: frontmatter.response_example,
        source_file: canonical_source,
        is_main,
    })
}

fn extract_substitute_variables(value: &Value) -> bool {
    let Some(map) = value.as_mapping() else {
        return true;
    };
    let Some(flag) = map.get(Value::String("substitute_variables".to_string())) else {
        return true;
    };
    match flag {
        Value::Bool(b) => *b,
        Value::String(s) => !matches!(
            s.trim().to_ascii_lowercase().as_str(),
            "false" | "0" | "no" | "n"
        ),
        _ => true,
    }
}

/// Discover and resolve all agents in `app_root` and `{app_root}/agents` (or `Agents`).
pub fn discover_agents(
    app_root: &Path,
    env: &EnvMap,
) -> Result<BTreeMap<String, ResolvedAgent>, ConfigError> {
    let global = load_global_config(app_root, env)?;
    let mut candidate_files = collect_agent_files_in_dir(app_root)?;
    for dir_name in ["agents", "Agents"] {
        let dir = app_root.join(dir_name);
        if dir.is_dir() {
            candidate_files.extend(collect_agent_files_in_dir(&dir)?);
            break;
        }
    }
    candidate_files.sort();

    let mut resolved_map: BTreeMap<String, ResolvedAgent> = BTreeMap::new();
    for file in candidate_files {
        let agent = load_agent_file(&file, &global, env)?;
        if let Some(existing) = resolved_map.get(&agent.slug) {
            return Err(ConfigError::DuplicateSlug {
                slug: agent.slug,
                first: existing.source_file.clone(),
                second: agent.source_file,
            });
        }
        resolved_map.insert(agent.slug.clone(), agent);
    }
    Ok(resolved_map)
}

fn collect_agent_files_in_dir(dir: &Path) -> Result<Vec<PathBuf>, ConfigError> {
    let mut files = Vec::new();
    let entries = fs::read_dir(dir).map_err(|source| ConfigError::Io {
        path: dir.to_path_buf(),
        source,
    })?;
    for entry in entries {
        let entry = entry.map_err(|source| ConfigError::Io {
            path: dir.to_path_buf(),
            source,
        })?;
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let Some(name) = path.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if classify_agent_filename(name).is_some() {
            files.push(path);
        }
    }
    Ok(files)
}

#[cfg(test)]
mod tests {
    use tempfile::TempDir;

    use super::*;

    #[test]
    fn parses_agent_md_with_env_vars_and_global_config() {
        let tmp = TempDir::new().unwrap();
        fs::write(
            tmp.path().join("agents.config.yaml"),
            "model: $DEFAULT_MODEL\ntimeout: 120\n",
        )
        .unwrap();

        let agents_dir = tmp.path().join("agents");
        fs::create_dir_all(&agents_dir).unwrap();
        fs::write(
            agents_dir.join("reviewer.agent.md"),
            "---\nname: PR Reviewer ($ENV_STAGE)\ndescription: Reviews PRs\nskills:\n  - code-style\n---\nReview for $TEAM_NAME.\n```python\nprint(\"$TEAM_NAME\")\n```\n",
        )
        .unwrap();

        let env = EnvMap::from_iter([
            ("DEFAULT_MODEL", "gpt-4.1"),
            ("ENV_STAGE", "prod"),
            ("TEAM_NAME", "AzureFunctions"),
        ]);

        let agents = discover_agents(tmp.path(), &env).unwrap();
        assert_eq!(agents.len(), 1);
        let reviewer = &agents["reviewer"];
        assert_eq!(reviewer.name, "PR Reviewer (prod)");
        assert_eq!(reviewer.model.as_deref(), Some("gpt-4.1"));
        assert_eq!(reviewer.timeout_seconds, 120);
        assert!(reviewer.skills.is_enabled("code-style"));
        assert!(!reviewer.skills.is_enabled("other-skill"));
        assert!(reviewer.instructions.contains("Review for AzureFunctions."));
        assert!(reviewer.instructions.contains("print(\"$TEAM_NAME\")"));
    }
}
