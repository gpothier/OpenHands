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
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
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
        _logger.info(
            f"Registered {type(sandbox).__name__} for {sandbox.sandbox_type.value}"
        )

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
                    f"Error searching specs from {sandbox.sandbox_type.value}: {e}"
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
                    f"Error getting spec from {sandbox.sandbox_type.value}: {e}"
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
                    f"Error searching sandboxes from {sandbox.sandbox_type.value}: {e}"
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
                f"No sandbox available. Registered types: {self.available_types}"
            )

        _logger.info(
            f"Starting sandbox: spec_id={params.sandbox_spec_id}, "
            f"type={sandbox.sandbox_type.value}"
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

    async def get_agent_server_internal_url(self, short_sandbox_id: str) -> str | None:
        """Return the host-local agent-server base URL for the given short sandbox ID.

        Used by the agent proxy route to forward browser traffic to the correct
        container port. Delegates to the appropriate sandbox adapter.

        Args:
            short_sandbox_id: The sandbox ID without the container-name prefix.

        Returns:
            A string like ``http://localhost:43210``, or None if not proxiable.
        """
        # Try each sandbox service to find one that handles this ID
        for sandbox in self.sandboxes.values():
            result = await sandbox.service.get_agent_server_internal_url(
                short_sandbox_id
            )
            if result:
                return result
        return None

    async def get_vscode_internal_url(self, short_sandbox_id: str) -> str | None:
        """Return the host-local VS Code base URL for the given short sandbox ID.

        Used by the vscode proxy route to forward browser traffic to the correct
        container port. Delegates to the appropriate sandbox adapter.

        Args:
            short_sandbox_id: The sandbox ID without the container-name prefix.

        Returns:
            A string like ``http://localhost:43211``, or None if not proxiable.
        """
        # Try each sandbox service to find one that handles this ID
        for sandbox in self.sandboxes.values():
            result = await sandbox.service.get_vscode_internal_url(short_sandbox_id)
            if result:
                return result
        return None

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
                raise SandboxError(f"Sandbox not found: {sandbox_id}")

            elapsed = time.time() - start
            _logger.debug(
                f"Sandbox {sandbox_id} status={sandbox.status.value} "
                f"elapsed={elapsed:.1f}s/{timeout}s"
            )

            if sandbox.status == SandboxStatus.ERROR:
                raise SandboxError(f"Sandbox entered error state: {sandbox_id}")

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

        raise SandboxError(f"Sandbox failed to start within {timeout}s: {sandbox_id}")

    async def _check_agent_server_alive(
        self, sandbox: SandboxInfo, httpx_client: httpx.AsyncClient
    ) -> bool:
        """Check if the agent server is responding to health checks."""
        from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER

        for url in sandbox.exposed_urls:
            if url.name == AGENT_SERVER:
                # Use internal_url if available (for proxy setups), else use url
                check_url = url.internal_url or url.url
                # When running in Docker, replace localhost with host.docker.internal
                check_url = replace_localhost_hostname_for_docker(check_url)
                alive_url = f"{check_url.rstrip('/')}/alive"
                try:
                    response = await httpx_client.get(alive_url, timeout=5.0)
                    _logger.debug(
                        f"Agent server alive check {alive_url} -> {response.status_code}"
                    )
                    return response.is_success
                except Exception as e:
                    _logger.debug(f"Agent server alive check {alive_url} failed: {e}")
                    return False
        _logger.debug(f"No AGENT_SERVER URL found in sandbox {sandbox.id} exposed_urls")
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
        import httpx

        from openhands.app_server.sandbox.docker_sandbox_service import (
            AGENT_SERVER,
            DockerSandboxService,
            ExposedPort,
            SSH,
            VSCODE,
            WORKER_1,
            WORKER_2,
        )
        from openhands.app_server.sandbox.docker_sandbox_spec_service import (
            get_default_sandbox_specs,
        )

        # Create Docker spec service directly using preset specs
        docker_spec_service = PresetSandboxSpecService(
            specs=get_default_sandbox_specs()
        )

        # Get config for web_url and cors origins
        config = get_global_config()

        # Default exposed ports (same as injector)
        default_exposed_ports = [
            ExposedPort(name=AGENT_SERVER, description="Agent server port", port=8000),
            ExposedPort(name=VSCODE, description="VSCode server port", port=8001),
            ExposedPort(
                name=SSH,
                description="SSH server port",
                port=2222,
                url_template="ssh://{host}:{port}",
            ),
            ExposedPort(name=WORKER_1, description="Worker port 1", port=8011),
            ExposedPort(name=WORKER_2, description="Worker port 2", port=8012),
        ]

        # Build Docker service with all required arguments
        kvm_enabled = os.getenv("SANDBOX_KVM_ENABLED", "").lower() in (
            "1",
            "true",
            "yes",
        )
        container_runtime = os.getenv("SANDBOX_CONTAINER_RUNTIME") or None
        _logger.info(
            f"DockerSandboxService kvm_enabled={kvm_enabled} "
            f"container_runtime={container_runtime!r} "
            f"(from SANDBOX_KVM_ENABLED / SANDBOX_CONTAINER_RUNTIME env vars)"
        )

        docker_service = DockerSandboxService(
            sandbox_spec_service=docker_spec_service,
            container_name_prefix="oh-agent-server-",
            host_port=int(os.getenv("SANDBOX_HOST_PORT", "3000")),
            container_url_pattern=os.getenv(
                "SANDBOX_CONTAINER_URL_PATTERN", "http://localhost:{port}"
            ),
            mounts=[],
            exposed_ports=default_exposed_ports,
            health_check_path="/health",
            httpx_client=httpx.AsyncClient(),
            max_num_sandboxes=5,
            web_url=config.web_url,
            permitted_cors_origins=config.permitted_cors_origins or [],
            extra_hosts={"host.docker.internal": "host-gateway"},
            startup_grace_seconds=int(os.getenv("SANDBOX_STARTUP_GRACE_SECONDS", "60")),
            kvm_enabled=kvm_enabled,
            container_runtime=container_runtime,
            proxy_vscode=os.getenv("SANDBOX_PROXY_VSCODE", "").lower()
            in ("1", "true", "yes", "on"),
            proxy_agent=os.getenv("SANDBOX_PROXY_AGENT", "").lower()
            in ("1", "true", "yes", "on"),
        )

        docker_adapter = SandboxAdapter(
            _sandbox_type=SandboxType.DOCKER,
            service=docker_service,
            spec_service=docker_spec_service,
        )
        registry.register(docker_adapter)
        _logger.info("Docker sandbox registered")
    except Exception as e:
        _logger.warning(f"Failed to initialize Docker sandbox: {e}")

    # Build Firecracker sandbox adapter if daemon is available
    daemon_socket = os.environ.get(
        "OH_FIRECRACKER_MANAGER_SOCKET",
        "/var/run/fcvmd/fcvmd.sock",
    )
    _logger.info(f"Checking for Firecracker daemon socket at: {daemon_socket}")
    if os.path.exists(daemon_socket):
        try:
            from openhands.app_server.sandbox.firecracker_sandbox_service import (
                FirecrackerSandboxService,
            )

            # Create Firecracker spec service with preset specs
            fc_specs = [
                SandboxSpecInfo(
                    id="firecracker-vm",
                    name="Firecracker microVM",
                    type=SandboxType.FIRECRACKER,
                    description="Lightweight microVM with hardware-level isolation",
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
            _logger.info("Firecracker sandbox registered")
        except Exception as e:
            _logger.warning(f"Failed to initialize Firecracker sandbox: {e}")
    else:
        _logger.info(
            f"Firecracker daemon socket not found at {daemon_socket}, "
            "Firecracker sandbox type will not be available"
        )

    yield registry
