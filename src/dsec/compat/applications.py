"""Opt-in legacy application loading; ordinary core imports need no cases."""
from importlib import import_module


def require_tb21(module):
    try:
        return import_module("dsec_tb21_case." + module)
    except ModuleNotFoundError as exc:
        if exc.name == "dsec_tb21_case" or exc.name == "dsec_tb21_case." + module:
            raise RuntimeError("TB2.1 requires the optional application; "
                               "install ./apps/tb21 from the same checkout") from exc
        raise
