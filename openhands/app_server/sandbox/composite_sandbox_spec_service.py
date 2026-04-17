"""Composite sandbox spec service that provides specs for multiple sandbox types.

This service combines specs from Docker and Firecracker (when available),
allowing users to select their preferred sandbox type.
"""

import logging
import os
from dataclasses import dataclass
from typing import AsyncGenerator

from fastapi import Request
from pydantic import Field

from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxSpecInfo,
    SandboxSpecInfoPage,
    SandboxType,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    SandboxSpecServiceInjector,
    get_agent_server_env,
    get_agent_server_image,
)
from openhands.app_server.services.injector import InjectorState
from openhands.sdk.utils.models import OpenHandsModel

_logger = logging.getLogger(__name__)

# Cache for Firecracker availability check (checked once at startup)
_firecracker_available: bool | None = None


@dataclass
class CompositeSandboxSpecService(SandboxSpecService):
    """Sandbox spec service that provides specs for multiple sandbox types.

    Returns specs for both Docker containers and Firecracker VMs (when available),
    allowing users to choose their preferred isolation level.
    """

    docker_available: bool = True
    firecracker_available: bool = False

    async def search_sandbox_specs(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxSpecInfoPage:
        """Return available sandbox specs for all supported types."""
        items: list[SandboxSpecInfo] = []

        # Docker spec (always available)
        if self.docker_available:
            items.append(
                SandboxSpecInfo(
                    id=get_agent_server_image(),
                    name='Docker Container',
                    type=SandboxType.DOCKER,
                    description='Run in a Docker container with process-level isolation',
                    initial_env={
                        'OPENVSCODE_SERVER_ROOT': '/openhands/.openvscode-server',
                        'OH_ENABLE_VNC': '0',
                        'LOG_JSON': 'true',
                        'OH_CONVERSATIONS_PATH': '/workspace/conversations',
                        'OH_BASH_EVENTS_DIR': '/workspace/bash_events',
                        'PYTHONUNBUFFERED': '1',
                        'ENV_LOG_LEVEL': '20',
                        **get_agent_server_env(),
                    },
                    working_dir='/workspace',
                )
            )

        # Firecracker spec (only if prerequisites are available)
        if self.firecracker_available:
            items.append(
                SandboxSpecInfo(
                    id=f'firecracker::{get_agent_server_image()}',
                    name='Firecracker microVM',
                    type=SandboxType.FIRECRACKER,
                    description=(
                        'Run in a Firecracker microVM with hardware-level isolation '
                        '(KVM). Provides stronger security boundaries than containers.'
                    ),
                    initial_env={
                        'OPENVSCODE_SERVER_ROOT': '/openhands/.openvscode-server',
                        'OH_ENABLE_VNC': '0',
                        'LOG_JSON': 'true',
                        'OH_CONVERSATIONS_PATH': '/workspace/conversations',
                        'OH_BASH_EVENTS_DIR': '/workspace/bash_events',
                        'PYTHONUNBUFFERED': '1',
                        'ENV_LOG_LEVEL': '20',
                        **get_agent_server_env(),
                    },
                    kvm_enabled=True,
                    working_dir='/workspace',
                )
            )

        return SandboxSpecInfoPage(items=items)

    async def get_sandbox_spec(self, sandbox_spec_id: str) -> SandboxSpecInfo | None:
        """Get a specific sandbox spec by ID."""
        # Check if it's a Firecracker spec
        if sandbox_spec_id.startswith('firecracker::'):
            if not self.firecracker_available:
                return None
            sandbox_spec_id[len('firecracker::') :]
            return SandboxSpecInfo(
                id=sandbox_spec_id,
                name='Firecracker microVM',
                type=SandboxType.FIRECRACKER,
                description='Run in a Firecracker microVM with hardware-level isolation',
                initial_env={
                    'OPENVSCODE_SERVER_ROOT': '/openhands/.openvscode-server',
                    'OH_ENABLE_VNC': '0',
                    'LOG_JSON': 'true',
                    'OH_CONVERSATIONS_PATH': '/workspace/conversations',
                    'OH_BASH_EVENTS_DIR': '/workspace/bash_events',
                    'PYTHONUNBUFFERED': '1',
                    'ENV_LOG_LEVEL': '20',
                    **get_agent_server_env(),
                },
                kvm_enabled=True,
                working_dir='/workspace',
            )

        # Docker spec
        if self.docker_available:
            return SandboxSpecInfo(
                id=sandbox_spec_id,
                name='Docker Container',
                type=SandboxType.DOCKER,
                description='Run in a Docker container',
                initial_env={
                    'OPENVSCODE_SERVER_ROOT': '/openhands/.openvscode-server',
                    'OH_ENABLE_VNC': '0',
                    'LOG_JSON': 'true',
                    'OH_CONVERSATIONS_PATH': '/workspace/conversations',
                    'OH_BASH_EVENTS_DIR': '/workspace/bash_events',
                    'PYTHONUNBUFFERED': '1',
                    'ENV_LOG_LEVEL': '20',
                    **get_agent_server_env(),
                },
                working_dir='/workspace',
            )

        return None


class CompositeSandboxSpecServiceInjector(SandboxSpecServiceInjector, OpenHandsModel):
    """Injector for composite sandbox spec service."""

    check_firecracker: bool = Field(
        default=True,
        description='Whether to check for Firecracker availability',
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxSpecService, None]:
        # Check Docker availability (assume always available for now)
        docker_available = True

        # Check Firecracker availability (cached after first check)
        global _firecracker_available
        if _firecracker_available is None and self.check_firecracker:
            # For daemon-based approach, check if the daemon socket is available
            # The daemon manages kernel/rootfs building on the host
            daemon_socket = os.environ.get(
                'OH_FIRECRACKER_MANAGER_SOCKET',
                '/var/run/oh-firecracker-manager/oh-firecracker.sock',
            )
            daemon_available = os.path.exists(daemon_socket)

            _firecracker_available = daemon_available

            _logger.info(
                f'Firecracker availability check (one-time): '
                f'Daemon socket={daemon_socket}, Available={_firecracker_available}'
            )

        firecracker_available = bool(
            _firecracker_available if self.check_firecracker else False
        )

        yield CompositeSandboxSpecService(
            docker_available=docker_available,
            firecracker_available=firecracker_available,
        )
