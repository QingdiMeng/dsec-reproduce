"""Legacy opt-in alias for the optional TB2.1 application."""
import sys
from dsec.compat.applications import require_tb21
sys.modules[__name__] = require_tb21("tb2_task_runtime")
