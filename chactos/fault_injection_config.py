from typing import Optional

import pydantic

from acto.lib.operator_config import DeployConfig


class KubernetesConfig(pydantic.BaseModel, extra="forbid"):
    """Kubernetes Config"""

    num_nodes: int = pydantic.Field(
        description="Number of workers in the Kubernetes cluster", default=4
    )
    version: str = pydantic.Field(
        default="v1.28.0", description="Kubernetes version"
    )
    feature_gates: Optional[dict[str, bool]] = pydantic.Field(
        description="Path to the feature gates file", default=None
    )


class FaultInjectionConfig(pydantic.BaseModel, extra="forbid"):
    """Fault Injection Config"""

    deploy: DeployConfig
    application_selector: dict
    priority_application_selector: Optional[dict] = None
    operator_selector: dict
    application_pod_prefix: str
    application_data_dir: str
    input_dir: Optional[str] = None
    kubernetes: Optional[KubernetesConfig] = pydantic.Field(
        description="Kubernetes Config", default=KubernetesConfig()
    )
    pod_failure_ratio: Optional[float] = 1.0
