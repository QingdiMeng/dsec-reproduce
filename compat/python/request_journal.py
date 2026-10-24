"""Compatibility alias for dsec.runtime.requests; canonical implementation lives there."""
import sys
from dsec.runtime import requests as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
