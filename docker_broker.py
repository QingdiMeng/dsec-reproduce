"""Compatibility alias for dsec.runtime.backends.docker_broker; canonical implementation lives there."""
import sys
from dsec.runtime.backends import docker_broker as _implementation

# Share identity and patched attributes with existing imports.
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    _implementation.main()
