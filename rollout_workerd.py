"""Compatibility alias for dsec.rollout.worker; canonical implementation lives there."""
import sys
from dsec.rollout import worker as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    _implementation.main()
