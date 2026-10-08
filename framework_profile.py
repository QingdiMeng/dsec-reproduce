"""Compatibility alias for dsec.contracts.profiles; canonical implementation lives there."""
import sys
from dsec.contracts import profiles as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
