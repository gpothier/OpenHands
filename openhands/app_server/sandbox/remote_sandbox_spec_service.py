from typing import AsyncGenerator

from fastapi import Request
from pydantic import Field

from openhands.app_server.sandbox.preset_sandbox_spec_service import (
    PresetSandboxSpecService,
)
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxSpecInfo,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    DEFAULT_WORKING_DIR,
    SandboxSpecService,
    SandboxSpecServiceInjector,
    get_agent_server_image,
)
from openhands.app_server.services.injector import InjectorState


def get_default_sandbox_specs():
    return [
        SandboxSpecInfo(
            id=get_agent_server_image(),
            command=['/usr/local/bin/openhands-agent-server', '--port', '60000'],
            # Remote-specific env var override (defaults come from sandbox service)
            initial_env={'OH_VSCODE_PORT': '60001'},
            working_dir=DEFAULT_WORKING_DIR,
        )
    ]


class RemoteSandboxSpecServiceInjector(SandboxSpecServiceInjector):
    specs: list[SandboxSpecInfo] = Field(
        default_factory=get_default_sandbox_specs,
        description='Preset list of sandbox specs',
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxSpecService, None]:
        yield PresetSandboxSpecService(self.specs)
