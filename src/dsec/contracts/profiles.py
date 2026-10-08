"""Declared rollout mechanisms for the E1–E5 single-host prototypes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
import re


class UnsupportedProfile(ValueError):
    pass


@dataclass(frozen=True)
class FrameworkProfile:
    backend: str = "microvm"
    environment: str = "fixed_ext4"
    environment_id: str | None = None
    storage: str = "local"
    memory: str = "baseline"
    cpu_qos: str = "default"
    lifecycle: str = "full_snapshot_stop"
    verifier_storage: str | None = None

    @classmethod
    def from_dict(cls, value):
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("profile must be an object")
        unknown = set(value) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError("Unknown profile fields: " + ", ".join(sorted(unknown)))
        if any((not isinstance(item, str) or not item)
                for key, item in value.items()
                if key not in ("environment_id", "verifier_storage")):
            raise ValueError("Profile values must be nonempty strings")
        if (value.get("verifier_storage") is not None and
                value["verifier_storage"] not in ("local", "threefs_lazy")):
            raise ValueError("verifier_storage must be local or threefs_lazy")
        if value.get("environment_id") is not None and (not isinstance(value["environment_id"], str)
                 or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", value["environment_id"])):
            raise ValueError("environment_id must be a catalog identifier")
        return cls(**value)

    def validate_runtime(self):
        if (self.backend == "microvm" and self.environment == "erofs_layers"
                and self.environment_id is not None and
                re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", self.environment_id)
                and self.storage in ("local", "threefs_lazy") and self.memory == "baseline"
                and (self.verifier_storage is None or
                     self.verifier_storage in ("local", "threefs_lazy"))
                and self.cpu_qos == "default" and self.lifecycle == "full_snapshot_stop"):
            return self
        if (self.backend == "container" and self.environment in ("erofs_split", "erofs_layers")
                and self.environment_id is not None and
                re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", self.environment_id)
                and self.storage in ("local", "threefs_lazy") and
                self.memory == "baseline" and self.lifecycle == "stop" and
                (self.cpu_qos == "default" or re.fullmatch(
                    r"(?:ls_core_cookie:[2468]|be_sched_idle:[2-9])", self.cpu_qos))):
            return self
        supported = FrameworkProfile()
        if (self.backend == "microvm" and self.environment == "e3_mixed"
                and self.storage == "local" and self.cpu_qos == "default"
                and self.lifecycle == "full_snapshot_stop"
                and self.memory in ("baseline", "dax", "damon_fpr", "dax_damon_fpr")):
            return self
        container = FrameworkProfile(backend="container", environment="erofs_overlay",
                                     lifecycle="stop")
        e2_local = FrameworkProfile(backend="container", environment="e2_full_erofs",
                                    lifecycle="stop")
        e2_remote = FrameworkProfile(backend="container", environment="e2_full_erofs",
                                     storage="threefs_lazy", lifecycle="stop")
        if self in (container, e2_local, e2_remote):
            return self
        if (re.fullmatch(r"(?:ls_core_cookie:[2468]|be_sched_idle:[2-9])", self.cpu_qos)
                and replace(self, cpu_qos="default") in (container, e2_local, e2_remote)):
            return self
        for field in fields(self):
            name = field.name
            if getattr(self, name) != getattr(supported, name):
                raise UnsupportedProfile(
                    f"{name}={getattr(self, name)!r} is not integrated; "
                    f"current value is {getattr(supported, name)!r}")
        return self

    def as_dict(self):
        result = asdict(self)
        if result["environment_id"] is None:
            del result["environment_id"]
        if result["verifier_storage"] is None:
            del result["verifier_storage"]
        return result


MECHANISM_ROADMAP = {
    "E1": {"field": "environment", "candidate": "erofs_overlay",
           "status": "integrated_container_prototype"},
    "E2": {"field": "storage", "candidate": "threefs_lazy",
           "status": "integrated_container_prototype"},
    "E3": {"field": "memory", "candidate": "dax_damon_fpr",
           "status": "integrated_microvm_prototype"},
    "E4": {"field": "cpu_qos", "candidate": "be_sched_idle_ls_cookie",
           "status": "integrated_container_prototype"},
    "E5": {"field": "lifecycle", "candidate": "full_snapshot_stop",
           "status": "integrated_prototype"},
}
