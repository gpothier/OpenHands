"""Sandbox service registry that routes to appropriate sandbox implementations.

This module provides a registry that manages multiple sandbox service implementations
(Docker, Firecracker, Process, Remote) and routes operations to the appropriate
service based on the sandbox spec type.
"""

import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator

from fastapi import Request

from openhands.app_server.sandbox.sandbox_models import (
    SandboxInfo,
    SandboxPage,
    SandboxStartParams,
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
class SandboxServiceRegistry(SandboxService):
    """Registry that routes sandbox operations to the appropriate service.

    This registry holds multiple sandbox service implementations and routes
    operations based on the sandbox spec type. All sandbox types are always
    available (if their dependencies are met).
    """

    # Map of sandbox type to service implementation
    services: dict[SandboxType, SandboxService] = field(default_factory=dict)
    # Default service to use when spec type is unknown
    default_type: SandboxType = SandboxType.DOCKER
    # Service to look up spec types
    sandbox_spec_service: SandboxSpecService | None = None
    # Track which service owns which sandbox (sandbox_id -> type)
    _sandbox_ownership: dict[str, SandboxType] = field(default_factory=dict)

    def register(self, sandbox_type: SandboxType, service: SandboxService) -> None:
        """Register a sandbox service for a specific type."""
        self.services[sandbox_type] = service
        _logger.info(f'Registered {type(service).__name__} for {sandbox_type.value}')

    def get_service_by_type(self, sandbox_type: SandboxType) -> SandboxService | None:
        """Get a registered service by type, or None if not registered."""
        return self.services.get(sandbox_type)

    def _get_service(self, sandbox_type: SandboxType) -> SandboxService:
        """Get the service for a sandbox type."""
        service = self.services.get(sandbox_type)
        if service is None:
            raise RuntimeError(
                f'No sandbox service registered for type {sandbox_type.value}. '
                f'Available types: {list(self.services.keys())}'
            )
        return service

    async def _get_type_for_spec(self, sandbox_spec_id: str | None) -> SandboxType:
        """Determine the sandbox type from a spec ID."""
        if sandbox_spec_id is None:
            return self.default_type

        if self.sandbox_spec_service is None:
            _logger.warning('No sandbox_spec_service, using default type')
            return self.default_type

        spec = await self.sandbox_spec_service.get_sandbox_spec(sandbox_spec_id)
        if spec is None:
            _logger.warning(f'Spec not found: {sandbox_spec_id}, using default type')
            return self.default_type

        return spec.type

    async def search_sandboxes(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxPage:
        """Search sandboxes across all registered services."""
        all_items: list[SandboxInfo] = []

        for sandbox_type, service in self.services.items():
            try:
                page = await service.search_sandboxes(page_id, limit)
                all_items.extend(page.items)
            except Exception as e:
                _logger.warning(f'Error searching {sandbox_type.value} sandboxes: {e}')

        # Simple pagination - just return up to limit
        return SandboxPage(
            items=all_items[:limit],
            next_page_id=None if len(all_items) <= limit else str(limit),
        )

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get a sandbox by ID, searching across all services."""
        # Check ownership cache first
        if sandbox_id in self._sandbox_ownership:
            sandbox_type = self._sandbox_ownership[sandbox_id]
            return await self.services[sandbox_type].get_sandbox(sandbox_id)

        # Search all services
        for sandbox_type, service in self.services.items():
            try:
                result = await service.get_sandbox(sandbox_id)
                if result is not None:
                    self._sandbox_ownership[sandbox_id] = sandbox_type
                    return result
            except Exception as e:
                _logger.debug(f'Error getting sandbox from {sandbox_type.value}: {e}')

        return None

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get sandbox by session API key, searching across all services."""
        for sandbox_type, service in self.services.items():
            try:
                result = await service.get_sandbox_by_session_api_key(session_api_key)
                if result is not None:
                    self._sandbox_ownership[result.id] = sandbox_type
                    return result
            except Exception as e:
                _logger.debug(
                    f'Error getting sandbox by key from {sandbox_type.value}: {e}'
                )

        return None

    async def start_sandbox(
        self,
        params: SandboxStartParams | None = None,
    ) -> SandboxInfo:
        """Start a sandbox using the appropriate service based on spec type."""
        if params is None:
            params = SandboxStartParams()

        sandbox_type = await self._get_type_for_spec(params.sandbox_spec_id)
        _logger.info(
            f'Starting sandbox: spec_id={params.sandbox_spec_id}, '
            f'type={sandbox_type.value}'
        )

        service = self._get_service(sandbox_type)
        info = await service.start_sandbox(params)

        # Track ownership
        self._sandbox_ownership[info.id] = sandbox_type
        return info

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        """Resume a sandbox, routing to the owning service."""
        if sandbox_id in self._sandbox_ownership:
            sandbox_type = self._sandbox_ownership[sandbox_id]
            return await self.services[sandbox_type].resume_sandbox(sandbox_id)

        # Try all services
        for sandbox_type, service in self.services.items():
            try:
                if await service.resume_sandbox(sandbox_id):
                    self._sandbox_ownership[sandbox_id] = sandbox_type
                    return True
            except Exception:
                pass

        return False

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Pause a sandbox, routing to the owning service."""
        if sandbox_id in self._sandbox_ownership:
            sandbox_type = self._sandbox_ownership[sandbox_id]
            return await self.services[sandbox_type].pause_sandbox(sandbox_id)

        # Try all services
        for sandbox_type, service in self.services.items():
            try:
                if await service.pause_sandbox(sandbox_id):
                    self._sandbox_ownership[sandbox_id] = sandbox_type
                    return True
            except Exception:
                pass

        return False

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete a sandbox, routing to the owning service."""
        if sandbox_id in self._sandbox_ownership:
            sandbox_type = self._sandbox_ownership[sandbox_id]
            result = await self.services[sandbox_type].delete_sandbox(sandbox_id)
            if result:
                del self._sandbox_ownership[sandbox_id]
            return result

        # Try all services
        for sandbox_type, service in self.services.items():
            try:
                if await service.delete_sandbox(sandbox_id):
                    return True
            except Exception:
                pass

        return False

    async def get_vscode_internal_url(self, short_sandbox_id: str) -> str | None:
        """Get VS Code internal URL, searching across all services."""
        for service in self.services.values():
            try:
                result = await service.get_vscode_internal_url(short_sandbox_id)
                if result:
                    return result
            except Exception:
                pass
        return None

    async def get_agent_server_internal_url(self, short_sandbox_id: str) -> str | None:
        """Get agent-server internal URL, searching across all services."""
        for service in self.services.values():
            try:
                result = await service.get_agent_server_internal_url(short_sandbox_id)
                if result:
                    return result
            except Exception:
                pass
        return None


class SandboxServiceRegistryInjector(SandboxServiceInjector):
    """Injector that creates a registry with all available sandbox services."""

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        import os

        from openhands.app_server.config import get_sandbox_spec_service

        registry = SandboxServiceRegistry()

        # Always try to register Docker service
        try:
            from openhands.app_server.sandbox.docker_sandbox_service import (
                DockerSandboxServiceInjector,
            )

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

            docker_injector = DockerSandboxServiceInjector(**docker_kwargs)
            async for docker_service in docker_injector.inject(state, request):
                registry.register(SandboxType.DOCKER, docker_service)
                break
        except Exception as e:
            _logger.warning(f'Failed to initialize Docker service: {e}')

        # Try to register Firecracker service if daemon is available
        daemon_socket = os.environ.get(
            'OH_FIRECRACKER_MANAGER_SOCKET',
            '/var/run/fcvmd/fcvmd.sock',
        )
        if os.path.exists(daemon_socket):
            try:
                from openhands.app_server.sandbox.firecracker_sandbox_service import (
                    FirecrackerSandboxServiceInjector,
                )

                fc_injector = FirecrackerSandboxServiceInjector()
                async for fc_service in fc_injector.inject(state, request):
                    registry.register(SandboxType.FIRECRACKER, fc_service)
                    break
                _logger.info('Firecracker service registered')
            except Exception as e:
                _logger.warning(f'Failed to initialize Firecracker service: {e}')

        # Set up spec service for type lookup
        async with get_sandbox_spec_service(state) as spec_service:
            registry.sandbox_spec_service = spec_service
            yield registry
