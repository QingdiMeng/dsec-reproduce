"""Legacy module alias; the implementation is a transport-only SDK."""
import sys
from dsec.sdk import client as _implementation
sys.modules[__name__] = _implementation
