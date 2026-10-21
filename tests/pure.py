"""Load the Home Assistant-free modules by path.

The package's __init__ imports Home Assistant. models.py and api.py do not;
this registers a stand-in package whose __path__ is the component directory,
so their relative imports resolve and they load as written.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "sslreverseproxy"
FIXTURES = ROOT / "tests" / "fixtures"

_PACKAGE = "sslreverseproxy_pure"


def load(name: str) -> types.ModuleType:
    """Import custom_components/sslreverseproxy/<name>.py without Home Assistant."""
    if _PACKAGE not in sys.modules:
        package = types.ModuleType(_PACKAGE)
        package.__path__ = [str(COMPONENT)]
        sys.modules[_PACKAGE] = package
    full = f"{_PACKAGE}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, COMPONENT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


def recorded(name: str) -> tuple[int, Any]:
    """A recorded answer from a demo control plane: (status, body)."""
    with open(FIXTURES / name, encoding="utf-8") as fh:
        data = json.load(fh)
    return int(data["status"]), data["body"]


def body(name: str) -> Any:
    return recorded(name)[1]
