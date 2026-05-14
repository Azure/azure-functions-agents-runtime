from __future__ import annotations

import sys
import types
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

PACKAGE_DIR = SRC_DIR / "azure_functions_agents"
if "azure_functions_agents" not in sys.modules:
    package = types.ModuleType("azure_functions_agents")
    package.__path__ = [str(PACKAGE_DIR)]
    package.__file__ = str(PACKAGE_DIR / "__init__.py")
    sys.modules["azure_functions_agents"] = package
