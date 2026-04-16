"""Firecracker-based sandbox service for running agent sandboxes in microVMs.

This module provides a SandboxService implementation that uses Firecracker
to run isolated microVMs instead of Docker containers. Firecracker provides
hardware-level isolation using KVM while maintaining fast startup times
(~125ms) and low memory overhead (<5 MiB per VM).

Requirements:
- Linux host with KVM support (/dev/kvm accessible)
- Firecracker binary installed
- Pre-built kernel and rootfs images with agent-server

Architecture:
- Each sandbox is a Firecracker microVM with its own kernel and filesystem
- Communication with the agent-server inside the VM is via virtio-vsock or network
- VMs are managed via Firecracker's REST API over Unix domain sockets
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from pydantic import BaseModel, Field

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.preset_sandbox_spec_service import (
    PresetSandboxSpecService,
)
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    VSCODE,
    ExposedUrl,
    SandboxInfo,
    SandboxPage,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import (
    SESSION_API_KEY_VARIABLE,
    WEBHOOK_CALLBACK_VARIABLE,
    SandboxService,
    SandboxServiceInjector,
)
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxSpecInfo,
    SandboxType,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    SandboxSpecServiceInjector,
    get_agent_server_env,
)
from openhands.app_server.services.injector import InjectorState

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import Request

_logger = logging.getLogger(__name__)


# Default paths for Firecracker resources
DEFAULT_FIRECRACKER_BIN = '/usr/local/bin/firecracker'
DEFAULT_JAILER_BIN = '/usr/local/bin/jailer'
DEFAULT_KERNEL_PATH = '/var/lib/firecracker/vmlinux'
DEFAULT_ROOTFS_PATH = '/var/lib/firecracker/rootfs.ext4'
DEFAULT_SOCKETS_DIR = '/tmp/firecracker'

# Network configuration
DEFAULT_TAP_PREFIX = 'fc-tap'
# Default VM subnet in CIDR notation: 172.16.0.0/30
# The prefix (172.16) defines the IP range, the suffix (/30) defines subnet size per VM
# /30 = 4 addresses per subnet: network (.0), host (.1), guest (.2), broadcast (.3)
# Can be overridden via FIRECRACKER_VM_SUBNET environment variable
DEFAULT_VM_SUBNET = '172.16.0.0/30'
DEFAULT_AGENT_SERVER_PORT = 8000
# Must match the port the agent-server image uses for VSCode (same as Docker sandbox)
DEFAULT_VSCODE_PORT = 8001


def parse_vm_subnet(cidr: str) -> tuple[str, int, str]:
    """Parse a CIDR notation string into network prefix, subnet length, and netmask.

    Args:
        cidr: CIDR notation like '172.16.0.0/30'

    Returns:
        Tuple of (network_prefix, subnet_prefix_len, netmask)
        e.g., ('172.16', 30, '255.255.255.252') for '172.16.0.0/30'
    """
    import ipaddress

    network = ipaddress.ip_network(cidr, strict=False)
    # Extract first two octets as the prefix for our allocation scheme
    octets = str(network.network_address).split('.')
    network_prefix = f'{octets[0]}.{octets[1]}'
    netmask = str(network.netmask)
    return network_prefix, network.prefixlen, netmask


@dataclass
class FirecrackerVM:
    """Represents a running Firecracker microVM."""

    vm_id: str
    socket_path: str
    pid: int | None = None
    tap_device: str | None = None
    guest_ip: str | None = None
    host_ip: str | None = None
    rootfs_path: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    session_api_key: str | None = None
    sandbox_spec_id: str | None = None
    status: SandboxStatus = SandboxStatus.STARTING

    def to_sandbox_info(self, web_url: str | None = None) -> SandboxInfo:
        """Convert to SandboxInfo for API responses.

        Args:
            web_url: External URL where OpenHands is accessible. If provided,
                the exposed URL will point to the agent proxy instead of the
                direct VM IP (which is not externally routable).
        """
        exposed_urls = None
        if self.guest_ip:
            # Internal URL is always the direct VM IP (for backend proxy use)
            agent_internal_url = f'http://{self.guest_ip}:{DEFAULT_AGENT_SERVER_PORT}'
            vscode_internal_url = f'http://{self.guest_ip}:{DEFAULT_VSCODE_PORT}'

            # External URL: use proxy if web_url is available, otherwise direct IP
            if web_url:
                # Route through the agent proxy: {web_url}/agent/{sandbox_id}
                agent_external_url = f'{web_url}/agent/{self.vm_id}'
                # VS Code goes through the vscode proxy with token
                vscode_external_url = (
                    f'{web_url}/vscode/{self.vm_id}/'
                    f'?tkn={self.session_api_key}&folder=/workspace'
                )
            else:
                # Fallback to direct IP (only works if VM is accessible)
                agent_external_url = agent_internal_url
                vscode_external_url = f'{vscode_internal_url}/?tkn={self.session_api_key}&folder=/workspace'

            exposed_urls = [
                ExposedUrl(
                    name=AGENT_SERVER,
                    url=agent_external_url,
                    port=DEFAULT_AGENT_SERVER_PORT,
                    internal_url=agent_internal_url,
                ),
                ExposedUrl(
                    name=VSCODE,
                    url=vscode_external_url,
                    port=DEFAULT_VSCODE_PORT,
                    internal_url=vscode_internal_url,
                ),
            ]

        return SandboxInfo(
            id=self.vm_id,
            created_by_user_id=None,
            sandbox_spec_id=self.sandbox_spec_id or '',
            status=self.status,
            session_api_key=self.session_api_key,
            exposed_urls=exposed_urls,
            created_at=self.created_at,
        )


class FirecrackerConfig(BaseModel):
    """Configuration for a Firecracker microVM."""

    boot_source: dict
    drives: list[dict]
    network_interfaces: list[dict] | None = None
    machine_config: dict | None = None
    vsock: dict | None = None


class FirecrackerSandboxService(SandboxService):
    """SandboxService implementation using Firecracker microVMs.

    This service manages the lifecycle of Firecracker microVMs, providing
    hardware-isolated sandboxes for running the OpenHands agent-server.

    Each VM gets:
    - Its own Linux kernel
    - A copy-on-write rootfs with the agent-server
    - A TAP network interface for host-guest communication
    - Optionally, a vsock interface for fast IPC
    """

    def __init__(
        self,
        firecracker_bin: str | None = None,
        jailer_bin: str | None = None,
        kernel_path: str | None = None,
        base_rootfs_path: str | None = None,
        sockets_dir: str | None = None,
        vcpu_count: int | None = None,
        mem_size_mib: int | None = None,
        use_jailer: bool = False,
        sandbox_spec_service: SandboxSpecService | None = None,
        vm_subnet: str | None = None,
        web_url: str | None = None,
        debug_console: bool = False,
        host_port: int | None = None,
    ):
        """Initialize the Firecracker sandbox service.

        All configuration can be set via constructor arguments or environment
        variables. Environment variables are used as fallbacks when arguments
        are not provided.

        Args:
            firecracker_bin: Path to firecracker binary (env: FIRECRACKER_BIN)
            jailer_bin: Path to jailer binary (optional, for production use)
            kernel_path: Path to guest kernel image (env: FIRECRACKER_KERNEL_PATH)
            base_rootfs_path: Path to base rootfs image (env: FIRECRACKER_ROOTFS_PATH)
            sockets_dir: Directory for Unix sockets (env: FIRECRACKER_SOCKETS_DIR)
            vcpu_count: Number of vCPUs per VM (env: FIRECRACKER_VCPU_COUNT)
            mem_size_mib: Memory size in MiB per VM (env: FIRECRACKER_MEM_SIZE_MIB)
            use_jailer: Whether to use jailer for additional isolation
            sandbox_spec_service: Service for managing sandbox specifications
            vm_subnet: CIDR for VM subnets (env: FIRECRACKER_VM_SUBNET)
            web_url: External URL where OpenHands is accessible (for proxy URLs)
            debug_console: Run VMs in screen sessions (env: FIRECRACKER_DEBUG_CONSOLE)
            host_port: Port of the orchestrator (env: OH_SANDBOX_HOST_PORT, default 3000)
        """
        # Read configuration from arguments or environment variables
        self.firecracker_bin = (
            firecracker_bin
            or os.environ.get('FIRECRACKER_BIN')
            or DEFAULT_FIRECRACKER_BIN
        )
        self.jailer_bin = jailer_bin or DEFAULT_JAILER_BIN
        self.kernel_path = (
            kernel_path
            or os.environ.get('FIRECRACKER_KERNEL_PATH')
            or DEFAULT_KERNEL_PATH
        )
        self.base_rootfs_path = (
            base_rootfs_path
            or os.environ.get('FIRECRACKER_ROOTFS_PATH')
            or DEFAULT_ROOTFS_PATH
        )
        self.sockets_dir = (
            sockets_dir
            or os.environ.get('FIRECRACKER_SOCKETS_DIR')
            or DEFAULT_SOCKETS_DIR
        )

        # Numeric configs with env var fallbacks
        if vcpu_count is not None:
            self.vcpu_count = vcpu_count
        elif os.environ.get('FIRECRACKER_VCPU_COUNT'):
            self.vcpu_count = int(os.environ['FIRECRACKER_VCPU_COUNT'])
        else:
            self.vcpu_count = 2

        if mem_size_mib is not None:
            self.mem_size_mib = mem_size_mib
        elif os.environ.get('FIRECRACKER_MEM_SIZE_MIB'):
            self.mem_size_mib = int(os.environ['FIRECRACKER_MEM_SIZE_MIB'])
        else:
            self.mem_size_mib = 1024

        self.use_jailer = use_jailer
        self.sandbox_spec_service = sandbox_spec_service
        self.web_url = web_url

        # Host port for webhook callbacks (orchestrator's port)
        # Use same env var as Docker sandbox for consistency
        if host_port is not None:
            self.host_port = host_port
        elif os.environ.get('OH_SANDBOX_HOST_PORT'):
            self.host_port = int(os.environ['OH_SANDBOX_HOST_PORT'])
        else:
            self.host_port = 3000  # Default OpenHands port

        # Boolean config with env var fallback
        if debug_console:
            self.debug_console = True
        else:
            env_val = os.environ.get('FIRECRACKER_DEBUG_CONSOLE', '').lower()
            self.debug_console = env_val in ('true', '1', 'yes')

        if self.debug_console:
            _logger.info('Debug console enabled: VMs will run in screen sessions')

        # Parse VM subnet configuration
        vm_subnet_config = (
            vm_subnet or os.environ.get('FIRECRACKER_VM_SUBNET') or DEFAULT_VM_SUBNET
        )
        self.network_prefix, self.subnet_prefix_len, self.netmask = parse_vm_subnet(
            vm_subnet_config
        )
        _logger.info(
            f'Firecracker network config: prefix={self.network_prefix}, '
            f'subnet_len=/{self.subnet_prefix_len}, netmask={self.netmask}'
        )

        # In-memory registry of running VMs
        self._vms: dict[str, FirecrackerVM] = {}

        # IP address pool for guest VMs (simple sequential allocation)
        # Start from index 0 (first /30 subnet)
        self._next_ip_index = 0

        # Ensure sockets directory exists
        Path(self.sockets_dir).mkdir(parents=True, exist_ok=True)

        # Compute a representative host IP for the MCP/callback URLs
        # Using the first subnet's host IP (e.g., 172.16.0.1 for 172.16.0.0/30)
        # This IP will be reachable from any VM on the TAP network
        self.host_ip = f'{self.network_prefix}.0.1'

        # Setup NAT/IP forwarding for VM internet access (inside container)
        self._setup_nat_forwarding()

    def _setup_nat_forwarding(self) -> None:
        """Setup NAT/IP forwarding for VM internet access.

        Since the TAP devices are created inside this container, the NAT rules
        must also be inside the container. This method attempts to:
        1. Enable IP forwarding (if not already enabled)
        2. Add iptables MASQUERADE rule for the VM subnet

        Note: Requires CAP_NET_ADMIN capability in the container.
        """
        vm_subnet = f'{self.network_prefix}.0.0/16'

        # Check/enable IP forwarding
        try:
            with open('/proc/sys/net/ipv4/ip_forward') as f:
                if f.read().strip() != '1':
                    # Try to enable it
                    try:
                        subprocess.run(
                            ['sysctl', '-w', 'net.ipv4.ip_forward=1'],
                            capture_output=True,
                            check=True,
                        )
                        _logger.info('Enabled IP forwarding')
                    except subprocess.CalledProcessError as e:
                        _logger.warning(
                            f'Could not enable IP forwarding: {e}. '
                            'VMs may not have internet access. '
                            'Run container with --sysctl net.ipv4.ip_forward=1'
                        )
                else:
                    _logger.debug('IP forwarding already enabled')
        except OSError as e:
            _logger.warning(f'Could not check IP forwarding status: {e}')

        # Add MASQUERADE rule if not exists
        try:
            # Check if rule already exists
            result = subprocess.run(
                [
                    'iptables',
                    '-t',
                    'nat',
                    '-C',
                    'POSTROUTING',
                    '-s',
                    vm_subnet,
                    '-j',
                    'MASQUERADE',
                ],
                capture_output=True,
            )

            if result.returncode != 0:
                # Rule doesn't exist, try to add it
                add_result = subprocess.run(
                    [
                        'iptables',
                        '-t',
                        'nat',
                        '-A',
                        'POSTROUTING',
                        '-s',
                        vm_subnet,
                        '-j',
                        'MASQUERADE',
                    ],
                    capture_output=True,
                )
                if add_result.returncode == 0:
                    _logger.info(f'Added NAT MASQUERADE rule for {vm_subnet}')
                else:
                    _logger.warning(
                        f'Could not add NAT MASQUERADE rule: {add_result.stderr.decode()}. '
                        'VMs may not have internet access. '
                        f'Run: iptables -t nat -A POSTROUTING -s {vm_subnet} -j MASQUERADE'
                    )
            else:
                _logger.debug(f'NAT MASQUERADE rule already exists for {vm_subnet}')
        except Exception as e:
            _logger.warning(
                f'Could not setup iptables NAT rules: {e}. '
                'VMs may not have internet access.'
            )

    def _validate_prerequisites(self) -> None:
        """Validate that all prerequisites are met."""
        # Check KVM access
        if not os.path.exists('/dev/kvm'):
            raise SandboxError(
                'KVM not available. Firecracker requires /dev/kvm. '
                'Ensure your host supports hardware virtualization.'
            )
        if not os.access('/dev/kvm', os.R_OK | os.W_OK):
            raise SandboxError(
                'No read/write access to /dev/kvm. '
                'Add your user to the kvm group or run with appropriate permissions.'
            )

        # Check firecracker binary
        if not os.path.exists(self.firecracker_bin):
            raise SandboxError(
                f'Firecracker binary not found at {self.firecracker_bin}. '
                'Install Firecracker from: https://github.com/firecracker-microvm/firecracker/releases'
            )

        # Check kernel
        if not os.path.exists(self.kernel_path):
            raise SandboxError(
                f'Kernel image not found at {self.kernel_path}. '
                'Download or build a kernel for Firecracker.'
            )

        # Check rootfs
        if not os.path.exists(self.base_rootfs_path):
            raise SandboxError(
                f'Base rootfs not found at {self.base_rootfs_path}. '
                'Build a rootfs with the agent-server installed.'
            )

    def _allocate_ip(self) -> tuple[str, str, int]:
        """Allocate IP addresses for a new VM.

        Each VM gets a subnet based on the configured prefix length.
        For example, with /30 subnets (4 addresses: network, host, guest, broadcast):
          - Index 0: x.x.0.0/30 → host .1, guest .2
          - Index 1: x.x.0.4/30 → host .5, guest .6
          - Index 64: x.x.1.0/30 → host .1, guest .2 (rolls over)

        Returns:
            Tuple of (host_ip, guest_ip, ip_index)
        """
        ip_index = self._next_ip_index
        self._next_ip_index += 1

        # Calculate subnet size from prefix length (e.g., /30 = 4 addresses)
        subnet_size = 2 ** (32 - self.subnet_prefix_len)

        # Calculate subnet base address offset
        subnet_base = ip_index * subnet_size

        # Calculate third and fourth octets
        third_octet = subnet_base // 256
        fourth_octet_base = subnet_base % 256

        # Host is at offset +1, guest is at offset +2 within the subnet
        host_ip = f'{self.network_prefix}.{third_octet}.{fourth_octet_base + 1}'
        guest_ip = f'{self.network_prefix}.{third_octet}.{fourth_octet_base + 2}'

        return host_ip, guest_ip, ip_index

    async def _setup_network(self, vm_id: str, host_ip: str, ip_index: int) -> str:
        """Set up TAP network interface for VM.

        Args:
            vm_id: The VM identifier
            host_ip: IP address for the host side of the TAP
            ip_index: Index used for TAP device naming

        Returns:
            TAP device name
        """
        tap_device = f'{DEFAULT_TAP_PREFIX}{ip_index}'

        try:
            # Delete existing TAP if present
            subprocess.run(
                ['ip', 'link', 'del', tap_device],
                capture_output=True,
                check=False,
            )

            # Create TAP device
            subprocess.run(
                ['ip', 'tuntap', 'add', 'dev', tap_device, 'mode', 'tap'],
                capture_output=True,
                check=True,
            )

            # Assign IP address
            subprocess.run(
                [
                    'ip',
                    'addr',
                    'add',
                    f'{host_ip}/{self.subnet_prefix_len}',
                    'dev',
                    tap_device,
                ],
                capture_output=True,
                check=True,
            )

            # Bring up the interface
            subprocess.run(
                ['ip', 'link', 'set', 'dev', tap_device, 'up'],
                capture_output=True,
                check=True,
            )

            _logger.info(f'Created TAP device {tap_device} with IP {host_ip}')
            return tap_device

        except subprocess.CalledProcessError as e:
            raise SandboxError(
                f'Failed to set up network for VM {vm_id}: {e.stderr.decode() if e.stderr else str(e)}'
            ) from e

    async def _cleanup_network(self, tap_device: str) -> None:
        """Clean up TAP network interface."""
        try:
            subprocess.run(
                ['ip', 'link', 'del', tap_device],
                capture_output=True,
                check=False,
            )
            _logger.debug(f'Deleted TAP device {tap_device}')
        except Exception as e:
            _logger.warning(f'Failed to delete TAP device {tap_device}: {e}')

    async def _create_rootfs_copy(
        self, vm_id: str, env_vars: dict[str, str] | None = None
    ) -> str:
        """Create a copy-on-write copy of the base rootfs for a VM.

        Uses qemu-img to create a qcow2 overlay, or falls back to a full copy
        if qemu-img is not available.

        Args:
            vm_id: The VM identifier
            env_vars: Environment variables to inject into the rootfs

        Returns:
            Path to the VM's rootfs
        """
        rootfs_dir = os.path.join(self.sockets_dir, vm_id)
        Path(rootfs_dir).mkdir(parents=True, exist_ok=True)
        rootfs_path = os.path.join(rootfs_dir, 'rootfs.ext4')

        # Try to create a sparse copy (copy-on-write not directly supported for ext4)
        # For production, consider using qcow2 with overlay or btrfs snapshots
        try:
            # Use cp --reflink for CoW on supported filesystems (btrfs, xfs)
            subprocess.run(
                [
                    'cp',
                    '--reflink=auto',
                    '--sparse=always',
                    self.base_rootfs_path,
                    rootfs_path,
                ],
                capture_output=True,
                check=True,
            )
            _logger.info(f'Created rootfs copy at {rootfs_path}')
        except subprocess.CalledProcessError:
            # Fall back to regular copy
            shutil.copy2(self.base_rootfs_path, rootfs_path)
            _logger.info(f'Created rootfs copy at {rootfs_path} (full copy)')

        # Inject environment variables into the rootfs
        if env_vars:
            await self._inject_env_vars(rootfs_path, env_vars)

        return rootfs_path

    async def _inject_env_vars(
        self, rootfs_path: str, env_vars: dict[str, str]
    ) -> None:
        """Inject environment variables into the rootfs.

        Uses debugfs to write an environment file directly to the ext4 image
        without requiring mount capabilities. The agent-server systemd service
        will read from /etc/agent-server.env on startup.

        Args:
            rootfs_path: Path to the ext4 rootfs image
            env_vars: Environment variables to inject
        """
        # Create env file content
        env_content = '\n'.join(f'{k}={v}' for k, v in env_vars.items()) + '\n'

        # Write to a temporary file first
        tmp_env_file = Path(rootfs_path).parent / 'agent-server.env'
        tmp_env_file.write_text(env_content)

        try:
            # Use debugfs to write the file into the ext4 image
            # debugfs -w -R "write <local_file> <path_in_fs>" <device>
            result = subprocess.run(
                [
                    'debugfs',
                    '-w',
                    '-R',
                    f'write {tmp_env_file} /etc/agent-server.env',
                    rootfs_path,
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                _logger.warning(
                    f'debugfs failed to inject env vars: {result.stderr}. '
                    'Env vars will not be available in the VM.'
                )
            else:
                _logger.info(f'Injected {len(env_vars)} env vars into rootfs')

        finally:
            # Clean up temp file
            tmp_env_file.unlink(missing_ok=True)

    def _generate_vm_config(
        self,
        vm: FirecrackerVM,
        env_vars: dict[str, str] | None = None,
    ) -> FirecrackerConfig:
        """Generate Firecracker configuration for a VM.

        Args:
            vm: The VM to configure
            env_vars: Environment variables to set in the guest

        Returns:
            FirecrackerConfig ready to be sent to Firecracker API
        """
        # Boot arguments include network configuration and env vars
        # Note: We do NOT pass pci=off because modern Firecracker uses ACPI+PCI
        # to configure virtio devices (not kernel cmdline MMIO parameters)
        boot_args = (
            f'console=ttyS0 reboot=k panic=1 '
            f'ip={vm.guest_ip}::{vm.host_ip}:{self.netmask}::eth0:off '
        )

        # Add environment variables as kernel parameters (limited approach)
        # For production, use cloud-init or inject into rootfs
        if env_vars:
            # Pass session API key through kernel cmdline (will be picked up by init)
            if SESSION_API_KEY_VARIABLE in env_vars:
                boot_args += f'OH_SESSION_API_KEY={env_vars[SESSION_API_KEY_VARIABLE]} '

        config = FirecrackerConfig(
            boot_source={
                'kernel_image_path': self.kernel_path,
                'boot_args': boot_args.strip(),
            },
            drives=[
                {
                    'drive_id': 'rootfs',
                    'path_on_host': vm.rootfs_path,
                    'is_root_device': True,
                    'is_read_only': False,
                }
            ],
            machine_config={
                'vcpu_count': self.vcpu_count,
                'mem_size_mib': self.mem_size_mib,
            },
        )

        # Add network interface if configured
        if vm.tap_device:
            # MAC address derived from guest IP for consistency
            ip_parts = vm.guest_ip.split('.') if vm.guest_ip else ['0', '0', '0', '2']
            mac = f'06:00:AC:{int(ip_parts[1]):02X}:{int(ip_parts[2]):02X}:{int(ip_parts[3]):02X}'
            config.network_interfaces = [
                {
                    'iface_id': 'eth0',
                    'guest_mac': mac,
                    'host_dev_name': vm.tap_device,
                }
            ]

        return config

    async def _send_api_request(
        self,
        socket_path: str,
        method: str,
        endpoint: str,
        data: dict | None = None,
    ) -> dict | None:
        """Send a request to the Firecracker API.

        Args:
            socket_path: Path to the Firecracker Unix socket
            method: HTTP method (GET, PUT, PATCH)
            endpoint: API endpoint (e.g., '/boot-source')
            data: JSON data to send

        Returns:
            Response data or None
        """
        connector = aiohttp.UnixConnector(path=socket_path)
        async with aiohttp.ClientSession(connector=connector) as session:
            url = f'http://localhost{endpoint}'

            if data is not None:
                async with session.request(method, url, json=data) as response:
                    if response.status >= 400:
                        text = await response.text()
                        raise SandboxError(
                            f'Firecracker API error: {response.status} - {text}'
                        )
                    if response.content_length and response.content_length > 0:
                        return await response.json()
                    return None
            else:
                async with session.request(method, url) as response:
                    if response.status >= 400:
                        text = await response.text()
                        raise SandboxError(
                            f'Firecracker API error: {response.status} - {text}'
                        )
                    if response.content_length and response.content_length > 0:
                        return await response.json()
                    return None

    async def _start_firecracker_process(self, vm: FirecrackerVM) -> int:
        """Start the Firecracker process for a VM.

        Args:
            vm: The VM to start

        Returns:
            PID of the Firecracker process (or screen process if debug_console)
        """
        # Create log file for VM console output
        vm_dir = Path(self.sockets_dir) / vm.vm_id
        vm_dir.mkdir(parents=True, exist_ok=True)
        log_file = vm_dir / 'console.log'

        firecracker_cmd = [
            self.firecracker_bin,
            '--api-sock',
            vm.socket_path,
        ]

        if self.debug_console:
            # Run Firecracker inside screen for interactive console access
            # Screen session name is the VM ID for easy identification
            screen_name = vm.vm_id
            cmd = [
                'screen',
                '-dmS',
                screen_name,
                '-L',
                '-Logfile',
                str(log_file),
                *firecracker_cmd,
            ]
            _logger.info(f'Starting Firecracker in screen session: {screen_name}')
            _logger.info(f'Attach with: screen -r {screen_name}')
            _logger.info(f'VM console log: {log_file}')

            process = subprocess.Popen(
                cmd,
                start_new_session=True,
            )
        else:
            cmd = firecracker_cmd
            _logger.info(f'Starting Firecracker: {" ".join(cmd)}')
            _logger.info(f'VM console log: {log_file}')

            # Start Firecracker with output logged to file
            with open(log_file, 'w') as log_fd:
                process = subprocess.Popen(
                    cmd,
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

        # Wait for socket to be created
        for _ in range(50):  # 5 second timeout
            if os.path.exists(vm.socket_path):
                break
            await asyncio.sleep(0.1)
        else:
            # Log any output before killing
            if log_file.exists():
                _logger.error(
                    f'Firecracker output before timeout:\n{log_file.read_text()}'
                )
            process.kill()
            raise SandboxError(f'Firecracker socket not created at {vm.socket_path}')

        _logger.info(f'Started Firecracker process with PID {process.pid}')
        return process.pid

    async def _configure_and_start_vm(
        self,
        vm: FirecrackerVM,
        config: FirecrackerConfig,
    ) -> None:
        """Configure and start a Firecracker VM via API.

        Args:
            vm: The VM to configure
            config: The VM configuration
        """
        # Set boot source
        await self._send_api_request(
            vm.socket_path, 'PUT', '/boot-source', config.boot_source
        )

        # Set drives
        for drive in config.drives:
            await self._send_api_request(
                vm.socket_path, 'PUT', f'/drives/{drive["drive_id"]}', drive
            )

        # Set machine config
        if config.machine_config:
            await self._send_api_request(
                vm.socket_path, 'PUT', '/machine-config', config.machine_config
            )

        # Set network interfaces
        if config.network_interfaces:
            for iface in config.network_interfaces:
                await self._send_api_request(
                    vm.socket_path,
                    'PUT',
                    f'/network-interfaces/{iface["iface_id"]}',
                    iface,
                )

        # Start the VM
        await self._send_api_request(
            vm.socket_path, 'PUT', '/actions', {'action_type': 'InstanceStart'}
        )

        _logger.info(f'VM {vm.vm_id} started successfully')

    async def search_sandboxes(
        self,
        page_id: str | None = None,
        limit: int = 100,
    ) -> SandboxPage:
        """Search for running Firecracker sandboxes."""
        sandboxes = [vm.to_sandbox_info(self.web_url) for vm in self._vms.values()]

        # Apply pagination
        start_idx = int(page_id) if page_id else 0
        end_idx = start_idx + limit
        page_sandboxes = sandboxes[start_idx:end_idx]

        next_page_id = str(end_idx) if end_idx < len(sandboxes) else None

        return SandboxPage(items=page_sandboxes, next_page_id=next_page_id)

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get a Firecracker sandbox by ID."""
        vm = self._vms.get(sandbox_id)
        return vm.to_sandbox_info(self.web_url) if vm else None

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get a sandbox by its session API key."""
        for vm in self._vms.values():
            if vm.session_api_key == session_api_key:
                return vm.to_sandbox_info(self.web_url)
        return None

    async def get_vscode_internal_url(self, short_sandbox_id: str) -> str | None:
        """Return the internal VS Code URL for the given sandbox ID.

        For Firecracker VMs, this returns the direct VM IP with VS Code port.
        The proxy uses this to forward requests to the VM.
        """
        vm = self._vms.get(short_sandbox_id)
        if vm and vm.guest_ip:
            return f'http://{vm.guest_ip}:{DEFAULT_VSCODE_PORT}'
        return None

    async def get_agent_server_internal_url(self, short_sandbox_id: str) -> str | None:
        """Return the internal agent-server URL for the given sandbox ID.

        For Firecracker VMs, the guest IP is only accessible from the OpenHands
        container, so the proxy must forward all traffic.
        """
        # Firecracker sandbox IDs start with 'fc-', look up directly
        sandbox_id = short_sandbox_id
        if not sandbox_id.startswith('fc-'):
            sandbox_id = f'fc-{short_sandbox_id}'

        vm = self._vms.get(sandbox_id)
        if not vm or not vm.guest_ip:
            return None
        return f'http://{vm.guest_ip}:{DEFAULT_AGENT_SERVER_PORT}'

    async def start_sandbox(
        self, sandbox_spec_id: str | None = None, sandbox_id: str | None = None
    ) -> SandboxInfo:
        """Start a new Firecracker microVM sandbox.

        Args:
            sandbox_spec_id: ID of the sandbox spec to use (optional)
            sandbox_id: Custom sandbox ID (optional, generates one if not provided)

        Returns:
            SandboxInfo for the new sandbox
        """
        self._validate_prerequisites()

        vm_id = sandbox_id or f'fc-{secrets.token_hex(8)}'
        socket_path = os.path.join(self.sockets_dir, f'{vm_id}.sock')
        session_api_key = secrets.token_urlsafe(32)

        # Allocate network resources
        host_ip, guest_ip, ip_index = self._allocate_ip()

        # Create VM record
        vm = FirecrackerVM(
            vm_id=vm_id,
            socket_path=socket_path,
            guest_ip=guest_ip,
            host_ip=host_ip,
            session_api_key=session_api_key,
            sandbox_spec_id=sandbox_spec_id,
            status=SandboxStatus.STARTING,
        )

        try:
            # Set up network
            vm.tap_device = await self._setup_network(vm_id, host_ip, ip_index)

            # Get environment variables from sandbox spec
            env_vars: dict[str, str] = {}
            if sandbox_spec_id and self.sandbox_spec_service:
                sandbox_spec = await self.sandbox_spec_service.get_sandbox_spec(
                    sandbox_spec_id
                )
                if sandbox_spec and sandbox_spec.initial_env:
                    env_vars.update(sandbox_spec.initial_env)
            else:
                # Fallback: get agent server env directly if no spec service
                env_vars.update(get_agent_server_env())

            # Add session API key (must be set)
            env_vars[SESSION_API_KEY_VARIABLE] = session_api_key

            # Add webhook callback URL for agent-server to call back to orchestrator
            # The VM reaches the host via the TAP device's host IP
            env_vars[WEBHOOK_CALLBACK_VARIABLE] = (
                f'http://{host_ip}:{self.host_port}/api/v1/webhooks'
            )

            # Set VSCode base path for proxy support
            # This tells the agent-server to configure OpenVSCode Server with
            # --server-base-path so URLs match the proxy route
            env_vars['OH_VSCODE_BASE_PATH'] = f'/vscode/{vm_id}'

            # Create rootfs copy with env vars injected
            vm.rootfs_path = await self._create_rootfs_copy(vm_id, env_vars)

            # Generate configuration
            config = self._generate_vm_config(vm, env_vars)

            # Start Firecracker process
            vm.pid = await self._start_firecracker_process(vm)

            # Configure and start the VM
            await self._configure_and_start_vm(vm, config)

            vm.status = SandboxStatus.RUNNING
            self._vms[vm_id] = vm

            _logger.info(f'Firecracker sandbox {vm_id} started at {guest_ip}')
            return vm.to_sandbox_info(self.web_url)

        except Exception as e:
            # Cleanup on failure
            await self._cleanup_vm(vm)
            raise SandboxError(f'Failed to start Firecracker sandbox: {e}') from e

    async def _cleanup_vm(self, vm: FirecrackerVM) -> None:
        """Clean up resources for a VM."""
        # Kill process
        if vm.pid:
            try:
                os.kill(vm.pid, 9)
            except ProcessLookupError:
                pass

        # Clean up network
        if vm.tap_device:
            await self._cleanup_network(vm.tap_device)

        # Clean up files
        rootfs_dir = os.path.join(self.sockets_dir, vm.vm_id)
        if os.path.exists(rootfs_dir):
            shutil.rmtree(rootfs_dir, ignore_errors=True)

        if os.path.exists(vm.socket_path):
            os.remove(vm.socket_path)

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        """Resume a paused Firecracker sandbox.

        Note: Firecracker supports snapshotting for pause/resume, but this
        requires additional implementation. For now, we return False.
        """
        vm = self._vms.get(sandbox_id)
        if not vm:
            return False

        if vm.status == SandboxStatus.RUNNING:
            return True

        # TODO: Implement snapshot-based resume
        _logger.warning('Firecracker pause/resume via snapshots not yet implemented')
        return False

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Pause a Firecracker sandbox.

        Note: Firecracker supports snapshotting for pause/resume, but this
        requires additional implementation. For now, we just stop the VM.
        """
        vm = self._vms.get(sandbox_id)
        if not vm:
            return False

        if vm.status == SandboxStatus.PAUSED:
            return True

        # For now, pausing means stopping
        # TODO: Implement proper snapshotting
        return await self.delete_sandbox(sandbox_id)

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete a Firecracker sandbox."""
        vm = self._vms.pop(sandbox_id, None)
        if not vm:
            return False

        await self._cleanup_vm(vm)
        _logger.info(f'Deleted Firecracker sandbox {sandbox_id}')
        return True


def get_default_firecracker_sandbox_specs() -> list[SandboxSpecInfo]:
    """Get the default list of sandbox specs for Firecracker-based sandboxes."""
    return [
        SandboxSpecInfo(
            id='firecracker-default',
            name='Firecracker MicroVM',
            type=SandboxType.FIRECRACKER,
            description='Isolated microVM sandbox using Firecracker with KVM hardware virtualization',
            command=None,  # Firecracker doesn't use command like Docker
            initial_env={
                # VSCode configuration (port uses agent-server's default: 8001)
                'OPENVSCODE_SERVER_ROOT': '/openhands/.openvscode-server',
                # Agent server configuration
                'OH_ENABLE_VNC': '0',
                'LOG_JSON': 'true',
                'OH_CONVERSATIONS_PATH': '/workspace/conversations',
                'OH_BASH_EVENTS_DIR': '/workspace/bash_events',
                'PYTHONUNBUFFERED': '1',
                'ENV_LOG_LEVEL': '20',
                # Include auto-forwarded env vars (LLM_*, etc.)
                **get_agent_server_env(),
            },
            working_dir='/workspace/project',
            kvm_enabled=True,
        )
    ]


class FirecrackerSandboxSpecServiceInjector(SandboxSpecServiceInjector):
    """Injector for Firecracker sandbox spec service."""

    specs: list[SandboxSpecInfo] = Field(
        default_factory=get_default_firecracker_sandbox_specs,
        description='The preset sandbox specs to use',
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxSpecService, None]:
        yield PresetSandboxSpecService(specs=self.specs)


# Module-level singleton for FirecrackerSandboxService
# This preserves in-memory VM state across requests
_firecracker_service_instance: FirecrackerSandboxService | None = None


class FirecrackerSandboxServiceInjector(SandboxServiceInjector):
    """Injector for Firecracker sandbox service.

    Uses a module-level singleton to preserve in-memory VM state across requests.
    """

    firecracker_bin: str = Field(
        default=DEFAULT_FIRECRACKER_BIN,
        description='Path to the firecracker binary',
    )
    kernel_path: str = Field(
        default=DEFAULT_KERNEL_PATH,
        description='Path to the guest kernel image',
    )
    base_rootfs_path: str = Field(
        default=DEFAULT_ROOTFS_PATH,
        description='Path to the base rootfs image',
    )
    sockets_dir: str = Field(
        default=DEFAULT_SOCKETS_DIR,
        description='Directory to store Unix sockets',
    )
    vcpu_count: int = Field(
        default=2,
        description='Number of vCPUs per VM',
    )
    mem_size_mib: int = Field(
        default=1024,
        description='Memory size in MiB per VM',
    )
    debug_console: bool = Field(
        default=False,
        description='Run VMs in screen sessions for interactive console access',
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        global _firecracker_service_instance
        from openhands.app_server.config import (
            config_from_env,
            get_sandbox_spec_service,
        )

        # Use singleton to preserve in-memory VM registry
        if _firecracker_service_instance is None:
            # Get web_url from global config for proxy URL generation
            config = config_from_env()
            web_url = config.web_url

            async with get_sandbox_spec_service(state) as sandbox_spec_service:
                _firecracker_service_instance = FirecrackerSandboxService(
                    firecracker_bin=self.firecracker_bin,
                    kernel_path=self.kernel_path,
                    base_rootfs_path=self.base_rootfs_path,
                    sockets_dir=self.sockets_dir,
                    vcpu_count=self.vcpu_count,
                    mem_size_mib=self.mem_size_mib,
                    sandbox_spec_service=sandbox_spec_service,
                    web_url=web_url,
                    debug_console=self.debug_console,
                )
        yield _firecracker_service_instance
