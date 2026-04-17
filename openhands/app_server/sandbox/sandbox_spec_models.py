from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from openhands.agent_server.utils import utc_now


class SandboxType(str, Enum):
    """Types of sandbox environments available."""

    DOCKER = 'docker'
    FIRECRACKER = 'firecracker'
    REMOTE = 'remote'
    PROCESS = 'process'


class ExposedPort(BaseModel):
    """Exposed port for a service running in the sandbox.

    This defines a service that should be exposed and accessible from outside
    the sandbox. Used by both Docker and Firecracker sandbox implementations.
    """

    name: str = Field(description='Service name (e.g., SSH, VSCODE, AGENT_SERVER)')
    port: int = Field(description='Port number the service listens on')
    url_template: str | None = Field(
        default=None,
        description=(
            'URL template for the service. Supports {host} and {port} placeholders. '
            'Example: "ssh://{host}:{port}" for SSH, or None to use default http:// pattern.'
        ),
    )
    description: str | None = Field(
        default=None,
        description='Human-readable description of the service',
    )

    model_config = ConfigDict(frozen=True)


class SandboxSpecInfo(BaseModel):
    """A template for creating a Sandbox (e.g: A Docker Image vs Container)."""

    id: str
    name: str | None = Field(
        default=None,
        description='Human-readable name for display in UI',
    )
    type: SandboxType = Field(
        default=SandboxType.DOCKER,
        description='The type of sandbox environment',
    )
    description: str | None = Field(
        default=None,
        description='Description of the sandbox spec for display in UI',
    )
    command: list[str] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    initial_env: dict[str, str] = Field(
        default_factory=dict, description='Initial Environment Variables'
    )
    working_dir: str = '/home/openhands/workspace'
    # VM-specific configuration (only used when type is VM)
    kvm_enabled: bool = Field(
        default=False,
        description='Whether KVM hardware acceleration is enabled for this sandbox',
    )
    # Additional services exposed by this sandbox spec
    exposed_ports: list[ExposedPort] = Field(
        default_factory=list,
        description='Additional ports/services exposed by the sandbox beyond AGENT_SERVER and VSCODE',
    )


class SandboxSpecInfoPage(BaseModel):
    items: list[SandboxSpecInfo]
    next_page_id: str | None = None
