"""Legacy Miles session alias; OpenEnv TB APIs are loaded only when requested."""
import sys
from . import miles_session as _implementation
sys.modules[__name__] = _implementation
