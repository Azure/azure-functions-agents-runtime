//! Provider resolution and `github_copilot_sdk::types::ProviderConfig` builder.
//!
//! Mirrors `src/azure_functions_agents/harness/_provider_config.py` and
//! `src/azure_functions_agents/harness/copilot_sdk/_copilot_providers.py`.

use std::sync::{Arc, LazyLock};

use async_trait::async_trait;
use github_copilot_sdk::provider_token::{
    BearerTokenError, BearerTokenProvider, ProviderTokenArgs,
};
use github_copilot_sdk::types::{AzureProviderOptions, ProviderConfig};
use regex::Regex;
use serde::{Deserialize, Serialize};
use thiserror::Error;
use url::Url;

use crate::env::EnvMap;

pub const AZURE_OPENAI_SCOPE: &str = "https://cognitiveservices.azure.com/.default";
pub const FOUNDRY_SCOPE: &str = "https://ai.azure.com/.default";
pub const OPENAI_BASE_URL: &str = "https://api.openai.com/v1";
pub const FOUNDRY_OPENAI_V1_SUFFIX: &str = "/openai/v1";

static FOUNDRY_PATH_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^/api/projects/[A-Za-z0-9_-]+$").expect("valid regex"));
static API_VERSION_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$").expect("valid regex"));

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderKind {
    OpenAi,
    AzureOpenAi,
    Foundry,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InferenceMode {
    Mock,
    Foundry,
}

impl ProviderKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::OpenAi => "openai",
            Self::AzureOpenAi => "azure_openai",
            Self::Foundry => "foundry",
        }
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ProviderError {
    #[error("{0}")]
    UnsupportedCapability(String),
}

#[derive(Debug, Clone)]
pub enum ResolvedProvider {
    OpenAi {
        api_key: String,
        default_model: Option<String>,
    },
    AzureOpenAi {
        endpoint: String,
        api_version: Option<String>,
        api_key: Option<String>,
        bearer_token: Option<String>,
        client_id: Option<String>,
        default_model: Option<String>,
    },
    Foundry {
        endpoint: String,
        bearer_token: Option<String>,
        client_id: Option<String>,
        default_model: Option<String>,
    },
}

/// Safe summary of the resolved provider for diagnostics and `initialize` responses.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderSummary {
    pub kind: ProviderKind,
    pub base_url: String,
    pub auth_mode: String,
    pub default_model: Option<String>,
}

/// Static or Managed-Identity / Azure CLI token provider implementing `BearerTokenProvider`.
struct EntraBearerTokenProvider {
    scope: String,
    static_token: Option<String>,
    client_id: Option<String>,
}

#[async_trait]
impl BearerTokenProvider for EntraBearerTokenProvider {
    async fn get_token(&self, _args: ProviderTokenArgs) -> Result<String, BearerTokenError> {
        if let Some(token) = &self.static_token {
            return Ok(token.clone());
        }
        acquire_entra_token(&self.scope, self.client_id.as_deref()).await
    }
}

async fn acquire_entra_token(
    scope: &str,
    client_id: Option<&str>,
) -> Result<String, BearerTokenError> {
    // 1. Check Azure App Service / Functions Managed Identity endpoint (IDENTITY_ENDPOINT + IDENTITY_HEADER).
    if let (Ok(endpoint), Ok(header)) = (
        std::env::var("IDENTITY_ENDPOINT"),
        std::env::var("IDENTITY_HEADER"),
    ) {
        let resource = scope.strip_suffix("/.default").unwrap_or(scope);
        let mut url = format!("{endpoint}?api-version=2019-08-01&resource={resource}");
        if let Some(cid) = client_id.filter(|s| !s.is_empty()) {
            url.push_str(&format!("&client_id={cid}"));
        }
        let client = reqwest::Client::new();
        let resp = client
            .get(&url)
            .header("X-IDENTITY-HEADER", header)
            .send()
            .await
            .map_err(|e| BearerTokenError::message(format!("Managed Identity token failed: {e}")))?;
        if resp.status().is_success() {
            let body: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| BearerTokenError::message(format!("Invalid MI token JSON: {e}")))?;
            if let Some(tok) = body.get("access_token").and_then(|v| v.as_str()) {
                return Ok(tok.to_string());
            }
        }
    }

    // 2. Fallback to Azure CLI (`az account get-access-token`) for local developer environments.
    let output = tokio::process::Command::new("az")
        .args([
            "account",
            "get-access-token",
            "--scope",
            scope,
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ])
        .output()
        .await
        .map_err(|e| BearerTokenError::message(format!("Failed to invoke Azure CLI for token: {e}")))?;

    if !output.status.success() {
        return Err(BearerTokenError::message(
            "Could not acquire Entra token from Managed Identity or Azure CLI.",
        ));
    }
    let token = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if token.is_empty() {
        return Err(BearerTokenError::message("Acquired Entra token was empty."));
    }
    Ok(token)
}

impl ResolvedProvider {
    /// Resolve provider and default model from environment variables, matching Python precedence:
    /// 1. Explicit `AZURE_FUNCTIONS_AGENTS_PROVIDER` (`foundry`, `azure_openai`, `openai`)
    /// 2. Auto-detect: `FOUNDRY_PROJECT_ENDPOINT` -> `AZURE_OPENAI_ENDPOINT` -> `OPENAI_API_KEY`
    pub fn from_env(env: &EnvMap) -> Result<Self, ProviderError> {
        let explicit = env.get_trimmed("AZURE_FUNCTIONS_AGENTS_PROVIDER");
        let kind = match explicit {
            Some("foundry") => ProviderKind::Foundry,
            Some("azure_openai") => ProviderKind::AzureOpenAi,
            Some("openai") => ProviderKind::OpenAi,
            Some(other) => {
                return Err(ProviderError::UnsupportedCapability(format!(
                    "Unsupported AZURE_FUNCTIONS_AGENTS_PROVIDER='{other}'. Expected openai, azure_openai, or foundry."
                )));
            }
            None => {
                if env.get_trimmed("FOUNDRY_PROJECT_ENDPOINT").is_some() {
                    ProviderKind::Foundry
                } else if env.get_trimmed("AZURE_OPENAI_ENDPOINT").is_some() {
                    ProviderKind::AzureOpenAi
                } else if env.get_trimmed("OPENAI_API_KEY").is_some() {
                    ProviderKind::OpenAi
                } else {
                    return Err(ProviderError::UnsupportedCapability(
                        "Copilot runtime requires FOUNDRY_PROJECT_ENDPOINT, AZURE_OPENAI_ENDPOINT, or OPENAI_API_KEY.".to_string(),
                    ));
                }
            }
        };

        let global_model = env
            .get_trimmed("AZURE_FUNCTIONS_AGENTS_MODEL")
            .map(ToString::to_string);
        let client_id = env.get_trimmed("AZURE_CLIENT_ID").map(ToString::to_string);
        let static_bearer = env
            .get_trimmed("AZURE_BEARER_TOKEN")
            .map(ToString::to_string);

        match kind {
            ProviderKind::OpenAi => {
                let api_key = env
                    .get_trimmed("OPENAI_API_KEY")
                    .ok_or_else(|| {
                        ProviderError::UnsupportedCapability(
                            "Copilot OpenAI requires OPENAI_API_KEY.".to_string(),
                        )
                    })?
                    .to_string();
                let default_model = global_model.or_else(|| Some("gpt-4o".to_string()));
                Ok(Self::OpenAi {
                    api_key,
                    default_model,
                })
            }
            ProviderKind::AzureOpenAi => {
                let endpoint = validate_azure_openai_endpoint(env)?;
                let api_version = validate_azure_openai_api_version(env)?;
                let api_key = env
                    .get_trimmed("AZURE_OPENAI_API_KEY")
                    .map(ToString::to_string);
                let default_model = global_model.or_else(|| {
                    env.get_trimmed("AZURE_OPENAI_DEPLOYMENT")
                        .map(ToString::to_string)
                });
                Ok(Self::AzureOpenAi {
                    endpoint,
                    api_version,
                    api_key,
                    bearer_token: static_bearer,
                    client_id,
                    default_model,
                })
            }
            ProviderKind::Foundry => {
                let endpoint = validate_foundry_endpoint(env)?;
                let default_model = global_model
                    .or_else(|| env.get_trimmed("FOUNDRY_MODEL").map(ToString::to_string));
                Ok(Self::Foundry {
                    endpoint,
                    bearer_token: static_bearer,
                    client_id,
                    default_model,
                })
            }
        }
    }

    pub fn from_foundry_env(env: &EnvMap) -> Result<Self, ProviderError> {
        Self::from_env(
            &env.clone()
                .with_overrides([("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")]),
        )
    }

    pub fn kind(&self) -> ProviderKind {
        match self {
            Self::OpenAi { .. } => ProviderKind::OpenAi,
            Self::AzureOpenAi { .. } => ProviderKind::AzureOpenAi,
            Self::Foundry { .. } => ProviderKind::Foundry,
        }
    }

    pub fn default_model(&self) -> Option<&str> {
        match self {
            Self::OpenAi { default_model, .. }
            | Self::AzureOpenAi { default_model, .. }
            | Self::Foundry { default_model, .. } => default_model.as_deref(),
        }
    }

    pub fn effective_model(&self, agent_model: Option<&str>) -> Result<String, ProviderError> {
        agent_model
            .filter(|m| !m.trim().is_empty())
            .or_else(|| self.default_model())
            .map(ToString::to_string)
            .ok_or_else(|| {
                ProviderError::UnsupportedCapability(
                    "Copilot runtime requires an explicit model in .agent.md, agents.config.yaml, or environment.".to_string(),
                )
            })
    }

    pub fn summary(&self) -> ProviderSummary {
        match self {
            Self::OpenAi { default_model, .. } => ProviderSummary {
                kind: ProviderKind::OpenAi,
                base_url: OPENAI_BASE_URL.to_string(),
                auth_mode: "OPENAI_API_KEY".to_string(),
                default_model: default_model.clone(),
            },
            Self::AzureOpenAi {
                endpoint,
                api_key,
                default_model,
                ..
            } => ProviderSummary {
                kind: ProviderKind::AzureOpenAi,
                base_url: endpoint.clone(),
                auth_mode: if api_key.is_some() {
                    "AZURE_OPENAI_API_KEY".to_string()
                } else {
                    "Azure credential".to_string()
                },
                default_model: default_model.clone(),
            },
            Self::Foundry {
                endpoint,
                default_model,
                ..
            } => ProviderSummary {
                kind: ProviderKind::Foundry,
                base_url: format!("{endpoint}{FOUNDRY_OPENAI_V1_SUFFIX}"),
                auth_mode: "Azure credential".to_string(),
                default_model: default_model.clone(),
            },
        }
    }

    /// Build the `github_copilot_sdk::types::ProviderConfig` for a session turn.
    pub fn to_sdk_provider_config(&self, model: &str) -> ProviderConfig {
        match self {
            Self::OpenAi { api_key, .. } => ProviderConfig::new(OPENAI_BASE_URL)
                .with_provider_type("openai")
                .with_wire_api("responses")
                .with_model_id(model)
                .with_wire_model(model)
                .with_bearer_token(api_key.clone()),
            Self::AzureOpenAi {
                endpoint,
                api_version,
                api_key,
                bearer_token,
                client_id,
                ..
            } => {
                let mut cfg = ProviderConfig::new(endpoint.clone())
                    .with_provider_type("azure")
                    .with_wire_api("responses")
                    .with_model_id(model)
                    .with_wire_model(model);
                if let Some(ver) = api_version {
                    cfg = cfg.with_azure(AzureProviderOptions {
                        api_version: Some(ver.clone()),
                    });
                }
                if let Some(key) = api_key {
                    cfg = cfg.with_api_key(key.clone());
                } else {
                    cfg = cfg.with_bearer_token_provider(Arc::new(EntraBearerTokenProvider {
                        scope: AZURE_OPENAI_SCOPE.to_string(),
                        static_token: bearer_token.clone(),
                        client_id: client_id.clone(),
                    }));
                }
                cfg
            }
            Self::Foundry {
                endpoint,
                bearer_token,
                client_id,
                ..
            } => ProviderConfig::new(format!("{endpoint}{FOUNDRY_OPENAI_V1_SUFFIX}"))
                .with_provider_type("openai")
                .with_wire_api("responses")
                .with_model_id(model)
                .with_wire_model(model)
                .with_bearer_token_provider(Arc::new(EntraBearerTokenProvider {
                    scope: FOUNDRY_SCOPE.to_string(),
                    static_token: bearer_token.clone(),
                    client_id: client_id.clone(),
                })),
        }
    }
}

pub fn has_foundry_intent(env: &EnvMap) -> bool {
    env.get_trimmed("AZURE_FUNCTIONS_AGENTS_PROVIDER")
        .is_some_and(|provider| provider.eq_ignore_ascii_case("foundry"))
        || env.get_trimmed("FOUNDRY_PROJECT_ENDPOINT").is_some()
        || env.get_trimmed("FOUNDRY_MODEL").is_some()
}

pub fn mock_inference_requested(env: &EnvMap, explicit_override: Option<bool>) -> bool {
    explicit_override == Some(true)
        || env.get_trimmed("FHS_MOCK_LLM").is_some_and(|value| {
            matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes")
        })
}

pub fn resolve_inference(
    env: &EnvMap,
    explicit_mock: Option<bool>,
) -> Result<(InferenceMode, Option<ResolvedProvider>), ProviderError> {
    if mock_inference_requested(env, explicit_mock) || !has_foundry_intent(env) {
        return Ok((InferenceMode::Mock, None));
    }

    let provider = ResolvedProvider::from_foundry_env(env)?;
    Ok((InferenceMode::Foundry, Some(provider)))
}

fn validate_azure_openai_endpoint(env: &EnvMap) -> Result<String, ProviderError> {
    let diag = "Copilot Azure OpenAI requires a valid host-only AZURE_OPENAI_ENDPOINT.";
    let raw = env
        .get_trimmed("AZURE_OPENAI_ENDPOINT")
        .ok_or_else(|| ProviderError::UnsupportedCapability(diag.to_string()))?
        .trim_end_matches('/');
    let url =
        Url::parse(raw).map_err(|_| ProviderError::UnsupportedCapability(diag.to_string()))?;
    if url.scheme() != "https"
        || url.host_str().is_none_or(|h| h.is_empty())
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || !matches!(url.path(), "" | "/")
    {
        return Err(ProviderError::UnsupportedCapability(diag.to_string()));
    }
    Ok(raw.to_string())
}

fn validate_foundry_endpoint(env: &EnvMap) -> Result<String, ProviderError> {
    let invalid_shape = "Copilot Foundry preview requires FOUNDRY_PROJECT_ENDPOINT in the form https://<resource>.services.ai.azure.com/api/projects/<project>.";
    let raw = env
        .get_trimmed("FOUNDRY_PROJECT_ENDPOINT")
        .ok_or_else(|| ProviderError::UnsupportedCapability(invalid_shape.to_string()))?
        .trim_end_matches('/');
    let url = Url::parse(raw).map_err(|_| {
        ProviderError::UnsupportedCapability(
            "Copilot Foundry preview requires a valid HTTPS project endpoint.".to_string(),
        )
    })?;
    let host_ok = url
        .host_str()
        .is_some_and(|h| h.ends_with(".services.ai.azure.com"));
    let port_ok = matches!(url.port(), None | Some(443));
    if url.scheme() != "https"
        || !host_ok
        || !port_ok
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || !FOUNDRY_PATH_RE.is_match(url.path())
    {
        return Err(ProviderError::UnsupportedCapability(
            invalid_shape.to_string(),
        ));
    }
    Ok(raw.to_string())
}

fn validate_azure_openai_api_version(env: &EnvMap) -> Result<Option<String>, ProviderError> {
    let Some(val) = env.get_trimmed("AZURE_OPENAI_API_VERSION") else {
        return Ok(None);
    };
    if !API_VERSION_RE.is_match(val) {
        return Err(ProviderError::UnsupportedCapability(
            "Copilot Azure OpenAI requires AZURE_OPENAI_API_VERSION to be a valid API-version token."
                .to_string(),
        ));
    }
    Ok(Some(val.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_foundry_provider_and_builds_sdk_config() {
        let env = EnvMap::from_iter([
            (
                "FOUNDRY_PROJECT_ENDPOINT",
                "https://my-res.services.ai.azure.com/api/projects/proj_1",
            ),
            ("FOUNDRY_MODEL", "gpt-4.1"),
        ]);
        let provider = ResolvedProvider::from_env(&env).unwrap();
        assert_eq!(provider.kind(), ProviderKind::Foundry);
        assert_eq!(provider.default_model(), Some("gpt-4.1"));
        let sdk = provider.to_sdk_provider_config("gpt-4.1");
        assert_eq!(sdk.provider_type.as_deref(), Some("openai"));
        assert_eq!(sdk.wire_api.as_deref(), Some("responses"));
        assert_eq!(
            sdk.base_url,
            "https://my-res.services.ai.azure.com/api/projects/proj_1/openai/v1"
        );
        assert!(sdk.bearer_token_provider.is_some());
    }

    #[test]
    fn resolves_azure_openai_with_api_key_and_api_version() {
        let env = EnvMap::from_iter([
            ("AZURE_OPENAI_ENDPOINT", "https://my-aoai.openai.azure.com/"),
            ("AZURE_OPENAI_API_KEY", "secret-key"),
            ("AZURE_OPENAI_API_VERSION", "2025-03-01-preview"),
            ("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-deploy"),
        ]);
        let provider = ResolvedProvider::from_env(&env).unwrap();
        assert_eq!(provider.kind(), ProviderKind::AzureOpenAi);
        let sdk = provider.to_sdk_provider_config("gpt-4o-deploy");
        assert_eq!(sdk.provider_type.as_deref(), Some("azure"));
        assert_eq!(sdk.base_url, "https://my-aoai.openai.azure.com");
        assert_eq!(sdk.api_key.as_deref(), Some("secret-key"));
        assert_eq!(
            sdk.azure.as_ref().and_then(|a| a.api_version.as_deref()),
            Some("2025-03-01-preview")
        );
    }

    #[test]
    fn rejects_invalid_foundry_endpoint_shape() {
        let env = EnvMap::from_iter([(
            "FOUNDRY_PROJECT_ENDPOINT",
            "https://example.com/wrong/path",
        )]);
        let err = ResolvedProvider::from_env(&env).unwrap_err();
        assert!(err.to_string().contains("FOUNDRY_PROJECT_ENDPOINT"));
    }

    #[test]
    fn foundry_intent_is_limited_to_nonblank_foundry_settings() {
        assert!(!has_foundry_intent(&EnvMap::default()));
        assert!(!has_foundry_intent(&EnvMap::from_iter([
            ("AZURE_FUNCTIONS_AGENTS_MODEL", "model"),
            ("DEFAULT_AGENT_MODEL", "model"),
            ("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com"),
        ])));
        assert!(has_foundry_intent(&EnvMap::from_iter([(
            "AZURE_FUNCTIONS_AGENTS_PROVIDER",
            " FoUnDrY ",
        )])));
        assert!(has_foundry_intent(&EnvMap::from_iter([(
            "FOUNDRY_PROJECT_ENDPOINT",
            "endpoint",
        )])));
        assert!(has_foundry_intent(&EnvMap::from_iter([(
            "FOUNDRY_MODEL",
            "model"
        )])));
        assert!(!has_foundry_intent(&EnvMap::from_iter([(
            "FOUNDRY_MODEL",
            "  "
        )])));
    }

    #[test]
    fn explicit_mock_override_takes_precedence_over_foundry_intent() {
        let env = EnvMap::from_iter([
            ("FOUNDRY_PROJECT_ENDPOINT", "not-a-live-endpoint"),
            ("FHS_MOCK_LLM", "false"),
        ]);
        assert!(mock_inference_requested(&env, Some(true)));
        assert!(!mock_inference_requested(&env, Some(false)));
        assert!(!mock_inference_requested(&env, None));
        assert!(mock_inference_requested(
            &EnvMap::from_iter([("FHS_MOCK_LLM", "yes")]),
            None
        ));
    }

    #[test]
    fn foundry_mode_uses_precedence_and_accepts_opaque_deployment_names() {
        let env = EnvMap::from_iter([
            (
                "FOUNDRY_PROJECT_ENDPOINT",
                "https://my-res.services.ai.azure.com/api/projects/proj_1",
            ),
            ("AZURE_FUNCTIONS_AGENTS_MODEL", "global-model"),
            ("FOUNDRY_MODEL", "foundry-model"),
        ]);
        let provider = ResolvedProvider::from_foundry_env(&env).unwrap();
        assert_eq!(
            provider
                .effective_model(Some("resolved-agent-model"))
                .unwrap(),
            "resolved-agent-model"
        );
        assert_eq!(provider.effective_model(None).unwrap(), "global-model");

        let foundry_model_only = EnvMap::from_iter([
            (
                "FOUNDRY_PROJECT_ENDPOINT",
                "https://my-res.services.ai.azure.com/api/projects/proj_1",
            ),
            ("FOUNDRY_MODEL", "mock-model"),
        ]);
        let provider = ResolvedProvider::from_foundry_env(&foundry_model_only).unwrap();
        assert_eq!(provider.effective_model(None).unwrap(), "mock-model");
    }

    #[test]
    fn foundry_model_without_endpoint_fails_live_configuration() {
        let env = EnvMap::from_iter([("FOUNDRY_MODEL", "deployment")]);
        let err = ResolvedProvider::from_foundry_env(&env).unwrap_err();
        assert!(err.to_string().contains("FOUNDRY_PROJECT_ENDPOINT"));
    }

    #[test]
    fn foundry_provider_setting_without_endpoint_fails_live_configuration() {
        let env = EnvMap::from_iter([("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")]);
        let err = ResolvedProvider::from_foundry_env(&env).unwrap_err();
        assert!(err.to_string().contains("FOUNDRY_PROJECT_ENDPOINT"));
    }

    #[test]
    fn inference_mode_defaults_to_mock_and_selects_foundry_only_on_intent() {
        let env = EnvMap::from_iter([
            ("DEFAULT_AGENT_MODEL", "configured-model"),
            ("AZURE_FUNCTIONS_AGENTS_MODEL", "global-model"),
        ]);
        let (mode, provider) = resolve_inference(&env, None).unwrap();
        assert_eq!(mode, InferenceMode::Mock);
        assert!(provider.is_none());

        let foundry_env = EnvMap::from_iter([
            (
                "FOUNDRY_PROJECT_ENDPOINT",
                "https://my-res.services.ai.azure.com/api/projects/proj_1",
            ),
            ("FOUNDRY_MODEL", "deployment"),
        ]);
        let (mode, provider) = resolve_inference(&foundry_env, None).unwrap();
        assert_eq!(mode, InferenceMode::Foundry);
        assert_eq!(
            provider.unwrap().effective_model(None).unwrap(),
            "deployment"
        );
    }

    #[test]
    fn explicit_mock_prevents_validation_or_contact_of_placeholder_foundry_settings() {
        let env = EnvMap::from_iter([
            ("FOUNDRY_PROJECT_ENDPOINT", "not-a-valid-endpoint"),
            ("FOUNDRY_MODEL", "deployment"),
        ]);
        let (mode, provider) = resolve_inference(&env, Some(true)).unwrap();
        assert_eq!(mode, InferenceMode::Mock);
        assert!(provider.is_none());
    }

    #[test]
    fn partial_foundry_intent_fails_instead_of_falling_back_to_mock() {
        let env = EnvMap::from_iter([("FOUNDRY_MODEL", "deployment")]);
        let err = resolve_inference(&env, None).unwrap_err();
        assert!(err.to_string().contains("FOUNDRY_PROJECT_ENDPOINT"));
    }

    #[test]
    fn foundry_endpoint_without_any_model_fails_live_configuration() {
        let env = EnvMap::from_iter([(
            "FOUNDRY_PROJECT_ENDPOINT",
            "https://my-res.services.ai.azure.com/api/projects/proj_1",
        )]);
        let (mode, provider) = resolve_inference(&env, None).unwrap();
        assert_eq!(mode, InferenceMode::Foundry);
        let err = provider.unwrap().effective_model(None).unwrap_err();
        assert!(err.to_string().contains("explicit model"));
    }
}
