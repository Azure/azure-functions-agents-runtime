//! Skill directory discovery (`skills/**/SKILL.md` up to depth 2) and agent filtering.
//!
//! Mirrors `src/azure_functions_agents/discovery/skills.py`.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::config::CapabilityFilter;

pub const SKILL_FILE_NAME: &str = "SKILL.md";
pub const SKILL_ROOT_SEARCH_DEPTH: usize = 2;

/// Immutable directory identity for a discovered skill root.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SkillDescriptor {
    pub name: String,
    pub path: PathBuf,
}

/// Filtered skill set for a single resolved agent.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AgentSkillPlan {
    pub enabled: Vec<SkillDescriptor>,
    pub disabled_names: Vec<String>,
}

/// Discover skill directories beneath `{app_root}/skills` (or `Skills`).
///
/// Searches through two child levels (`SKILL_ROOT_SEARCH_DEPTH = 2`), stopping
/// at each directory that contains `SKILL.md` without descending further.
pub fn discover_skills(app_root: &Path) -> Vec<SkillDescriptor> {
    let Some(skills_dir) = resolve_skills_dir(app_root) else {
        return Vec::new();
    };
    let mut skill_files = Vec::new();
    visit_skill_dir(&skills_dir, 0, &mut skill_files);
    skill_files.sort();

    let mut seen = BTreeSet::new();
    let mut descriptors = Vec::new();
    for skill_file in skill_files {
        let Some(parent) = skill_file.parent() else {
            continue;
        };
        let canonical = parent
            .canonicalize()
            .unwrap_or_else(|_| parent.to_path_buf());
        let Some(name) = canonical.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        let key = (name.to_string(), canonical.clone());
        if seen.insert(key) {
            descriptors.push(SkillDescriptor {
                name: name.to_string(),
                path: canonical,
            });
        }
    }
    descriptors
}

/// Apply an agent's `skills` filter to the discovered catalog of skills.
pub fn filter_skills_for_agent(
    catalog: &[SkillDescriptor],
    filter: &CapabilityFilter,
) -> AgentSkillPlan {
    let mut enabled = Vec::new();
    let mut disabled_names = Vec::new();
    for skill in catalog {
        if filter.is_enabled(&skill.name) {
            enabled.push(skill.clone());
        } else {
            disabled_names.push(skill.name.clone());
        }
    }
    AgentSkillPlan {
        enabled,
        disabled_names,
    }
}

fn resolve_skills_dir(app_root: &Path) -> Option<PathBuf> {
    for candidate_name in ["skills", "Skills"] {
        let candidate = app_root.join(candidate_name);
        if candidate.is_dir() {
            return Some(candidate);
        }
    }
    None
}

fn visit_skill_dir(directory: &Path, depth: usize, out: &mut Vec<PathBuf>) {
    let skill_file = directory.join(SKILL_FILE_NAME);
    if skill_file.is_file() {
        out.push(skill_file);
        return;
    }
    if depth >= SKILL_ROOT_SEARCH_DEPTH {
        return;
    }
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    let mut children: Vec<PathBuf> = entries
        .filter_map(|e| e.ok().map(|entry| entry.path()))
        .filter(|p| p.is_dir())
        .collect();
    children.sort();
    for child in children {
        visit_skill_dir(&child, depth + 1, out);
    }
}

#[cfg(test)]
mod tests {
    use tempfile::TempDir;

    use super::*;

    #[test]
    fn discovers_skills_up_to_depth_two_and_stops_at_skill_root() {
        let tmp = TempDir::new().unwrap();
        let skills_root = tmp.path().join("skills");

        // Depth 1 skill with a nestedSKILL.md that should NOT be discovered separately.
        let alpha = skills_root.join("alpha");
        let alpha_nested = alpha.join("nested");
        fs::create_dir_all(&alpha_nested).unwrap();
        fs::write(alpha.join("SKILL.md"), "# Alpha skill").unwrap();
        fs::write(alpha_nested.join("SKILL.md"), "# Nested inside alpha").unwrap();

        // Depth 2 categorized skill.
        let beta = skills_root.join("category").join("beta");
        fs::create_dir_all(&beta).unwrap();
        fs::write(beta.join("SKILL.md"), "# Beta skill").unwrap();

        // Depth 3 skill (too deep, should be ignored).
        let too_deep = skills_root.join("a").join("b").join("gamma");
        fs::create_dir_all(&too_deep).unwrap();
        fs::write(too_deep.join("SKILL.md"), "# Gamma skill").unwrap();

        let discovered = discover_skills(tmp.path());
        let names: Vec<&str> = discovered.iter().map(|d| d.name.as_str()).collect();
        assert_eq!(names, vec!["alpha", "beta"]);
    }
}
