"""Compatibility alias for dsec.runtime.admission_guard; canonical implementation lives there."""
import sys
from dsec.runtime import admission_guard as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
