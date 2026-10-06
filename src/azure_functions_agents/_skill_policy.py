"""SDK-independent default-deny policy for native skill resource/script helpers."""

from __future__ import annotations

import os
import shlex
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .discovery.skills import SkillDescriptor

_INTERPRETER_SUFFIXES = {"python": ".py", "python3": ".py", "bash": ".sh"}
_DIRECT_SCRIPT_SUFFIXES = frozenset(_INTERPRETER_SUFFIXES.values())
_SHELL_METACHARACTERS = frozenset(";&|<>$`(){}[]*?~#!")
_SCRIPTS_DIRECTORY = "scripts"


def _canonical_descriptors(
    descriptors: tuple[SkillDescriptor, ...],
) -> tuple[SkillDescriptor, ...]:
    return tuple(
        SkillDescriptor.create(name=descriptor.name, path=descriptor.path)
        for descriptor in descriptors
    )


@dataclass(frozen=True)
class SkillPolicy:
    """Authorize helper targets only through their approved canonical skill owner.

    The catalog is frozen; cwd is only a relative-read resolution base.
    Callers log denials without paths or command text.
    Trusted scripts keep host privileges; this is not a filesystem sandbox.
    """

    approved: tuple[SkillDescriptor, ...]
    discovered: tuple[SkillDescriptor, ...]
    working_directory: Path
    disabled_names: tuple[str, ...]
    _ambiguous_roots: frozenset[Path] = field(repr=False)

    @classmethod
    def create(
        cls,
        *,
        approved: tuple[SkillDescriptor, ...],
        discovered: tuple[SkillDescriptor, ...],
        working_directory: Path,
    ) -> SkillPolicy:
        """Freeze canonical approval identities and the complete ownership catalog."""
        approved = _canonical_descriptors(approved)
        discovered = _canonical_descriptors(discovered)
        root_counts = Counter(descriptor.path for descriptor in discovered)
        name_counts = Counter(descriptor.name for descriptor in discovered)
        ambiguous_roots = frozenset(
            descriptor.path
            for descriptor in discovered
            if root_counts[descriptor.path] > 1 or name_counts[descriptor.name] > 1
        )
        disabled_names = tuple(
            sorted(
                {descriptor.name for descriptor in discovered}
                - {descriptor.name for descriptor in approved}
            )
        )
        return cls(
            approved=approved,
            discovered=discovered,
            working_directory=working_directory.resolve(),
            disabled_names=disabled_names,
            _ambiguous_roots=ambiguous_roots,
        )

    def _owner(self, path: Path) -> SkillDescriptor | None:
        owners = [
            descriptor for descriptor in self.discovered if path.is_relative_to(descriptor.path)
        ]
        if not owners:
            return None
        owner = max(owners, key=lambda descriptor: len(descriptor.path.parts))
        if owner.path in self._ambiguous_roots or not owner.path.is_dir():
            return None
        return owner

    def _owned_file(self, path_text: str) -> tuple[SkillDescriptor, Path] | None:
        try:
            path = Path(path_text)
            if ".." in path.parts:
                return None
            if not path.is_absolute():
                if path.anchor:
                    return None
                path = self.working_directory / path
            source_owner = self._owner(path)
            if source_owner is None:
                return None
            target = path.resolve(strict=True)
            if not target.is_file():
                return None
            owner = self._owner(target)
            if owner is None:
                return None
            if (source_owner.name, source_owner.path) != (owner.name, owner.path):
                return None
        except (OSError, RuntimeError, ValueError):
            return None
        if not any(
            descriptor.name == owner.name and descriptor.path == owner.path
            for descriptor in self.approved
        ):
            return None
        return owner, target

    def allows_read(self, path: str) -> bool:
        """Permit only regular files in an approved, unambiguous owning skill tree."""
        return bool(self.approved) and self._owned_file(path) is not None

    def allows_shell(self, command: str) -> bool:
        """Permit python/python3 .py, bash .sh, or direct POSIX executable .py/.sh.

        Script paths must be absolute and inside their owner's scripts tree.
        Arguments are shlex literals; shell metacharacters are denied even quoted.
        """
        if not self.approved or any(
            character in _SHELL_METACHARACTERS
            or (not character.isprintable() and character != "\t")
            for character in command
        ):
            return False
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return False
        if not tokens:
            return False
        interpreter_suffix = _INTERPRETER_SUFFIXES.get(tokens[0])
        if interpreter_suffix is not None:
            if len(tokens) < 2:
                return False
            script_text = tokens[1]
        else:
            script_text = tokens[0]
        if not Path(script_text).is_absolute():
            return False
        owned = self._owned_file(script_text)
        if owned is None:
            return False
        owner, script = owned
        try:
            scripts_root = owner.path / _SCRIPTS_DIRECTORY
            scripts_directory = scripts_root.resolve(strict=True)
            if scripts_directory != scripts_root:
                return False
            if not scripts_directory.is_dir() or not script.is_relative_to(scripts_directory):
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        if interpreter_suffix is not None:
            return script.suffix == interpreter_suffix
        return (
            os.name == "posix"
            and script.suffix in _DIRECT_SCRIPT_SUFFIXES
            and os.access(script, os.X_OK)
        )
