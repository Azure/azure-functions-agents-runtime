"""Shared session-id validation pattern.

The session id is used as a filename component by the runner's history
providers, so anything outside this safe set is rejected. Kept in a tiny,
dependency-free module so both the runner and the endpoint layer can import it
without the endpoint layer eagerly importing the heavy ``runner`` module.
"""

from __future__ import annotations

import re

# Validated session-id pattern. The id is used as a filename component, so
# refuse anything that could escape the session directory.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
