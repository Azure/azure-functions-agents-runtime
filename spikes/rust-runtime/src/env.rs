//! Environment variable resolution and `$VAR` / `%VAR%` substitution.
//!
//! Mirrors `src/azure_functions_agents/config/env.py` in the Python reference runtime.

use std::collections::HashMap;
use std::sync::LazyLock;

use regex::Regex;
use serde_yml::Value;

static ESCAPED_DOLLAR_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\$\$([A-Za-z_][A-Za-z0-9_]*)").expect("valid regex"));
static ESCAPED_PERCENT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"%%([A-Za-z_][A-Za-z0-9_]*)%%").expect("valid regex"));
static INLINE_DOLLAR_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\$([A-Za-z_][A-Za-z0-9_]*)").expect("valid regex"));
static INLINE_PERCENT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"%([A-Za-z_][A-Za-z0-9_]*)%").expect("valid regex"));

const LITERAL_DOLLAR_SENTINEL: &str = "\x00AF_LITERAL_DOLLAR:";
const LITERAL_PERCENT_SENTINEL: &str = "\x00AF_LITERAL_PERCENT:";
const LITERAL_SENTINEL_SUFFIX: &str = "\x00";

static RESTORE_DOLLAR_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        "{}([A-Za-z_][A-Za-z0-9_]*){}",
        regex::escape(LITERAL_DOLLAR_SENTINEL),
        regex::escape(LITERAL_SENTINEL_SUFFIX)
    ))
    .expect("valid regex")
});

static RESTORE_PERCENT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        "{}([A-Za-z_][A-Za-z0-9_]*){}",
        regex::escape(LITERAL_PERCENT_SENTINEL),
        regex::escape(LITERAL_SENTINEL_SUFFIX)
    ))
    .expect("valid regex")
});

/// Snapshot + override map of environment variables for deterministic config resolution.
#[derive(Debug, Clone, Default)]
pub struct EnvMap {
    vars: HashMap<String, String>,
}

impl EnvMap {
    /// Capture current process environment variables.
    pub fn from_process() -> Self {
        Self {
            vars: std::env::vars().collect(),
        }
    }

    /// Construct from an explicit map of variables.
    #[allow(clippy::should_implement_trait)]
    pub fn from_iter<I, K, V>(iter: I) -> Self
    where
        I: IntoIterator<Item = (K, V)>,
        K: Into<String>,
        V: Into<String>,
    {
        Self {
            vars: iter
                .into_iter()
                .map(|(k, v)| (k.into(), v.into()))
                .collect(),
        }
    }

    /// Merge overrides on top of this environment map.
    pub fn with_overrides<I, K, V>(mut self, overrides: I) -> Self
    where
        I: IntoIterator<Item = (K, V)>,
        K: Into<String>,
        V: Into<String>,
    {
        for (k, v) in overrides {
            self.vars.insert(k.into(), v.into());
        }
        self
    }

    /// Insert or overwrite a single environment variable.
    pub fn insert(&mut self, key: impl Into<String>, value: impl Into<String>) {
        self.vars.insert(key.into(), value.into());
    }

    /// Get the raw value of an environment variable.
    pub fn get_raw(&self, key: &str) -> Option<&str> {
        self.vars.get(key).map(String::as_str)
    }

    /// Get a trimmed non-empty value of an environment variable.
    pub fn get_trimmed(&self, key: &str) -> Option<&str> {
        self.vars
            .get(key)
            .map(|v| v.trim())
            .filter(|v| !v.is_empty())
    }
}

/// Perform inline `$VAR` and `%VAR%` substitution across a single string value,
/// preserving escaped `$$VAR` and `%%VAR%%` literals.
pub fn substitute_env_vars_in_value(value: &str, env: &EnvMap) -> String {
    let masked_dollar = ESCAPED_DOLLAR_RE.replace_all(value, |caps: &regex::Captures<'_>| {
        format!(
            "{}{}{}",
            LITERAL_DOLLAR_SENTINEL, &caps[1], LITERAL_SENTINEL_SUFFIX
        )
    });
    let masked_both = ESCAPED_PERCENT_RE.replace_all(&masked_dollar, |caps: &regex::Captures<'_>| {
        format!(
            "{}{}{}",
            LITERAL_PERCENT_SENTINEL, &caps[1], LITERAL_SENTINEL_SUFFIX
        )
    });

    let sub_dollar = INLINE_DOLLAR_RE.replace_all(&masked_both, |caps: &regex::Captures<'_>| {
        env.get_raw(&caps[1])
            .map(ToString::to_string)
            .unwrap_or_else(|| caps[0].to_string())
    });
    let sub_both = INLINE_PERCENT_RE.replace_all(&sub_dollar, |caps: &regex::Captures<'_>| {
        env.get_raw(&caps[1])
            .map(ToString::to_string)
            .unwrap_or_else(|| caps[0].to_string())
    });

    let restored_dollar = RESTORE_DOLLAR_RE.replace_all(&sub_both, "$$$1");
    RESTORE_PERCENT_RE
        .replace_all(&restored_dollar, "%$1%")
        .into_owned()
}

/// Perform inline env-var substitution in markdown instructions outside fenced code blocks (` ``` `).
pub fn substitute_env_vars_in_text(text: &str, env: &EnvMap) -> String {
    let mut parts: Vec<String> = text.split("```").map(ToString::to_string).collect();
    for (idx, part) in parts.iter_mut().enumerate() {
        if idx % 2 == 0 {
            *part = substitute_env_vars_in_value(part, env);
        }
    }
    parts.join("```")
}

/// Recursively substitute env vars in string values within a parsed YAML value.
pub fn resolve_env_vars_in_yaml(value: Value, env: &EnvMap) -> Value {
    match value {
        Value::String(s) => Value::String(substitute_env_vars_in_value(&s, env)),
        Value::Sequence(seq) => Value::Sequence(
            seq.into_iter()
                .map(|item| resolve_env_vars_in_yaml(item, env))
                .collect(),
        ),
        Value::Mapping(map) => Value::Mapping(
            map.into_iter()
                .map(|(k, v)| (k, resolve_env_vars_in_yaml(v, env)))
                .collect(),
        ),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn substitutes_dollar_and_percent_and_preserves_escaped() {
        let env = EnvMap::from_iter([("GREETING", "Hello"), ("TARGET", "World")]);
        let input = "$GREETING %TARGET%! Escaped: $$GREETING and %%TARGET%% and $MISSING.";
        let output = substitute_env_vars_in_value(input, &env);
        assert_eq!(
            output,
            "Hello World! Escaped: $GREETING and %TARGET% and $MISSING."
        );
    }

    #[test]
    fn skips_fenced_code_blocks_in_markdown() {
        let env = EnvMap::from_iter([("MODEL_NAME", "gpt-4.1")]);
        let md = "Use $MODEL_NAME.\n```bash\necho $MODEL_NAME\n```\nDone with %MODEL_NAME%.";
        let output = substitute_env_vars_in_text(md, &env);
        assert_eq!(
            output,
            "Use gpt-4.1.\n```bash\necho $MODEL_NAME\n```\nDone with gpt-4.1."
        );
    }
}
