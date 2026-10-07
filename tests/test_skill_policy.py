from __future__ import annotations

import os
import shlex
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from azure_functions_agents._skill_policy import SkillPolicy
from azure_functions_agents.discovery.skills import SkillDescriptor, describe_skill_catalog

SCOPED_SKILLS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "config_scenarios" / "20_scoped_skills"
)


def _explicit_nested_skills() -> tuple[SkillDescriptor, ...]:
    parent = SCOPED_SKILLS_FIXTURE / "skills" / "guide-parent"
    return describe_skill_catalog([parent, parent / "guide-child"])


def _skill(root: Path, name: str) -> SkillDescriptor:
    root.mkdir(parents=True, exist_ok=True)
    return SkillDescriptor.create(name=name, path=root)


def _file(path: Path, content: str = "resource\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _command(interpreter: str, script: Path, *arguments: str) -> str:
    return shlex.join([interpreter, str(script), *arguments])


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.name == "nt" and exc.winerror in {5, 1314}:
            pytest.skip("Windows account cannot create symlinks")
        raise


@pytest.fixture
def approved_skill(tmp_path: Path) -> tuple[SkillPolicy, SkillDescriptor]:
    skill = _skill(tmp_path / "skills" / "approved", "approved")
    policy = SkillPolicy.create(
        approved=(skill,), discovered=(skill,), working_directory=tmp_path
    )
    return policy, skill


@pytest.mark.parametrize(
    ("approved_name", "denied_name"),
    [("guide-parent", "guide-child"), ("guide-child", "guide-parent")],
)
def test_most_specific_owner_has_an_independent_grant(
    approved_name: str, denied_name: str
) -> None:
    discovered = _explicit_nested_skills()
    skills = {skill.name: skill for skill in discovered}
    policy = SkillPolicy.create(
        approved=(skills[approved_name],),
        discovered=discovered,
        working_directory=SCOPED_SKILLS_FIXTURE,
    )

    assert policy.disabled_names == (denied_name,)
    assert policy.allows_read(str(skills[approved_name].path / "references" / "guide.txt"))
    assert policy.allows_shell(
        _command("python3", skills[approved_name].path / "scripts" / "report.py")
    )
    assert not policy.allows_read(str(skills[denied_name].path / "references" / "guide.txt"))
    assert not policy.allows_shell(
        _command("python3", skills[denied_name].path / "scripts" / "report.py")
    )


def test_disabling_all_skills_grants_no_helpers() -> None:
    discovered = _explicit_nested_skills()
    policy = SkillPolicy.create(
        approved=(), discovered=discovered, working_directory=SCOPED_SKILLS_FIXTURE
    )

    assert policy.disabled_names == ("guide-child", "guide-parent")
    for skill in discovered:
        assert not policy.allows_read(str(skill.path / "SKILL.md"))
        assert not policy.allows_shell(_command("python", skill.path / "scripts" / "report.py"))


def test_nested_skill_inside_parent_scripts_uses_its_own_grant(tmp_path: Path) -> None:
    parent = _skill(tmp_path / "parent", "parent")
    child = _skill(parent.path / "scripts" / "child", "child")
    script = _file(child.path / "scripts" / "report.py")
    parent_policy = SkillPolicy.create(
        approved=(parent,), discovered=(parent, child), working_directory=tmp_path
    )
    child_policy = SkillPolicy.create(
        approved=(child,), discovered=(parent, child), working_directory=tmp_path
    )

    assert not parent_policy.allows_shell(_command("python", script))
    assert child_policy.allows_shell(_command("python", script))


def test_unknown_approval_does_not_replace_discovered_ownership(tmp_path: Path) -> None:
    skill = _skill(tmp_path / "skill", "approved")
    resource = _file(skill.path / "references" / "guide.txt")
    policy = SkillPolicy.create(
        approved=(skill,), discovered=(), working_directory=tmp_path
    )

    assert not policy.allows_read(str(resource))


def test_policy_is_immutable_and_does_not_mutate_the_inventory(tmp_path: Path) -> None:
    parent = _skill(tmp_path / "parent", "parent")
    child = _skill(parent.path / "child", "child")
    discovered = (parent, child)
    policy = SkillPolicy.create(
        approved=(parent,), discovered=discovered, working_directory=tmp_path / "."
    )

    assert discovered == (parent, child)
    assert policy.discovered == discovered
    assert policy.working_directory == tmp_path.resolve()
    with pytest.raises(FrozenInstanceError):
        policy.working_directory = child.path


def test_cwd_is_only_a_resolution_base_not_a_read_grant(
    approved_skill: tuple[SkillPolicy, SkillDescriptor], tmp_path: Path
) -> None:
    policy, skill = approved_skill
    resource = _file(skill.path / "references" / "guide.txt")
    project_code = _file(tmp_path / "function_app.py")

    assert policy.allows_read(str(resource))
    assert policy.allows_read(str(resource.relative_to(tmp_path)))
    assert not policy.allows_read(str(project_code))
    assert not policy.allows_read(str(project_code.relative_to(tmp_path)))
    assert not policy.allows_read(str(skill.path))
    assert not policy.allows_read(str(resource.parent))
    assert not policy.allows_read(str(skill.path / "missing.txt"))
    assert not policy.allows_read("")
    assert not policy.allows_read("\0")


def test_read_rejects_traversal_even_when_it_resolves_inside_an_approved_root(
    approved_skill: tuple[SkillPolicy, SkillDescriptor],
) -> None:
    policy, skill = approved_skill
    resource = _file(skill.path / "references" / "guide.txt")

    assert not policy.allows_read(str(resource.parent / ".." / "references" / resource.name))
    relative = Path("skills") / "approved" / ".." / "approved" / "references" / resource.name
    assert not policy.allows_read(str(relative))


def test_string_prefix_overlap_is_not_path_containment(
    approved_skill: tuple[SkillPolicy, SkillDescriptor],
) -> None:
    policy, skill = approved_skill
    sibling = skill.path.with_name(f"{skill.path.name}-not-approved")
    resource = _file(sibling / "references" / "guide.txt")
    script = _file(sibling / "scripts" / "report.py")

    assert not policy.allows_read(str(resource))
    assert not policy.allows_shell(_command("python", script))


def test_symlink_escape_cannot_borrow_an_approved_root(
    approved_skill: tuple[SkillPolicy, SkillDescriptor], tmp_path: Path
) -> None:
    policy, skill = approved_skill
    outside = _file(tmp_path / "outside.txt")
    link = skill.path / "escape.txt"
    _symlink(link, outside)

    assert not policy.allows_read(str(link))


def test_alias_within_the_same_skill_keeps_its_canonical_owner(
    approved_skill: tuple[SkillPolicy, SkillDescriptor],
) -> None:
    policy, skill = approved_skill
    resource = _file(skill.path / "references" / "guide.txt")
    alias = skill.path / "reference-alias.txt"
    _symlink(alias, resource)

    assert policy.allows_read(str(alias))


def test_alias_outside_skill_roots_is_not_a_read_grant(
    approved_skill: tuple[SkillPolicy, SkillDescriptor], tmp_path: Path
) -> None:
    policy, skill = approved_skill
    resource = _file(skill.path / "references" / "guide.txt")
    alias = tmp_path / "project-alias.txt"
    _symlink(alias, resource)

    assert not policy.allows_read(str(alias))


def test_symlink_loop_is_denied_without_a_policy_error(
    approved_skill: tuple[SkillPolicy, SkillDescriptor],
) -> None:
    policy, skill = approved_skill
    (skill.path / "scripts").mkdir()
    loop = skill.path / "scripts" / "loop.py"
    _symlink(loop, loop)

    assert not policy.allows_read(str(loop))
    assert not policy.allows_shell(_command("python", loop))


def test_symlink_cannot_escape_into_a_different_approved_skill(tmp_path: Path) -> None:
    first = _skill(tmp_path / "first", "first")
    second = _skill(tmp_path / "second", "second")
    resource = _file(second.path / "references" / "guide.txt")
    script = _file(second.path / "scripts" / "report.py")
    (first.path / "scripts").mkdir()
    resource_alias = first.path / "resource-alias.txt"
    script_alias = first.path / "scripts" / "report.py"
    _symlink(resource_alias, resource)
    _symlink(script_alias, script)
    policy = SkillPolicy.create(
        approved=(first, second), discovered=(first, second), working_directory=tmp_path
    )

    assert policy.allows_read(str(resource))
    assert policy.allows_shell(_command("python", script))
    assert not policy.allows_read(str(resource_alias))
    assert not policy.allows_shell(_command("python", script_alias))


def test_symlink_to_excluded_nested_owner_does_not_inherit_parent_grant(tmp_path: Path) -> None:
    parent = _skill(tmp_path / "parent", "parent")
    child = _skill(parent.path / "child", "child")
    resource = _file(child.path / "references" / "guide.txt")
    script = _file(child.path / "scripts" / "report.py")
    (parent.path / "scripts").mkdir()
    resource_alias = parent.path / "resource-alias.txt"
    script_alias = parent.path / "scripts" / "report.py"
    _symlink(resource_alias, resource)
    _symlink(script_alias, script)
    policy = SkillPolicy.create(
        approved=(parent,), discovered=(parent, child), working_directory=tmp_path
    )

    assert not policy.allows_read(str(resource_alias))
    assert not policy.allows_shell(_command("python", script_alias))


def test_canonical_alias_root_with_conflicting_names_is_ambiguous(tmp_path: Path) -> None:
    skill = _skill(tmp_path / "approved", "approved")
    resource = _file(skill.path / "references" / "guide.txt")
    script = _file(skill.path / "scripts" / "report.py")
    alias = tmp_path / "alias"
    _symlink(alias, skill.path, directory=True)
    other = SkillDescriptor(name="excluded", path=alias)
    policy = SkillPolicy.create(
        approved=(skill,), discovered=(skill, other), working_directory=tmp_path
    )

    assert policy.discovered[0].path == policy.discovered[1].path
    assert not policy.allows_read(str(resource))
    assert not policy.allows_shell(_command("python", script))


def test_distinct_same_name_roots_require_their_own_path_approval(tmp_path: Path) -> None:
    first = _skill(tmp_path / "first", "shared")
    second = _skill(tmp_path / "second", "shared")
    resource = _file(first.path / "references" / "guide.txt")
    policy = SkillPolicy.create(
        approved=(first,), discovered=(first, second), working_directory=tmp_path
    )

    assert policy.allows_read(str(resource))
    assert not policy.allows_read(str(_file(second.path / "references" / "guide.txt")))


@pytest.mark.asyncio
async def test_forwarded_same_slug_candidates_keep_sdk_selected_root_permissions(tmp_path: Path):
    from unittest.mock import Mock

    from agent_framework import FileSkillsSource, SkillsSourceContext
    from copilot.rpc import PermissionDecisionApproveOnce
    from copilot.session_events import PermissionRequestRead

    from azure_functions_agents.harness.copilot_sdk._copilot_capabilities import permission_handler

    first = tmp_path / "first" / "shared"
    second = tmp_path / "second" / "shared"
    _file(first / "SKILL.md", "---\nname: wrong\ndescription: Invalid\n---\n")
    _file(second / "SKILL.md", "---\nname: shared\ndescription: Valid second\n---\n")
    resource = _file(second / "references" / "guide.txt")
    script = _file(second / "scripts" / "report.py")
    candidates = describe_skill_catalog([first, second])
    assert [item.path for item in candidates] == [first.resolve(), second.resolve()]
    loaded = await FileSkillsSource([item.path for item in candidates]).get_skills(
        SkillsSourceContext(agent=Mock())
    )
    assert [item.frontmatter.description for item in loaded] == ["Valid second"]
    policy = SkillPolicy.create(
        approved=candidates, discovered=candidates, working_directory=tmp_path
    )
    assert policy.allows_read(str(resource))
    assert policy.allows_shell(_command("python", script))
    decision = permission_handler(policy)(
        PermissionRequestRead(intention="resource", path=str(resource)),
        {"session_id": "test"},
    )
    assert decision.kind == PermissionDecisionApproveOnce.kind


@pytest.mark.parametrize(
    ("interpreter", "filename"),
    [("python", "report.py"), ("python3", "report.py"), ("bash", "report.sh")],
)
def test_literal_approved_script_forms_support_spaces_and_arguments(
    tmp_path: Path, interpreter: str, filename: str
) -> None:
    skill = _skill(tmp_path / "approved skill's", "approved")
    script = _file(skill.path / "scripts" / "nested directory" / filename)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    policy = SkillPolicy.create(
        approved=(skill,), discovered=(skill,), working_directory=unrelated
    )

    assert policy.allows_shell(
        _command(interpreter, script, "--label", "two words", "--count=2", "can't", "-c")
    )
    assert policy.allows_shell(f'{interpreter} "{script}" --label "two words"')


def test_script_arguments_are_literals_not_additional_file_grants(
    approved_skill: tuple[SkillPolicy, SkillDescriptor], tmp_path: Path
) -> None:
    policy, skill = approved_skill
    script = _file(skill.path / "scripts" / "report.py")
    output = tmp_path / "outside-output.txt"

    assert policy.allows_shell(_command("python", script, "--output", str(output)))
    assert not policy.allows_read(str(output))


@pytest.mark.parametrize(
    ("interpreter", "directory", "filename"),
    [
        ("python", "references", "report.py"),
        ("python", "scripts", "report.sh"),
        ("python3", "scripts", "report"),
        ("bash", "scripts", "report.py"),
        ("bash", "references", "report.sh"),
        ("sh", "scripts", "report.sh"),
        ("node", "scripts", "report.py"),
    ],
)
def test_only_supported_interpreters_and_own_script_tree_are_granted(
    approved_skill: tuple[SkillPolicy, SkillDescriptor],
    interpreter: str,
    directory: str,
    filename: str,
) -> None:
    policy, skill = approved_skill
    script = _file(skill.path / directory / filename)

    assert not policy.allows_shell(_command(interpreter, script))


def test_shell_requires_a_regular_absolute_script_target(
    approved_skill: tuple[SkillPolicy, SkillDescriptor],
) -> None:
    policy, skill = approved_skill
    script = _file(skill.path / "scripts" / "report.py")

    relative = Path("skills") / "approved" / "scripts" / script.name
    assert not policy.allows_shell(_command("python", relative))
    assert not policy.allows_shell(_command("python", script.parent))
    assert not policy.allows_shell(_command("python", script.parent / "missing.py"))
    assert not policy.allows_shell(_command("python", script.parent / ".." / "scripts" / script.name))


def test_scripts_symlinked_outside_the_owning_script_tree_are_denied(
    approved_skill: tuple[SkillPolicy, SkillDescriptor], tmp_path: Path
) -> None:
    policy, skill = approved_skill
    script_dir = skill.path / "scripts"
    script_dir.mkdir()
    outside = _file(tmp_path / "outside.py")
    resource = _file(skill.path / "references" / "report.py")
    _symlink(script_dir / "outside.py", outside)
    _symlink(script_dir / "resource.py", resource)

    assert not policy.allows_shell(_command("python", script_dir / "outside.py"))
    assert not policy.allows_shell(_command("python", script_dir / "resource.py"))


def test_aliased_scripts_directory_cannot_expand_the_script_grant(
    approved_skill: tuple[SkillPolicy, SkillDescriptor],
) -> None:
    policy, skill = approved_skill
    resource = _file(skill.path / "references" / "report.py")
    scripts_alias = skill.path / "scripts"
    _symlink(scripts_alias, resource.parent, directory=True)

    assert policy.allows_read(str(resource))
    assert not policy.allows_shell(_command("python", resource))
    assert not policy.allows_shell(_command("python", scripts_alias / resource.name))


@pytest.mark.parametrize(
    "command",
    [
        "",
        "pwd",
        "ls",
        "cat SKILL.md",
        "bash",
        "bash -c 'echo arbitrary'",
        "python -c 'print(1)'",
        "python3 -m http.server",
        "env python script.py",
        "command python script.py",
        "python -- script.py",
        "python -I script.py",
        "python 'unterminated",
    ],
)
def test_general_commands_are_denied_even_inside_an_approved_root(
    approved_skill: tuple[SkillPolicy, SkillDescriptor], command: str
) -> None:
    _, skill = approved_skill
    policy = SkillPolicy.create(
        approved=(skill,), discovered=(skill,), working_directory=skill.path
    )

    assert not policy.allows_shell(command)


@pytest.mark.parametrize(
    "suffix",
    [
        "; echo arbitrary",
        " && echo arbitrary",
        " || echo arbitrary",
        " | cat",
        " > output.txt",
        " 2>> output.txt",
        " < input.txt",
        " &",
        "\necho arbitrary",
        "\rwhoami",
        " $(whoami)",
        " `whoami`",
        " $HOME",
        ' "$HOME"',
        " ${HOME}",
        " $((1 + 1))",
        " <(whoami)",
        " *.txt",
        " file?.txt",
        " [ab].txt",
        " {a,b}.txt",
        " ~/input.txt",
        " # comment",
        " !history",
        " \0",
        " \x1b",
    ],
)
def test_shell_operators_expansions_and_nonliteral_forms_are_denied(
    approved_skill: tuple[SkillPolicy, SkillDescriptor], suffix: str
) -> None:
    policy, skill = approved_skill
    script = _file(skill.path / "scripts" / "report.py")

    assert not policy.allows_shell(_command("python", script) + suffix)


@pytest.mark.skipif(os.name != "posix", reason="Direct execution uses POSIX executable bits")
def test_direct_execution_requires_an_executable_supported_script(
    approved_skill: tuple[SkillPolicy, SkillDescriptor],
) -> None:
    policy, skill = approved_skill
    script = _file(skill.path / "scripts" / "report.sh", "#!/bin/bash\nprintf 'ok\\n'\n")
    script.chmod(0o600)
    assert not policy.allows_shell(shlex.quote(str(script)))

    script.chmod(0o700)
    assert policy.allows_shell(shlex.join([str(script), "--label", "two words"]))
    other = _file(skill.path / "scripts" / "binary", "#!/bin/bash\n")
    other.chmod(0o700)
    assert not policy.allows_shell(shlex.quote(str(other)))
