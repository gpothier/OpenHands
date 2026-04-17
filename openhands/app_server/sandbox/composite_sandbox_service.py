"""Composite sandbox service that supports multiple sandbox types.

This service routes sandbox operations to the appropriate underlying service
(Docker or Firecracker) based on the sandbox spec type.
"""

import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator

from fastapi import Request

from openhands.app_server.sandbox.sandbox_models import (
    SandboxInfo,
    SandboxPage,
)
from openhands.app_server.sandbox.sandbox_service import (
    SandboxService,
    SandboxServiceInjector,
)
from openhands.app_server.sandbox.sandbox_spec_models import SandboxType
from openhands.app_server.sandbox.sandbox_spec_service import SandboxSpecService
from openhands.app_server.services.injector import InjectorState

_logger = logging.getLogger(__name__)


@dataclass
class CompositeSandboxService(SandboxService):
    """Sandbox service that delegates to Docker or Firecracker based on spec type.

    This allows a single OpenHands instance to support both Docker containers
    and Firecracker microVMs, with users selecting the type per-conversation.
    """

    docker_service: SandboxService
    firecracker_service: SandboxService | None
    sandbox_spec_service: SandboxSpecService
    # Track which service owns which sandbox (sandbox_id -> service)
    _sandbox_ownership: dict[str, SandboxService] = field(default_factory=dict)

    def _get_service_for_type(self, sandbox_type: SandboxType) -> SandboxService:
        """Get the appropriate service for a sandbox type."""
        if sandbox_type == SandboxType.FIRECRACKER:
            if self.firecracker_service is None:
                raise RuntimeError(
                    'Firecracker sandbox type requested but Firecracker service '
                    'is not available. Ensure /dev/kvm is accessible and '
                    'Firecracker resources are mounted.'
                )
            return self.firecracker_service
        # Default to Docker for all other types
        return self.docker_service

    async def _get_service_for_spec(
        self, sandbox_spec_id: str | None
    ) -> SandboxService:
        """Get the appropriate service based on sandbox spec."""
        if sandbox_spec_id is None:
            _logger.info('sandbox_spec_id is None, defaulting to Docker')
            return self.docker_service

        spec = await self.sandbox_spec_service.get_sandbox_spec(sandbox_spec_id)
        if spec is None:
            _logger.warning(f'Sandbox spec not found: {sandbox_spec_id}, using Docker')
            return self.docker_service

        _logger.info(f'Found spec: id={spec.id}, type={spec.type}')
        return self._get_service_for_type(spec.type)

    async def search_sandboxes(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxPage:
        """Search sandboxes across all services."""
        # For now, just search Docker. In future, merge results from both.
        # The sandbox ownership tracking will help route operations correctly.
        docker_page = await self.docker_service.search_sandboxes(page_id, limit)

        if self.firecracker_service:
            fc_page = await self.firecracker_service.search_sandboxes(page_id, limit)
            # Simple merge - in production you'd want proper pagination
            return SandboxPage(
                items=docker_page.items + fc_page.items,
                next_page_id=docker_page.next_page_id or fc_page.next_page_id,
            )

        return docker_page

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get a single sandbox, routing to the appropriate service."""
        # Check ownership cache first
        if sandbox_id in self._sandbox_ownership:
            return await self._sandbox_ownership[sandbox_id].get_sandbox(sandbox_id)

        # Try Docker first
        result = await self.docker_service.get_sandbox(sandbox_id)
        if result is not None:
            self._sandbox_ownership[sandbox_id] = self.docker_service
            return result

        # Try Firecracker
        if self.firecracker_service:
            result = await self.firecracker_service.get_sandbox(sandbox_id)
            if result is not None:
                self._sandbox_ownership[sandbox_id] = self.firecracker_service
                return result

        return None

    async def batch_get_sandboxes(
        self, sandbox_ids: list[str]
    ) -> list[SandboxInfo | None]:
        """Get sandboxes by ID, routing to appropriate service."""
        results: list[SandboxInfo | None] = []
        for sandbox_id in sandbox_ids:
            result = await self.get_sandbox(sandbox_id)
            results.append(result)
        return results

    async def start_sandbox(
        self,
        sandbox_spec_id: str | None = None,
        sandbox_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> SandboxInfo:
        """Start a sandbox using the appropriate service based on spec type."""
        _logger.info(
            f'CompositeSandboxService.start_sandbox called with '
            f'sandbox_spec_id={sandbox_spec_id}, sandbox_id={sandbox_id}'
        )
        service = await self._get_service_for_spec(sandbox_spec_id)
        service_name = type(service).__name__
        _logger.info(f'Routing to service: {service_name}')
        info = await service.start_sandbox(sandbox_spec_id, sandbox_id, extra_env)
        # Track ownership
        self._sandbox_ownership[info.id] = service
        return info

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Pause a sandbox, routing to the owning service."""
        service = self._sandbox_ownership.get(sandbox_id)
        if service:
            return await service.pause_sandbox(sandbox_id)

        # Try both services
        if await self.docker_service.pause_sandbox(sandbox_id):
            self._sandbox_ownership[sandbox_id] = self.docker_service
            return True
        if self.firecracker_service:
            if await self.firecracker_service.pause_sandbox(sandbox_id):
                self._sandbox_ownership[sandbox_id] = self.firecracker_service
                return True
        return False

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        """Resume a sandbox, routing to the owning service."""
        service = self._sandbox_ownership.get(sandbox_id)
        if service:
            return await service.resume_sandbox(sandbox_id)

        # Try both services
        if await self.docker_service.resume_sandbox(sandbox_id):
            self._sandbox_ownership[sandbox_id] = self.docker_service
            return True
        if self.firecracker_service:
            if await self.firecracker_service.resume_sandbox(sandbox_id):
                self._sandbox_ownership[sandbox_id] = self.firecracker_service
                return True
        return False

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete a sandbox, routing to the owning service."""
        service = self._sandbox_ownership.get(sandbox_id)
        if service:
            result = await service.delete_sandbox(sandbox_id)
            if result:
                del self._sandbox_ownership[sandbox_id]
            return result

        # Try both services
        if await self.docker_service.delete_sandbox(sandbox_id):
            return True
        if self.firecracker_service:
            return await self.firecracker_service.delete_sandbox(sandbox_id)
        return False

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get sandbox by session API key from any service."""
        # Try Docker first
        result = await self.docker_service.get_sandbox_by_session_api_key(
            session_api_key
        )
        if result:
            self._sandbox_ownership[result.id] = self.docker_service
            return result

        # Try Firecracker
        if self.firecracker_service:
            result = await self.firecracker_service.get_sandbox_by_session_api_key(
                session_api_key
            )
            if result:
                self._sandbox_ownership[result.id] = self.firecracker_service
                return result

        return None

    async def get_vscode_internal_url(self, short_sandbox_id: str) -> str | None:
        """Get VS Code internal URL from appropriate service."""
        # Try Docker first
        result = await self.docker_service.get_vscode_internal_url(short_sandbox_id)
        if result:
            return result

        # Try Firecracker
        if self.firecracker_service:
            result = await self.firecracker_service.get_vscode_internal_url(
                short_sandbox_id
            )
            if result:
                return result

        return None

    async def get_agent_server_internal_url(self, short_sandbox_id: str) -> str | None:
        """Get agent-server internal URL from appropriate service."""
        # Try Docker first
        result = await self.docker_service.get_agent_server_internal_url(
            short_sandbox_id
        )
        if result:
            return result

        # Try Firecracker
        if self.firecracker_service:
            result = await self.firecracker_service.get_agent_server_internal_url(
                short_sandbox_id
            )
            if result:
                return result

        return None


class CompositeSandboxServiceInjector(SandboxServiceInjector):
    """Injector for composite sandbox service supporting multiple sandbox types."""

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        import os

        from openhands.app_server.config import get_sandbox_spec_service
        from openhands.app_server.sandbox.docker_sandbox_service import (
            DockerSandboxServiceInjector,
        )

        # Build Docker injector kwargs from environment variables
        # (same logic as in config.py for direct Docker sandbox)
        docker_kwargs: dict = {}
        if os.getenv('SANDBOX_STARTUP_GRACE_SECONDS'):
            docker_kwargs['startup_grace_seconds'] = int(
                os.environ['SANDBOX_STARTUP_GRACE_SECONDS']
            )
        if os.getenv('SANDBOX_PROXY_VSCODE'):
            docker_kwargs['proxy_vscode'] = os.environ[
                'SANDBOX_PROXY_VSCODE'
            ].lower() in ('1', 'true', 'yes', 'on')
        if os.getenv('SANDBOX_PROXY_AGENT'):
            docker_kwargs['proxy_agent'] = os.environ[
                'SANDBOX_PROXY_AGENT'
            ].lower() in ('1', 'true', 'yes', 'on')

        # Always create Docker service
        docker_injector = DockerSandboxServiceInjector(**docker_kwargs)

        # Try to create Firecracker service if daemon socket is available
        firecracker_service: SandboxService | None = None
        daemon_socket = os.environ.get(
            'OH_FIRECRACKER_MANAGER_SOCKET',
            '/var/run/oh-firecracker-manager/oh-firecracker.sock',
        )
        if os.path.exists(daemon_socket):
            try:
                from openhands.app_server.sandbox.firecracker_sandbox_service import (
                    FirecrackerSandboxServiceInjector,
                )

                fc_injector = FirecrackerSandboxServiceInjector()
                async for fc_service in fc_injector.inject(state, request):
                    firecracker_service = fc_service
                    break
                _logger.info('Firecracker service initialized via daemon')
            except ImportError:
                _logger.debug('Firecracker service module not available')
            except Exception as e:
                _logger.warning(f'Failed to initialize Firecracker service: {e}')

        async with get_sandbox_spec_service(state) as sandbox_spec_service:
            async for docker_service in docker_injector.inject(state, request):
                yield CompositeSandboxService(
                    docker_service=docker_service,
                    firecracker_service=firecracker_service,
                    sandbox_spec_service=sandbox_spec_service,
                )
