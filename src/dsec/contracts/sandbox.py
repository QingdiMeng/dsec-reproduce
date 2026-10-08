"""libdsec-style run specifications; host artifacts are resolved by Edge."""
from __future__ import annotations
from dataclasses import dataclass
import math
import re


class UnsupportedCapability(ValueError):
    """A requested sandbox feature is not implemented by this backend."""


@dataclass(frozen=True)
class DSecMicroVMRunArgs:
    ttl_running_stop: float = 300
    environment_id: str | None = None
    storage: str = "local"
    memory_profile: str = "baseline"
    memory_limit_mb: int | None = None
    cpu_cores_limit: int | None = None
    network_rules: dict | None = None
    init_user: str | None = None
    verifier_storage: str | None = None
    baseline_id: str | None = None

    def service_args(self):
        if self.baseline_id is not None and not re.fullmatch(r"[0-9a-f]{12}", self.baseline_id):
            raise ValueError("Invalid baseline_id")
        # The current daemon boots one configured image with fixed 1-vCPU/256-MiB
        # resources. It must not silently accept a policy it cannot enforce.
        unsupported = ("memory_limit_mb", "cpu_cores_limit", "network_rules", "init_user")
        for name in unsupported:
            if getattr(self, name) is not None:
                raise UnsupportedCapability(f"microVM backend does not support {name} yet")
        if not isinstance(self.ttl_running_stop, (int, float)) or isinstance(self.ttl_running_stop, bool):
            raise ValueError("ttl_running_stop must be a positive number")
        if not math.isfinite(self.ttl_running_stop) or self.ttl_running_stop <= 0:
            raise ValueError("ttl_running_stop must be finite and positive")
        if self.storage not in ("local", "threefs_lazy"):
            raise UnsupportedCapability("Unsupported microVM storage")
        if self.environment_id is None:
            if (self.memory_profile != "baseline" or self.verifier_storage is not None or
                    self.storage != "local"):
                raise UnsupportedCapability("Nonbaseline memory requires e3-mixed environment")
            return {"idle_ttl_seconds": self.ttl_running_stop,
                    **({"baseline_id": self.baseline_id} if self.baseline_id else {})}
        if self.environment_id != "e3-mixed" or self.memory_profile not in (
                "baseline", "dax", "damon_fpr", "dax_damon_fpr"):
            if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", self.environment_id)
                    or self.memory_profile != "baseline"):
                raise UnsupportedCapability("Unsupported microVM environment/memory profile")
        if self.verifier_storage is not None:
            if (not self.environment_id.startswith("tb2-") or
                    self.verifier_storage not in ("local", "threefs_lazy")):
                raise UnsupportedCapability("Verifier storage requires a TB2 microVM and local/threefs_lazy")
        if self.storage != "local" and (self.environment_id == "e3-mixed" or
                                         self.environment_id.startswith("tb2-")):
            raise UnsupportedCapability("Task-specific microVM storage policy is separate")
        result = {"idle_ttl_seconds": self.ttl_running_stop,
                  "environment_id": self.environment_id,
                  "memory_profile": self.memory_profile}
        if self.verifier_storage is not None:
            result["verifier_storage"] = self.verifier_storage
        if self.environment_id != "e3-mixed" and not self.environment_id.startswith("tb2-"):
            result["storage"] = self.storage
        if self.baseline_id:
            result["baseline_id"] = self.baseline_id
        return result


@dataclass(frozen=True)
class DSecContainerRunArgs:
    environment_id: str = "e1-real"
    storage: str = "local"
    memory_limit_mb: int = 512
    cpu_cores_limit: float = 1.0
    cpu_qos: str = "default"
    ttl_running_stop: float | None = None
    network_rules: dict | None = None
    init_user: str | None = None

    def validate(self):
        if (not isinstance(self.environment_id, str) or
                not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", self.environment_id) or
                self.storage not in ("local", "threefs_lazy")):
            raise UnsupportedCapability("Unsupported container environment/storage combination")
        if self.network_rules is not None or self.init_user is not None:
            raise UnsupportedCapability("Container network rules and init_user are not integrated")
        if self.ttl_running_stop is not None:
            raise UnsupportedCapability("Container idle TTL is not integrated")
        if self.cpu_qos != "default" and not re.fullmatch(
                r"(?:ls_core_cookie:[2468]|be_sched_idle:[2-9])", self.cpu_qos):
            raise UnsupportedCapability("Unsupported E4 container CPU QoS profile")
        if (not isinstance(self.memory_limit_mb, int) or isinstance(self.memory_limit_mb, bool)
                or self.memory_limit_mb < 128):
            raise ValueError("memory_mb must be at least 128")
        if (not isinstance(self.cpu_cores_limit, (int, float)) or
                isinstance(self.cpu_cores_limit, bool) or
                not math.isfinite(self.cpu_cores_limit) or self.cpu_cores_limit <= 0):
            raise ValueError("cpus must be finite and positive")
        return self

    def lifecycle_args(self):
        return {"environment_id": self.environment_id, "storage": self.storage,
                "memory_mb": self.memory_limit_mb, "cpus": self.cpu_cores_limit,
                "qos": self.cpu_qos}

    def stop_args(self):
        return {"environment_id": self.environment_id, "storage": self.storage}


@dataclass(frozen=True)
class DSecTB2RunArgs:
    task_id: str
    image: str
    memory_limit_mb: int = 2048
    cpu_cores_limit: float = 1.0

    def validate(self):
        if (not isinstance(self.task_id, str) or
                not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", self.task_id)):
            raise ValueError("Invalid TB2 task ID")
        if not isinstance(self.image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image):
            raise ValueError("TB2 image must be pinned to a local sha256 image ID")
        if (not isinstance(self.memory_limit_mb, int) or isinstance(self.memory_limit_mb, bool)
                or self.memory_limit_mb < 512):
            raise ValueError("memory_mb must be at least 512")
        if (not isinstance(self.cpu_cores_limit, (int, float)) or
                isinstance(self.cpu_cores_limit, bool) or
                not math.isfinite(self.cpu_cores_limit) or self.cpu_cores_limit <= 0):
            raise ValueError("cpus must be finite and positive")
        return self

    def lifecycle_args(self):
        return {"environment_id": "tb2-openenv", "task_id": self.task_id,
                "image": self.image, "memory_mb": self.memory_limit_mb,
                "cpus": self.cpu_cores_limit}

    def stop_args(self):
        return {"environment_id": "tb2-openenv", "task_id": self.task_id,
                "image": self.image}
