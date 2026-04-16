from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from openhands.agent_server.utils import utc_now


class SandboxType(str, Enum):
    """Types of sandbox environments available."""

    DOCKER = 'docker'
    FIRECRACKER = 'firecracker'
    REMOTE = 'remote'
    PROCESS = 'process'


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


class SandboxSpecInfoPage(BaseModel):
    items: list[SandboxSpecInfo]
    next_page_id: str | None = None
