"""Sandbox registry that manages multiple sandbox implementations.

This module provides the `SandboxRegistry` class that holds all available
sandbox implementations and provides unified access to specs and operations.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from openhands.app_server.sandbox.sandbox import Sandbox, SandboxAdapter
from openhands.app_server.sandbox.sandbox_models import (
    SandboxInfo,
    SandboxPage,
    SandboxStartParams,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxSpecInfo,
    SandboxSpecInfoPage,
    SandboxType,
)

_logger = logging.getLogger(__name__)


@dataclass
class SandboxRegistry:
    """Registry that holds all available sandbox implementations.

    This is NOT a Sandbox itself - it's a container that manages multiple
    sandbox implementations and provides methods to access them.
    """

    # All registered sandbox implementations
    sandboxes: dict[SandboxType, Sandbox] = field(default_factory=dict)
    # Default type when none is specified
    default_type: SandboxType = SandboxType.DOCKER
    # Cache: sandbox_id -> type (to avoid searching all sandboxes)
    _ownership: dict[str, SandboxType] = field(default_factory=dict)

    def register(self, sandbox: Sandbox) -> None:
        """Register a sandbox implementation."""
        self.sandboxes[sandbox.sandbox_type] = sandbox
        _logger.info(f'Registered {type(sandbox).__name__} for {sandbox.sandbox_type.value}')

    def get(self, sandbox_type: SandboxType) -> Sandbox | None:
        """Get a sandbox implementation by type."""
        return self.sandboxes.get(sandbox_type)

    @property
    def available_types(self) -> list[SandboxType]:
        """Return list of available sandbox types."""
        return list(self.sandboxes.keys())

    # -------------------------------------------------------------------------
    # Spec aggregation
    # -------------------------------------------------------------------------

    async def search_all_specs(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxSpecInfoPage:
        """Search specs across all sandbox types."""
        all_items: list[SandboxSpecInfo] = []

        for sandbox in self.sandboxes.values():
            try:
                page = await sandbox.search_specs(page_id, limit)
                all_items.extend(page.items)
            except Exception as e:
                _logger.warning(
                    f'Error searching specs from {sandbox.sandbox_type.value}: {e}'
                )

        return SandboxSpecInfoPage(
            items=all_items[:limit],
            next_page_id=None if len(all_items) <= limit else str(limit),
        )

    async def get_spec(self, spec_id: str) -> SandboxSpecInfo | None:
        """Get a spec by ID, searching across all sandbox types."""
        for sandbox in self.sandboxes.values():
            try:
                spec = await sandbox.get_spec(spec_id)
                if spec is not None:
                    return spec
            except Exception as e:
                _logger.debug(
                    f'Error getting spec from {sandbox.sandbox_type.value}: {e}'
                )
        return None

    async def get_sandbox_for_spec(self, spec_id: str) -> Sandbox | None:
        """Get the sandbox implementation that provides a given spec."""
        for sandbox in self.sandboxes.values():
            try:
                spec = await sandbox.get_spec(spec_id)
                if spec is not None:
                    return sandbox
            except Exception:
                pass
        return None

    async def get_default_spec(self) -> SandboxSpecInfo | None:
        """Get the default spec from the default sandbox type."""
        sandbox = self.sandboxes.get(self.default_type)
        if sandbox:
            return await sandbox.get_default_spec()
        # Fall back to first available
        for sandbox in self.sandboxes.values():
            spec = await sandbox.get_default_spec()
            if spec:
                return spec
        return None

    # -------------------------------------------------------------------------
    # Sandbox operations (aggregate across all types)
    # -------------------------------------------------------------------------

    async def search_all_sandboxes(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxPage:
        """Search sandboxes across all types."""
        all_items: list[SandboxInfo] = []

        for sandbox in self.sandboxes.values():
            try:
                page = await sandbox.search_sandboxes(page_id, limit)
                all_items.extend(page.items)
            except Exception as e:
                _logger.warning(
                    f'Error searching sandboxes from {sandbox.sandbox_type.value}: {e}'
                )

        return SandboxPage(
            items=all_items[:limit],
            next_page_id=None if len(all_items) <= limit else str(limit),
        )

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get a sandbox by ID, searching across all types."""
        # Check cache first
        if sandbox_id in self._ownership:
            sandbox = self.sandboxes.get(self._ownership[sandbox_id])
            if sandbox:
                return await sandbox.get_sandbox(sandbox_id)

        # Search all
        for sandbox in self.sandboxes.values():
            try:
                info = await sandbox.get_sandbox(sandbox_id)
                if info is not None:
                    self._ownership[sandbox_id] = sandbox.sandbox_type
                    return info
            except Exception:
                pass
        return None

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get a sandbox by session API key."""
        for sandbox in self.sandboxes.values():
            try:
                info = await sandbox.get_sandbox_by_session_api_key(session_api_key)
                if info is not None:
                    self._ownership[info.id] = sandbox.sandbox_type
                    return info
            except Exception:
                pass
        return None

    async def start_sandbox(
        self, params: SandboxStartParams | None = None
    ) -> SandboxInfo:
        """Start a sandbox, routing to the appropriate implementation based on spec."""
        if params is None:
            params = SandboxStartParams()

        # Determine which sandbox type to use
        sandbox: Sandbox | None = None
        if params.sandbox_spec_id:
            sandbox = await self.get_sandbox_for_spec(params.sandbox_spec_id)

        if sandbox is None:
            sandbox = self.sandboxes.get(self.default_type)

        if sandbox is None:
            raise RuntimeError(
                f'No sandbox available. Registered types: {self.available_types}'
            )

        _logger.info(
            f'Starting sandbox: spec_id={params.sandbox_spec_id}, '
            f'type={sandbox.sandbox_type.value}'
        )

        info = await sandbox.start_sandbox(params)
        self._ownership[info.id] = sandbox.sandbox_type
        return info

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete a sandbox."""
        # Check cache
        if sandbox_id in self._ownership:
            sandbox = self.sandboxes.get(self._ownership[sandbox_id])
            if sandbox:
                result = await sandbox.delete_sandbox(sandbox_id)
                if result:
                    del self._ownership[sandbox_id]
                return result

        # Try all
        for sandbox in self.sandboxes.values():
            try:
                if await sandbox.delete_sandbox(sandbox_id):
                    return True
            except Exception:
                pass
        return False

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Pause a sandbox."""
        if sandbox_id in self._ownership:
            sandbox = self.sandboxes.get(self._ownership[sandbox_id])
            if sandbox:
                return await sandbox.pause_sandbox(sandbox_id)

        for sandbox in self.sandboxes.values():
            try:
                if await sandbox.pause_sandbox(sandbox_id):
                    self._ownership[sandbox_id] = sandbox.sandbox_type
                    return True
            except Exception:
                pass
        return False

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        """Resume a sandbox."""
        if sandbox_id in self._ownership:
            sandbox = self.sandboxes.get(self._ownership[sandbox_id])
            if sandbox:
                return await sandbox.resume_sandbox(sandbox_id)

        for sandbox in self.sandboxes.values():
            try:
                if await sandbox.resume_sandbox(sandbox_id):
                    self._ownership[sandbox_id] = sandbox.sandbox_type
                    return True
            except Exception:
                pass
        return False

    async def batch_get_sandboxes(
        self, sandbox_ids: list[str]
    ) -> list[SandboxInfo | None]:
        """Get a batch of sandboxes, returning None for any not found."""
        results = await asyncio.gather(
            *[self.get_sandbox(sandbox_id) for sandbox_id in sandbox_ids]
        )
        return list(results)

    async def wait_for_sandbox_running(
        self,
        sandbox_id: str,
        timeout: int = 120,
        poll_interval: int = 2,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> SandboxInfo:
        """Wait for a sandbox to reach RUNNING status with an alive agent server.

        Args:
            sandbox_id: The sandbox ID to wait for
            timeout: Maximum time to wait in seconds
            poll_interval: Time between status checks in seconds
            httpx_client: Optional httpx client for agent server health checks.
                If provided, will verify the agent server /alive endpoint responds
                before returning.

        Returns:
            SandboxInfo with RUNNING status and verified agent server

        Raises:
            SandboxError: If sandbox not found, enters ERROR state, or times out
        """
        from openhands.app_server.errors import SandboxError

        start = time.time()
        while time.time() - start <= timeout:
            sandbox = await self.get_sandbox(sandbox_id)
            if sandbox is None:
                raise SandboxError(f'Sandbox not found: {sandbox_id}')

            if sandbox.status == SandboxStatus.ERROR:
                raise SandboxError(f'Sandbox entered error state: {sandbox_id}')

            if sandbox.status == SandboxStatus.RUNNING:
                # Optionally verify agent server is alive to avoid race conditions
                # where sandbox reports RUNNING but agent server isn't ready yet
                if httpx_client and sandbox.exposed_urls:
                    if await self._check_agent_server_alive(sandbox, httpx_client):
                        return sandbox
                    # Agent server not ready yet, continue polling
                else:
                    return sandbox

            await asyncio.sleep(poll_interval)

        raise SandboxError(f'Sandbox failed to start within {timeout}s: {sandbox_id}')

    async def _check_agent_server_alive(
        self, sandbox: SandboxInfo, httpx_client: httpx.AsyncClient
    ) -> bool:
        """Check if the agent server is responding to health checks."""
        for url in sandbox.exposed_urls:
            if url.name == 'agent':
                try:
                    response = await httpx_client.get(
                        f'{url.url}/alive', timeout=5.0
                    )
                    return response.status_code == 200
                except Exception:
                    return False
        return False


@asynccontextmanager
async def create_sandbox_registry() -> AsyncGenerator[SandboxRegistry, None]:
    """Create a SandboxRegistry with all available sandbox implementations.

    This factory function creates services directly (not via injectors)
    and registers them in the registry.
    """
    from openhands.app_server.config import get_global_config
    from openhands.app_server.sandbox.preset_sandbox_spec_service import (
        PresetSandboxSpecService,
    )
    from openhands.app_server.sandbox.sandbox_spec_models import SandboxType
    from openhands.app_server.sandbox.sandbox_spec_service import (
        DEFAULT_WORKING_DIR,
    )

    registry = SandboxRegistry()

    # Build Docker sandbox adapter directly (not using injector pattern
    # since injectors call get_sandbox_spec_service which requires registry)
    try:
        from openhands.app_server.sandbox.docker_sandbox_service import (
            DockerSandboxService,
        )
        from openhands.app_server.sandbox.docker_sandbox_spec_service import (
            get_default_sandbox_specs,
        )

        # Create Docker spec service directly using preset specs
        docker_spec_service = PresetSandboxSpecService(specs=get_default_sandbox_specs())

        # Get config for web_url and cors origins
        config = get_global_config()

        # Get Docker kwargs from environment
        docker_kwargs: dict[str, object] = {
            'sandbox_spec_service': docker_spec_service,
        }
        if os.getenv('SANDBOX_STARTUP_GRACE_SECONDS'):
            docker_kwargs['startup_grace_seconds'] = int(
                os.environ['SANDBOX_STARTUP_GRACE_SECONDS']
            )
        if os.getenv('SANDBOX_PROXY_VSCODE'):
            docker_kwargs['proxy_vscode'] = os.environ['SANDBOX_PROXY_VSCODE'].lower() in (
                '1',
                'true',
                'yes',
                'on',
            )
        if os.getenv('SANDBOX_PROXY_AGENT'):
            docker_kwargs['proxy_agent'] = os.environ['SANDBOX_PROXY_AGENT'].lower() in (
                '1',
                'true',
                'yes',
                'on',
            )
        if config.web_url:
            docker_kwargs['web_url'] = config.web_url
        if config.permitted_cors_origins:
            docker_kwargs['permitted_cors_origins'] = config.permitted_cors_origins

        docker_service = DockerSandboxService(**docker_kwargs)

        docker_adapter = SandboxAdapter(
            _sandbox_type=SandboxType.DOCKER,
            service=docker_service,
            spec_service=docker_spec_service,
        )
        registry.register(docker_adapter)
        _logger.info('Docker sandbox registered')
    except Exception as e:
        _logger.warning(f'Failed to initialize Docker sandbox: {e}')

    # Build Firecracker sandbox adapter if daemon is available
    daemon_socket = os.environ.get(
        'OH_FIRECRACKER_MANAGER_SOCKET',
        '/var/run/fcvmd/fcvmd.sock',
    )
    if os.path.exists(daemon_socket):
        try:
            from openhands.app_server.sandbox.firecracker_sandbox_service import (
                FirecrackerSandboxService,
            )

            # Create Firecracker spec service with preset specs
            fc_specs = [
                SandboxSpecInfo(
                    id='firecracker-vm',
                    name='Firecracker microVM',
                    type=SandboxType.FIRECRACKER,
                    description='Lightweight microVM with hardware-level isolation',
                    working_dir=DEFAULT_WORKING_DIR,
                    kvm_enabled=True,
                ),
            ]
            fc_spec_service = PresetSandboxSpecService(specs=fc_specs)

            # Create Firecracker service
            fc_service = FirecrackerSandboxService(
                daemon_socket=daemon_socket,
                sandbox_spec_service=fc_spec_service,
            )
            await fc_service.initialize()

            fc_adapter = SandboxAdapter(
                _sandbox_type=SandboxType.FIRECRACKER,
                service=fc_service,
                spec_service=fc_spec_service,
            )
            registry.register(fc_adapter)
            _logger.info('Firecracker sandbox registered')
        except Exception as e:
            _logger.warning(f'Failed to initialize Firecracker sandbox: {e}')

    yield registry
