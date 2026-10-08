"""Compatibility alias for dsec.runtime.resource_rpc; canonical implementation lives there."""
import sys
from dsec.runtime import resource_rpc as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation
