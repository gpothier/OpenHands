"""Firecracker-based sandbox service for running agent sandboxes in microVMs.

This module provides a SandboxService implementation that uses Firecracker
to run isolated microVMs instead of Docker containers. Firecracker provides
hardware-level isolation using KVM while maintaining fast startup times
(~125ms) and low memory overhead (<5 MiB per VM).

This implementation is a client to the external fcvmd daemon,
which handles VM lifecycle, networking, and kernel/rootfs building. The daemon
runs on the host and manages VMs that persist across OpenHands container restarts.

Requirements:
- fcvmd daemon running on host
- Socket mounted at OH_FIRECRACKER_MANAGER_SOCKET (default: /var/run/fcvmd/fcvmd.sock)

Architecture:
- This service communicates with the daemon via Unix socket
- Daemon manages VM lifecycle, networking, kernel/rootfs building
- VMs persist across OpenHands container restarts
- Multiple OpenHands instances share the same VM pool
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.preset_sandbox_spec_service import (
    PresetSandboxSpecService,
)
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    SSH,
    VSCODE,
    WORKER_1,
    WORKER_2,
    ExposedUrl,
    FirecrackerSandboxStartParams,
    SandboxInfo,
    SandboxPage,
    SandboxStartParams,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import (
    SESSION_API_KEY_VARIABLE,
    SandboxService,
    SandboxServiceInjector,
)
from openhands.app_server.sandbox.sandbox_spec_models import (
    ExposedPort,
    SandboxSpecInfo,
    SandboxType,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    DEFAULT_WORKING_DIR,
    SandboxSpecService,
    SandboxSpecServiceInjector,
    get_default_sandbox_env,
)
from openhands.app_server.services.injector import InjectorState

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import Request

_logger = logging.getLogger(__name__)


# Default daemon socket path
DEFAULT_DAEMON_SOCKET = '/var/run/fcvmd/fcvmd.sock'

# Default VM subnet (must match daemon's default)
DEFAULT_VM_SUBNET = '172.16.0.0/30'

# Environment variable name for webhook callback
WEBHOOK_CALLBACK_VARIABLE = 'WEBHOOK_CALLBACK_URL'

# Default exposed ports for Firecracker sandboxes (beyond AGENT_SERVER and VSCODE)
# These are merged with any spec-defined exposed_ports
DEFAULT_EXPOSED_PORTS = [
    ExposedPort(
        name=SSH,
        description='SSH server for Local VSCode Remote-SSH access',
        port=2222,
        url_template='ssh://{host}:{port}',
    ),
    ExposedPort(
        name=WORKER_1,
        description='First port for agent-started application servers',
        port=8011,
    ),
    ExposedPort(
        name=WORKER_2,
        description='Second port for agent-started application servers',
        port=8012,
    ),
]


def _compute_host_ip(vm_subnet: str) -> str:
    """Compute the first host IP from a VM subnet.

    The daemon allocates /30 subnets from the configured range.
    The first host_ip is always at offset .0.1 from the network prefix.
    This IP is reachable from all VMs on the TAP network.

    Args:
        vm_subnet: CIDR notation subnet (e.g., '172.16.0.0/30')

    Returns:
        First host IP (e.g., '172.16.0.1')
    """
    network = ipaddress.ip_network(vm_subnet, strict=False)
    octets = str(network.network_address).split('.')
    return f'{octets[0]}.{octets[1]}.0.1'


class DaemonClient:
    """Client for communicating with the fcvmd daemon."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def _send_request(
        self, method: str, path: str, body: dict | None = None, timeout: float = 300
    ) -> dict:
        """Send a request to the daemon and return the response."""
        request = {'method': method, 'path': path}
        if body:
            request['body'] = body

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(self.socket_path)

            # Send request
            data = json.dumps(request).encode() + b'\n'
            sock.sendall(data)

            # Read response
            response_data = b''
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                if b'\n' in response_data:
                    break

            sock.close()
            return json.loads(response_data.decode().strip())
        except socket.error as e:
            raise SandboxError(f'Failed to connect to daemon: {e}')
        except json.JSONDecodeError as e:
            raise SandboxError(f'Invalid response from daemon: {e}')

    def create_vm(
        self,
        vm_id: str,
        image: str,
        env_vars: dict[str, str] | None = None,
        entrypoint: list[str] | None = None,
        user: str | None = None,
        working_dir: str | None = None,
        exposed_ports: list[int] | None = None,
        disk_size_gb: int | None = None,
        ram_size_gb: int | None = None,
    ) -> dict:
        """Create a new VM.
        
        Args:
            vm_id: Unique VM identifier
            image: Container image to use for the VM
            env_vars: Environment variables to set in the VM (supports {host_ip} substitution)
            entrypoint: Command to run as the main service (overrides image entrypoint)
            user: User to run the entrypoint as (default: root)
            working_dir: Working directory for the entrypoint
            exposed_ports: List of VM ports to expose on the host (returns port_mappings)
            disk_size_gb: Storage size in GB for the VM's root filesystem
            ram_size_gb: RAM size in GB for the VM (default: ~1GB if not specified)
        """
        body = {
            'vm_id': vm_id,
            'image': image,
            'env_vars': env_vars or {},
        }
        if entrypoint:
            body['entrypoint'] = entrypoint
        if user:
            body['user'] = user
        if working_dir:
            body['working_dir'] = working_dir
        if exposed_ports:
            body['exposed_ports'] = exposed_ports
        if disk_size_gb:
            # Convert GB to bytes for the daemon API
            body['disk_size_bytes'] = disk_size_gb * 1024 * 1024 * 1024
        if ram_size_gb:
            # Convert GB to MiB for the daemon API
            body['mem_size_mib'] = ram_size_gb * 1024
        response = self._send_request('POST', '/vms', body, timeout=1800)
        if response.get('error'):
            raise SandboxError(response['error'])
        return response

    def get_vm(self, vm_id: str) -> dict | None:
        """Get VM info by ID."""
        response = self._send_request('GET', f'/vms/{vm_id}')
        if response.get('error'):
            return None
        return response

    def delete_vm(self, vm_id: str) -> bool:
        """Delete a VM."""
        response = self._send_request('DELETE', f'/vms/{vm_id}')
        return not response.get('error')

    def list_vms(self) -> list[dict]:
        """List all VMs."""
        response = self._send_request('GET', '/vms')
        return response.get('vms', [])

    def get_status(self) -> dict:
        """Get daemon status (health check)."""
        return self._send_request('GET', '/health')


@dataclass
class FirecrackerVM:
    """Represents a Firecracker microVM managed by the daemon."""

    vm_id: str
    guest_ip: str | None = None
    host_ip: str | None = None
    agent_server_port: int = 8000
    vscode_port: int = 8001
    session_api_key: str | None = None
    sandbox_spec_id: str | None = None
    status: str = 'starting'
    created_at: datetime = field(default_factory=utc_now)
    working_dir: str = '/workspace/project'
    exposed_ports: list[ExposedPort] = field(default_factory=list)
    # Port mappings from daemon: {vm_port: host_port}
    port_mappings: dict[int, int] = field(default_factory=dict)

    @classmethod
    def from_daemon_response(cls, data: dict) -> FirecrackerVM:
        """Create from daemon API response."""
        created_at = data.get('created_at')
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        elif created_at is None:
            created_at = utc_now()

        # port_mappings comes as {"2222": 45678} from JSON, convert keys to int
        port_mappings_raw = data.get('port_mappings') or {}
        port_mappings = {int(k): v for k, v in port_mappings_raw.items()}

        # Extract session_api_key from env_vars if available
        env_vars = data.get('env_vars') or {}
        session_api_key = env_vars.get(SESSION_API_KEY_VARIABLE)

        return cls(
            vm_id=data['vm_id'],
            guest_ip=data.get('guest_ip'),
            host_ip=data.get('host_ip'),
            # agent_server_port and vscode_port are now OpenHands-specific defaults
            # (the daemon no longer returns them)
            agent_server_port=8000,
            vscode_port=8001,
            status=data.get('status', 'unknown'),
            created_at=created_at,
            port_mappings=port_mappings,
            session_api_key=session_api_key,
        )

    def to_sandbox_status(self) -> SandboxStatus:
        """Convert daemon status to SandboxStatus."""
        status_map = {
            'starting': SandboxStatus.STARTING,
            'running': SandboxStatus.RUNNING,
            'stopped': SandboxStatus.PAUSED,  # No STOPPED in SandboxStatus
            'error': SandboxStatus.ERROR,
        }
        return status_map.get(self.status, SandboxStatus.MISSING)

    def to_sandbox_info(self, web_url: str | None = None) -> SandboxInfo:
        """Convert to SandboxInfo for API responses."""
        exposed_urls = None
        if self.guest_ip:
            agent_internal_url = f'http://{self.guest_ip}:{self.agent_server_port}'
            vscode_internal_url = f'http://{self.guest_ip}:{self.vscode_port}'

            if web_url:
                agent_external_url = f'{web_url}/agent/{self.vm_id}'
                vscode_external_url = (
                    f'{web_url}/vscode/{self.vm_id}/'
                    f'?tkn={self.session_api_key}&folder={self.working_dir}'
                )
            else:
                agent_external_url = agent_internal_url
                vscode_external_url = (
                    f'{vscode_internal_url}/?tkn={self.session_api_key}&folder={self.working_dir}'
                )

            exposed_urls = [
                ExposedUrl(
                    name=AGENT_SERVER,
                    url=agent_external_url,
                    port=self.agent_server_port,
                    internal_url=agent_internal_url,
                ),
                ExposedUrl(
                    name=VSCODE,
                    url=vscode_external_url,
                    port=self.vscode_port,
                    internal_url=vscode_internal_url,
                ),
            ]

            # Add additional ports from the sandbox spec
            # For URL templates, use web_url's hostname (external access) or guest_ip (local)
            # and the mapped host port (if available) for external access
            external_host = self.guest_ip
            if web_url:
                parsed = urlparse(web_url)
                if parsed.hostname:
                    external_host = parsed.hostname

            for exposed_port in self.exposed_ports:
                vm_port = exposed_port.port
                # Use mapped host port for external access, VM port internally
                host_port = self.port_mappings.get(vm_port, vm_port)
                internal_url = f'http://{self.guest_ip}:{vm_port}'

                if exposed_port.url_template:
                    # Use host port in template for external access
                    url = exposed_port.url_template.format(
                        host=external_host, port=host_port
                    )
                else:
                    url = f'http://{external_host}:{host_port}'
                exposed_urls.append(
                    ExposedUrl(
                        name=exposed_port.name,
                        url=url,
                        port=host_port,  # Report the host port, not VM port
                        internal_url=internal_url,
                    )
                )

        return SandboxInfo(
            id=self.vm_id,
            created_by_user_id=None,
            sandbox_spec_id=self.sandbox_spec_id or '',
            status=self.to_sandbox_status(),
            session_api_key=self.session_api_key,
            exposed_urls=exposed_urls,
            created_at=self.created_at,
        )


class FirecrackerSandboxService(SandboxService):
    """SandboxService implementation using Firecracker microVMs via daemon.

    This service communicates with the fcvmd daemon to manage VMs. The daemon
    handles all low-level details (networking, kernel/rootfs building, VM lifecycle).
    """

    def __init__(
        self,
        daemon_socket: str | None = None,
        sandbox_spec_service: SandboxSpecService | None = None,
        web_url: str | None = None,
        host_port: int | None = None,
        sdk_image: str | None = None,
        vm_subnet: str | None = None,
    ):
        """Initialize the Firecracker sandbox service.

        Args:
            daemon_socket: Path to daemon Unix socket (env: OH_FIRECRACKER_MANAGER_SOCKET)
            sandbox_spec_service: Service for managing sandbox specifications
            web_url: External URL where OpenHands is accessible (for proxy URLs)
            host_port: Port of the orchestrator (env: OH_SANDBOX_HOST_PORT, default 3000)
            sdk_image: Docker image to use for VMs (env: OH_FIRECRACKER_SDK_IMAGE)
            vm_subnet: CIDR for VM subnets (env: FIRECRACKER_VM_SUBNET, default 172.16.0.0/30)
        """
        self.daemon_socket = (
            daemon_socket
            or os.environ.get('OH_FIRECRACKER_MANAGER_SOCKET')
            or DEFAULT_DAEMON_SOCKET
        )
        self.sandbox_spec_service = sandbox_spec_service
        self.web_url = web_url or os.environ.get('OH_WEB_URL')
        self.host_port = host_port or int(os.environ.get('OH_SANDBOX_HOST_PORT', '3000'))
        self.sdk_image = (
            sdk_image
            or os.environ.get('OH_FIRECRACKER_SDK_IMAGE')
            or 'ghcr.io/openhands/agent-server:latest-python'
        )

        # Compute host_ip for internal URLs (MCP, callbacks from VMs)
        # This must match the daemon's subnet configuration
        vm_subnet = vm_subnet or os.environ.get('FIRECRACKER_VM_SUBNET', DEFAULT_VM_SUBNET)
        self.host_ip = _compute_host_ip(vm_subnet)

        self._client = DaemonClient(self.daemon_socket)
        # Local cache of VMs we've created (for sandbox_spec_id tracking)
        self._vms: dict[str, FirecrackerVM] = {}

        _logger.info(
            f'FirecrackerSandboxService initialized, daemon socket: {self.daemon_socket}, '
            f'host_ip: {self.host_ip}'
        )

    async def initialize(self) -> None:
        """Initialize the service and verify daemon connection."""
        if not os.path.exists(self.daemon_socket):
            raise SandboxError(
                f'Daemon socket not found: {self.daemon_socket}. '
                'Is fcvmd running?'
            )

        try:
            status = self._client.get_status()
            _logger.info(f'Connected to daemon: {status}')
        except SandboxError as e:
            raise SandboxError(f'Failed to connect to daemon: {e}')

    async def shutdown(self) -> None:
        """Shutdown the service."""
        _logger.info('FirecrackerSandboxService shutdown')

    def _ensure_default_exposed_ports(self, vm: 'FirecrackerVM') -> None:
        """Ensure VM has default exposed ports if none are set.

        This handles VMs that were created before default ports were added,
        or when the service cache doesn't have the VM's exposed_ports.
        """
        if not vm.exposed_ports:
            vm.exposed_ports = list(DEFAULT_EXPOSED_PORTS)

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get sandbox info by ID."""
        vm_data = self._client.get_vm(sandbox_id)
        if not vm_data:
            return None

        vm = FirecrackerVM.from_daemon_response(vm_data)
        # Restore locally-stored fields from cache
        if sandbox_id in self._vms:
            cached = self._vms[sandbox_id]
            vm.sandbox_spec_id = cached.sandbox_spec_id
            vm.session_api_key = cached.session_api_key
            vm.working_dir = cached.working_dir
            vm.exposed_ports = cached.exposed_ports
        self._ensure_default_exposed_ports(vm)
        return vm.to_sandbox_info(self.web_url)

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get sandbox by session API key."""
        # Search through local cache for matching session API key
        # (session_api_key is stored locally, not in the daemon)
        for vm_id, cached_vm in self._vms.items():
            if cached_vm.session_api_key == session_api_key:
                # Verify VM still exists in daemon
                vm_data = self._client.get_vm(vm_id)
                if vm_data:
                    vm = FirecrackerVM.from_daemon_response(vm_data)
                    vm.sandbox_spec_id = cached_vm.sandbox_spec_id
                    vm.session_api_key = cached_vm.session_api_key
                    vm.working_dir = cached_vm.working_dir
                    vm.exposed_ports = cached_vm.exposed_ports
                    self._ensure_default_exposed_ports(vm)
                    return vm.to_sandbox_info(self.web_url)
        return None

    def get_sandbox_host_ip(self, sandbox_id: str) -> str | None:
        """Get the host/gateway IP for a specific sandbox.

        Each Firecracker VM has its own TAP interface with a unique gateway IP.
        This method queries the daemon to get the specific host_ip for a sandbox.

        Args:
            sandbox_id: The sandbox/VM ID

        Returns:
            The host IP that the VM should use to reach the host, or None
        """
        vm_data = self._client.get_vm(sandbox_id)
        if not vm_data:
            return None
        return vm_data.get('host_ip')

    async def search_sandboxes(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxPage:
        """Search sandboxes (returns all Firecracker VMs)."""
        vms = self._client.list_vms()

        sandboxes = []
        for vm_data in vms:
            vm = FirecrackerVM.from_daemon_response(vm_data)
            if vm.vm_id in self._vms:
                cached = self._vms[vm.vm_id]
                vm.sandbox_spec_id = cached.sandbox_spec_id
                vm.working_dir = cached.working_dir
                vm.exposed_ports = cached.exposed_ports
            self._ensure_default_exposed_ports(vm)
            sandboxes.append(vm.to_sandbox_info(self.web_url))

        # Simple pagination - just return up to limit
        return SandboxPage(
            sandboxes=sandboxes[:limit],
            total=len(sandboxes),
            page=1,
            page_size=limit,
        )

    async def get_vscode_internal_url(self, short_sandbox_id: str) -> str | None:
        """Get the internal VSCode URL for a sandbox."""
        vm_id = short_sandbox_id
        if not vm_id.startswith('fc-'):
            vm_id = f'fc-{short_sandbox_id}'

        vm_data = self._client.get_vm(vm_id)
        if not vm_data or not vm_data.get('guest_ip'):
            return None

        port = vm_data.get('vscode_port', 8001)
        return f"http://{vm_data['guest_ip']}:{port}"

    async def get_agent_server_internal_url(self, short_sandbox_id: str) -> str | None:
        """Get the internal agent-server URL for a sandbox."""
        vm_id = short_sandbox_id
        if not vm_id.startswith('fc-'):
            vm_id = f'fc-{short_sandbox_id}'

        vm_data = self._client.get_vm(vm_id)
        if not vm_data or not vm_data.get('guest_ip'):
            return None

        port = vm_data.get('agent_server_port', 8000)
        return f"http://{vm_data['guest_ip']}:{port}"

    async def start_sandbox(
        self,
        params: SandboxStartParams | None = None,
    ) -> SandboxInfo:
        """Start a new Firecracker microVM sandbox."""
        if params is None:
            params = SandboxStartParams()

        # Extract Firecracker-specific parameters
        storage_size_gb: int | None = None
        ram_size_gb: int | None = None
        if isinstance(params, FirecrackerSandboxStartParams):
            storage_size_gb = params.storage_size_gb
            ram_size_gb = params.ram_size_gb

        vm_id = params.sandbox_id or f'fc-{secrets.token_hex(8)}'
        session_api_key = secrets.token_urlsafe(32)

        # Get environment variables (defaults + spec overrides + extra_env)
        env_vars = get_default_sandbox_env()
        working_dir = DEFAULT_WORKING_DIR
        spec_exposed_ports: list[ExposedPort] = []
        if params.sandbox_spec_id and self.sandbox_spec_service:
            sandbox_spec = await self.sandbox_spec_service.get_sandbox_spec(
                params.sandbox_spec_id
            )
            if sandbox_spec:
                if sandbox_spec.initial_env:
                    env_vars.update(sandbox_spec.initial_env)
                working_dir = sandbox_spec.working_dir
                spec_exposed_ports = sandbox_spec.exposed_ports
        if params.extra_env:
            env_vars.update(params.extra_env)

        # Merge default exposed ports with spec ports (spec ports override defaults)
        spec_port_names = {p.name for p in spec_exposed_ports}
        exposed_ports = [
            p for p in DEFAULT_EXPOSED_PORTS if p.name not in spec_port_names
        ] + list(spec_exposed_ports)

        # Add session API key
        env_vars[SESSION_API_KEY_VARIABLE] = session_api_key

        # Add webhook callback URL
        # Note: The daemon sets up host_ip, we use a placeholder that gets resolved
        env_vars[WEBHOOK_CALLBACK_VARIABLE] = (
            f'http://{{host_ip}}:{self.host_port}/api/v1/webhooks'
        )

        # Add VSCode base path for proxy support
        env_vars['OH_VSCODE_BASE_PATH'] = f'/vscode/{vm_id}'

        _logger.info(f'Creating VM {vm_id} with image {self.sdk_image}')

        # Extract port numbers to expose on the host
        # The daemon will set up port forwarding and return the mapped host ports
        ports_to_expose = [p.port for p in exposed_ports]

        # Call daemon to create VM
        # Don't pass entrypoint - let the daemon extract it from the image metadata
        # The SDK agent-server image has the correct entrypoint built in
        # Pass user='openhands' to run as the openhands user (consistent with Docker)
        response = self._client.create_vm(
            vm_id=vm_id,
            image=self.sdk_image,
            env_vars=env_vars,
            user='openhands',
            working_dir=working_dir,
            exposed_ports=ports_to_expose,
            disk_size_gb=storage_size_gb,
            ram_size_gb=ram_size_gb,
        )

        vm = FirecrackerVM.from_daemon_response(response)
        vm.sandbox_spec_id = params.sandbox_spec_id
        # Store session_api_key locally (not returned by daemon)
        vm.session_api_key = session_api_key
        vm.working_dir = working_dir
        vm.exposed_ports = exposed_ports
        # port_mappings is already populated from daemon response
        self._vms[vm_id] = vm

        _logger.info(f'VM {vm_id} created successfully, guest_ip={vm.guest_ip}')
        return vm.to_sandbox_info(self.web_url)

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        """Resume a paused sandbox (not supported - VMs are always running)."""
        vm_data = self._client.get_vm(sandbox_id)
        return vm_data is not None and vm_data.get('status') == 'running'

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Pause a sandbox (not supported - deletes instead)."""
        return await self.delete_sandbox(sandbox_id)

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete a sandbox."""
        self._vms.pop(sandbox_id, None)
        return self._client.delete_vm(sandbox_id)

    async def list_sandboxes(
        self,
        user_id: str | None = None,
        page: int = 1,
        page_size: int = 10,
        include_stopped: bool = True,
        sandbox_spec_id: str | None = None,
    ) -> SandboxPage:
        """List sandboxes."""
        vms = self._client.list_vms()

        # Filter by sandbox_spec_id if provided
        if sandbox_spec_id:
            vms = [
                vm
                for vm in vms
                if self._vms.get(vm['vm_id'], FirecrackerVM(vm_id='')).sandbox_spec_id
                == sandbox_spec_id
            ]

        # Filter out stopped if requested
        if not include_stopped:
            vms = [vm for vm in vms if vm.get('status') == 'running']

        total = len(vms)
        start = (page - 1) * page_size
        end = start + page_size
        page_vms = vms[start:end]

        sandboxes = []
        for vm_data in page_vms:
            vm = FirecrackerVM.from_daemon_response(vm_data)
            if vm.vm_id in self._vms:
                cached = self._vms[vm.vm_id]
                vm.sandbox_spec_id = cached.sandbox_spec_id
                vm.session_api_key = cached.session_api_key
                vm.working_dir = cached.working_dir
                vm.exposed_ports = cached.exposed_ports
            self._ensure_default_exposed_ports(vm)
            sandboxes.append(vm.to_sandbox_info(self.web_url))

        return SandboxPage(
            sandboxes=sandboxes,
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_sandbox_specs(
        self, user_id: str, is_public: bool | None = None
    ) -> list[SandboxSpecInfo]:
        """Get available sandbox specs."""
        if self.sandbox_spec_service:
            return await self.sandbox_spec_service.get_all_sandbox_specs(
                user_id, is_public
            )
        # Return a default spec for Firecracker
        return [
            SandboxSpecInfo(
                id='firecracker-default',
                name='Firecracker VM',
                sandbox_type=SandboxType.FIRECRACKER,
                description='Firecracker microVM sandbox',
            )
        ]

    async def get_sandbox_spec(self, sandbox_spec_id: str) -> SandboxSpecInfo | None:
        """Get a sandbox spec by ID."""
        if self.sandbox_spec_service:
            return await self.sandbox_spec_service.get_sandbox_spec(sandbox_spec_id)
        if sandbox_spec_id == 'firecracker-default':
            return SandboxSpecInfo(
                id='firecracker-default',
                name='Firecracker VM',
                sandbox_type=SandboxType.FIRECRACKER,
                description='Firecracker microVM sandbox',
            )
        return None


class FirecrackerSandboxServiceInjector(SandboxServiceInjector):
    """Dependency injector for FirecrackerSandboxService."""

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        from openhands.app_server.config import (
            get_global_config,
            get_sandbox_spec_service,
        )

        config = get_global_config()
        web_url = config.web_url

        async with get_sandbox_spec_service(state) as sandbox_spec_service:
            service = FirecrackerSandboxService(
                sandbox_spec_service=sandbox_spec_service,
                web_url=web_url,
            )
            await service.initialize()
            yield service
