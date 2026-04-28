import logging
import os
from typing import Optional

from .config import get_app_root


def resolve_session_directory_for_skills() -> Optional[str]:
    """
    Resolve the skills directory at {app_root}/skills.
    """
    app_root = str(get_app_root())
    env_session_dir = os.environ.get("COPILOT_SESSION_DIRECTORY")
    if env_session_dir:
        resolved = os.path.expanduser(env_session_dir)
        if os.path.isdir(resolved):
            return resolved

    for name in ("skills", "Skills"):
        skill_path = os.path.join(app_root, name)
        if os.path.isdir(skill_path):
            return skill_path

    return None
