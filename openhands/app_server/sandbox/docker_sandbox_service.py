import asyncio
import logging
import os
import shutil
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncGenerator

import base62
import docker
import httpx
from docker.errors import APIError, NotFound
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.docker_sandbox_spec_service import get_docker_client
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    VSCODE,
    WORKER_1,
    WORKER_2,
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
from openhands.app_server.sandbox.sandbox_spec_service import SandboxSpecService
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)

_logger = logging.getLogger(__name__)
STARTUP_GRACE_SECONDS = 15

# Label for rootless Docker per sandbox feature
# The host daemon (oh-rootless-docker-manager) watches for containers with this label
# and sets up a rootless Docker daemon for each sandbox
_OH_ROOTLESS_DOCKER_LABEL = 'openhands.rootless-docker'

# Subdirectory names within the per-sandbox base directory
_DOCKERD_SOCKET_SUBDIR = 'dockerd-socket'
_DOCKERD_USER_HOME_SUBDIR = 'dockerd-user-home'
_WORKSPACE_SUBDIR = 'workspace'

# Label to track the workspace directory for cleanup
_OH_WORKSPACE_DIR_LABEL = 'openhands.workspace-dir'


def _get_use_host_network_default() -> bool:
    """Get the default value for use_host_network from environment variables.

    This function is called at runtime (not at class definition time) to ensure
    that environment variable changes are picked up correctly.
    """
    value = os.getenv('AGENT_SERVER_USE_HOST_NETWORK', '')
    return value.lower() in ('true', '1', 'yes')


def _get_kvm_enabled_default() -> bool:
    """Get the default value for kvm_enabled from environment variables."""
    value = os.getenv('SANDBOX_KVM_ENABLED', '')
    return value.lower() in ('true', '1', 'yes')


def _get_rootless_docker_enabled_default() -> bool:
    """Get the default value for rootless_docker_enabled from environment variables.

    When enabled, each sandbox gets its own isolated rootless Docker daemon.
    This requires the oh-rootless-docker-manager daemon to be running on the host.
    """
    value = os.getenv('SANDBOX_ROOTLESS_DOCKER_ENABLED', '')
    return value.lower() in ('true', '1', 'yes')


def _get_sandbox_dir_base_default() -> str:
    """Get the default base directory for per-sandbox directories (container path).

    Each sandbox gets a directory at {base}/{container_name}/ containing:
    - dockerd-socket/: rootless Docker socket (if rootless_docker_enabled)
    - dockerd-user-home/: home directory for the sandbox user
    - workspace/: workspace data (if auto_workspace_dir enabled, in that branch)

    This is the path as seen from inside the OpenHands container.
    For bind mounts to sandbox containers, use SANDBOX_DIR_BASE_HOST.
    """
    return os.getenv('SANDBOX_DIR_BASE') or os.path.expanduser('~/.openhands/sandboxes')


def _get_sandbox_dir_base_host_default() -> str:
    """Get the host path for per-sandbox directories (for bind mounts).

    When creating sandbox containers, bind mount paths must be HOST paths,
    not paths inside the OpenHands container. This setting specifies
    the host path that corresponds to SANDBOX_DIR_BASE.

    Default matches the oh-rootless-docker-manager daemon's default.
    """
    return (
        os.getenv('SANDBOX_DIR_BASE_HOST') or '/var/lib/cali-openhands/state/sandboxes'
    )


def _get_auto_workspace_dir_default() -> bool:
    """Get the default value for auto_workspace_dir from environment variables.

    When enabled, each sandbox gets a unique workspace directory on the host
    that is mounted at /workspace in the sandbox container. This persists
    workspace data across sandbox restarts and enables rootless Docker volume
    mounts to work correctly.
    """
    value = os.getenv('SANDBOX_AUTO_WORKSPACE_DIR', '')
    return value.lower() in ('true', '1', 'yes')


class VolumeMount(BaseModel):
    """Mounted volume within the container."""

    host_path: str
    container_path: str
    mode: str = 'rw'

    model_config = ConfigDict(frozen=True)


class ExposedPort(BaseModel):
    """Exposed port within container to be matched to a free port on the host."""

    name: str
    description: str
    container_port: int = 8000

    model_config = ConfigDict(frozen=True)


@dataclass
class DockerSandboxService(SandboxService):
    """Sandbox service built on docker.

    The Docker API does not currently support async operations, so some of these operations will block.
    Given that the docker API is intended for local use on a single machine, this is probably acceptable.
    """

    sandbox_spec_service: SandboxSpecService
    container_name_prefix: str
    host_port: int
    container_url_pattern: str
    mounts: list[VolumeMount]
    exposed_ports: list[ExposedPort]
    health_check_path: str | None
    httpx_client: httpx.AsyncClient
    max_num_sandboxes: int
    web_url: str | None = None
    permitted_cors_origins: list[str] = field(default_factory=list)
    extra_hosts: dict[str, str] = field(default_factory=dict)
    docker_client: docker.DockerClient = field(default_factory=get_docker_client)
    startup_grace_seconds: int = STARTUP_GRACE_SECONDS
    use_host_network: bool = False
    kvm_enabled: bool = False
    rootless_docker_enabled: bool = False
    sandbox_dir_base: str | None = None
    sandbox_dir_base_host: str | None = None
    auto_workspace_dir: bool = False

    def _find_unused_port(self) -> int:
        """Find an unused port on the host machine."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port

    def _docker_status_to_sandbox_status(self, docker_status: str) -> SandboxStatus:
        """Convert Docker container status to SandboxStatus."""
        status_mapping = {
            'running': SandboxStatus.RUNNING,
            'paused': SandboxStatus.PAUSED,
            # The stop button was pressed in the docker console
            'exited': SandboxStatus.PAUSED,
            'created': SandboxStatus.STARTING,
            'restarting': SandboxStatus.STARTING,
            'removing': SandboxStatus.MISSING,
            'dead': SandboxStatus.ERROR,
        }
        return status_mapping.get(docker_status.lower(), SandboxStatus.ERROR)

    def _get_container_env_vars(self, container) -> dict[str, str | None]:
        env_vars_list = container.attrs['Config']['Env']
        result = {}
        for env_var in env_vars_list:
            if '=' in env_var:
                key, value = env_var.split('=', 1)
                result[key] = value
            else:
                # Handle cases where an environment variable might not have a value
                result[env_var] = None
        return result

    async def _container_to_sandbox_info(self, container) -> SandboxInfo | None:
        """Convert Docker container to SandboxInfo."""
        # Convert Docker status to runtime status
        status = self._docker_status_to_sandbox_status(container.status)

        # Parse creation time
        created_str = container.attrs.get('Created', '')
        try:
            created_at = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            created_at = utc_now()

        # Get URL and session key for running containers
        exposed_urls = None
        session_api_key = None

        if status == SandboxStatus.RUNNING:
            # Get session API key first
            env = self._get_container_env_vars(container)
            session_api_key = env.get(SESSION_API_KEY_VARIABLE)

            # Get the exposed port mappings
            exposed_urls = []

            # Check if container is using host network mode
            network_mode = container.attrs.get('HostConfig', {}).get('NetworkMode', '')
            is_host_network = network_mode == 'host'

            if is_host_network:
                # Host network mode: container ports are directly accessible on host
                for exposed_port in self.exposed_ports:
                    host_port = exposed_port.container_port
                    url = self.container_url_pattern.format(port=host_port)

                    # VSCode URLs require the api_key and working dir
                    if exposed_port.name == VSCODE:
                        url += f'/?tkn={session_api_key}&folder={container.attrs["Config"]["WorkingDir"]}'

                    exposed_urls.append(
                        ExposedUrl(
                            name=exposed_port.name,
                            url=url,
                            port=exposed_port.container_port,
                        )
                    )
            else:
                # Bridge network mode: use port bindings
                port_bindings = container.attrs.get('NetworkSettings', {}).get(
                    'Ports', {}
                )
                if port_bindings:
                    for container_port, host_bindings in port_bindings.items():
                        if host_bindings:
                            host_port = int(host_bindings[0]['HostPort'])
                            matching_port = next(
                                (
                                    ep
                                    for ep in self.exposed_ports
                                    if container_port == f'{ep.container_port}/tcp'
                                ),
                                None,
                            )
                            if matching_port:
                                url = self.container_url_pattern.format(port=host_port)

                                # VSCode URLs require the api_key and working dir
                                if matching_port.name == VSCODE:
                                    url += f'/?tkn={session_api_key}&folder={container.attrs["Config"]["WorkingDir"]}'

                                exposed_urls.append(
                                    ExposedUrl(
                                        name=matching_port.name,
                                        url=url,
                                        port=matching_port.container_port,
                                    )
                                )

        if not container.image.tags:
            _logger.debug(
                f'Skipping container {container.name!r}: image has no tags (image id: {container.image.id})'
            )
            return None

        return SandboxInfo(
            id=container.name,
            created_by_user_id=None,
            sandbox_spec_id=container.image.tags[0],
            status=status,
            session_api_key=session_api_key,
            exposed_urls=exposed_urls,
            created_at=created_at,
        )

    async def _container_to_checked_sandbox_info(self, container) -> SandboxInfo | None:
        sandbox_info = await self._container_to_sandbox_info(container)
        if (
            sandbox_info
            and self.health_check_path is not None
            and sandbox_info.exposed_urls
        ):
            app_server_url = next(
                exposed_url.url
                for exposed_url in sandbox_info.exposed_urls
                if exposed_url.name == AGENT_SERVER
            )
            try:
                # When running in Docker, replace localhost hostname with host.docker.internal for internal requests
                app_server_url = replace_localhost_hostname_for_docker(app_server_url)

                response = await self.httpx_client.get(
                    f'{app_server_url}{self.health_check_path}'
                )
                response.raise_for_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # If the server has exceeded the startup grace period, it's an error
                if sandbox_info.created_at < utc_now() - timedelta(
                    seconds=self.startup_grace_seconds
                ):
                    _logger.info(
                        f'Sandbox server not running: {app_server_url} : {exc}'
                    )
                    sandbox_info.status = SandboxStatus.ERROR
                else:
                    _logger.debug(
                        f'Sandbox server not yet available (still starting): '
                        f'{app_server_url} : {exc}'
                    )
                    sandbox_info.status = SandboxStatus.STARTING
                sandbox_info.exposed_urls = None
                sandbox_info.session_api_key = None
        return sandbox_info

    async def search_sandboxes(
        self,
        page_id: str | None = None,
        limit: int = 100,
    ) -> SandboxPage:
        """Search for sandboxes."""
        try:
            # Get all containers with our prefix
            all_containers = self.docker_client.containers.list(all=True)
            sandboxes = []

            for container in all_containers:
                if container.name and container.name.startswith(
                    self.container_name_prefix
                ):
                    sandbox_info = await self._container_to_checked_sandbox_info(
                        container
                    )
                    if sandbox_info:
                        sandboxes.append(sandbox_info)

            # Sort by creation time (newest first)
            sandboxes.sort(key=lambda x: x.created_at, reverse=True)

            # Apply pagination
            start_idx = 0
            if page_id:
                try:
                    start_idx = int(page_id)
                except ValueError:
                    start_idx = 0

            end_idx = start_idx + limit
            paginated_containers = sandboxes[start_idx:end_idx]

            # Determine next page ID
            next_page_id = None
            if end_idx < len(sandboxes):
                next_page_id = str(end_idx)

            return SandboxPage(items=paginated_containers, next_page_id=next_page_id)

        except APIError:
            return SandboxPage(items=[], next_page_id=None)

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get a single sandbox info."""
        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return None
            container = self.docker_client.containers.get(sandbox_id)
            return await self._container_to_checked_sandbox_info(container)
        except (NotFound, APIError):
            return None

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get a single sandbox by session API key."""
        try:
            # Get all containers with our prefix
            all_containers = self.docker_client.containers.list(all=True)

            for container in all_containers:
                if container.name and container.name.startswith(
                    self.container_name_prefix
                ):
                    # Check if this container has the matching session API key
                    env_vars = self._get_container_env_vars(container)
                    container_session_key = env_vars.get(SESSION_API_KEY_VARIABLE)

                    if container_session_key == session_api_key:
                        return await self._container_to_checked_sandbox_info(container)

            return None
        except (NotFound, APIError):
            return None

    async def start_sandbox(
        self, sandbox_spec_id: str | None = None, sandbox_id: str | None = None
    ) -> SandboxInfo:
        """Start a new sandbox."""
        # Warn about port collision risk when using host network mode with multiple sandboxes
        if self.use_host_network and self.max_num_sandboxes > 1:
            _logger.warning(
                'Host network mode is enabled with max_num_sandboxes > 1. '
                'Multiple sandboxes will attempt to bind to the same ports, '
                'which may cause port collision errors. Consider setting '
                'max_num_sandboxes=1 when using host network mode.'
            )

        # Enforce sandbox limits by cleaning up old sandboxes
        await self.pause_old_sandboxes(self.max_num_sandboxes - 1)

        if sandbox_spec_id is None:
            sandbox_spec = await self.sandbox_spec_service.get_default_sandbox_spec()
        else:
            sandbox_spec_maybe = await self.sandbox_spec_service.get_sandbox_spec(
                sandbox_spec_id
            )
            if sandbox_spec_maybe is None:
                raise ValueError('Sandbox Spec not found')
            sandbox_spec = sandbox_spec_maybe

        # Generate a sandbox id if none was provided
        if sandbox_id is None:
            sandbox_id = base62.encodebytes(os.urandom(16))

        # Generate container name and session api key
        container_name = f'{self.container_name_prefix}{sandbox_id}'
        session_api_key = base62.encodebytes(os.urandom(32))

        # Prepare environment variables
        env_vars = sandbox_spec.initial_env.copy()
        env_vars[SESSION_API_KEY_VARIABLE] = session_api_key
        env_vars[WEBHOOK_CALLBACK_VARIABLE] = (
            f'http://host.docker.internal:{self.host_port}/api/v1/webhooks'
        )

        # Set CORS origins for remote browser access when web_url is configured.
        # This allows the agent-server container to accept requests from the
        # frontend when running OpenHands on a remote machine.
        # Each origin gets its own indexed env var (OH_ALLOW_CORS_ORIGINS_0, _1, etc.)
        cors_origins: list[str] = []
        if self.web_url:
            cors_origins.append(self.web_url)
        cors_origins.extend(self.permitted_cors_origins)
        # Deduplicate while preserving order
        seen: set[str] = set()
        for origin in cors_origins:
            if origin not in seen:
                seen.add(origin)
                idx = len(seen) - 1
                env_vars[f'OH_ALLOW_CORS_ORIGINS_{idx}'] = origin

        # Prepare port mappings and add port environment variables
        # When using host network, container ports are directly accessible on the host
        # so we use the container ports directly instead of mapping to random host ports
        port_mappings: dict[int, int] | None = None
        if self.use_host_network:
            # Host network mode: container ports are directly accessible
            for exposed_port in self.exposed_ports:
                env_vars[exposed_port.name] = str(exposed_port.container_port)
        else:
            # Bridge network mode: map container ports to random host ports
            port_mappings = {}
            for exposed_port in self.exposed_ports:
                host_port = self._find_unused_port()
                port_mappings[exposed_port.container_port] = host_port
                env_vars[exposed_port.name] = str(exposed_port.container_port)

        # Prepare labels
        labels = {
            'sandbox_spec_id': sandbox_spec.id,
        }

        # Prepare volumes
        volumes = {
            mount.host_path: {
                'bind': mount.container_path,
                'mode': mount.mode,
            }
            for mount in self.mounts
        }

        # Startup commands to run inside the container after it starts
        startup_commands: list[str] = []

        # Per-sandbox directory setup (shared by rootless_docker and auto_workspace_dir)
        # Directory structure:
        #   {sandbox_dir_base}/{container_name}/
        #   ├── workspace/         ← workspace data (if auto_workspace_dir enabled)
        #   ├── dockerd-socket/    ← rootless Docker socket (if rootless_docker_enabled)
        #   └── dockerd-user-home/ ← home directory for the sandbox user
        sandbox_base: str | None = None
        sandbox_base_host: str | None = None

        if self.rootless_docker_enabled or self.auto_workspace_dir:
            # Container path (for creating directories from within OpenHands container)
            base = self.sandbox_dir_base or os.path.expanduser('~/.openhands/sandboxes')
            sandbox_base = os.path.join(base, container_name)

            # Host path (for bind mounts - Docker interprets these as host paths)
            base_host = (
                self.sandbox_dir_base_host or '/var/lib/cali-openhands/state/sandboxes'
            )
            sandbox_base_host = os.path.join(base_host, container_name)

        # Auto workspace directory setup
        # Creates a unique workspace directory on the host per sandbox and mounts it.
        # This enables:
        # - Workspace data persistence across sandbox restarts
        # - Rootless Docker volume mounts to work (dockerd can access /workspace)
        if self.auto_workspace_dir and sandbox_base and sandbox_base_host:
            workspace_dir = os.path.join(sandbox_base, _WORKSPACE_SUBDIR)
            workspace_dir_host = os.path.join(sandbox_base_host, _WORKSPACE_SUBDIR)

            # UID 10001 is the standard container user (openhands) that needs write access
            container_uid = 10001

            # Create workspace directory owned by container user with mode 0755
            # This allows the container to write files, and the oh-rootless-docker-manager
            # will grant the sandbox user ACL access for Docker volume mounts
            os.makedirs(workspace_dir, exist_ok=True)
            os.chown(workspace_dir, container_uid, container_uid)
            os.chmod(workspace_dir, 0o755)

            # Pre-create the working_dir subdirectory (e.g., /workspace/project)
            # This prevents Docker from creating it as root, which would make it
            # unwritable by the container user (UID 10001)
            working_dir_basename = os.path.basename(sandbox_spec.working_dir)
            project_dir = os.path.join(workspace_dir, working_dir_basename)
            os.makedirs(project_dir, exist_ok=True)
            os.chown(project_dir, container_uid, container_uid)
            os.chmod(project_dir, 0o755)

            # Mount workspace at the parent of working_dir (e.g., /workspace)
            # This allows sibling directories like /workspace/conversations and
            # /workspace/bash_events to also be persisted
            workspace_mount_point = os.path.dirname(sandbox_spec.working_dir)
            volumes[workspace_dir_host] = {
                'bind': workspace_mount_point,
                'mode': 'rw',
            }

            # Add label for cleanup tracking
            labels[_OH_WORKSPACE_DIR_LABEL] = workspace_dir

            _logger.info(
                f'Auto workspace directory for sandbox {container_name}: '
                f'{workspace_dir_host} -> {workspace_mount_point}'
            )

        # Rootless Docker per sandbox setup
        # Creates isolated Docker daemon for each sandbox via oh-rootless-docker-manager
        if self.rootless_docker_enabled and sandbox_base and sandbox_base_host:
            # Create subdirectories for rootless docker (using container path)
            socket_dir = os.path.join(sandbox_base, _DOCKERD_SOCKET_SUBDIR)
            user_home_dir = os.path.join(sandbox_base, _DOCKERD_USER_HOME_SUBDIR)

            os.makedirs(socket_dir, exist_ok=True)
            os.chmod(socket_dir, 0o755)
            os.makedirs(user_home_dir, exist_ok=True)
            os.chmod(user_home_dir, 0o755)

            # Mount the socket directory into the sandbox (using host path)
            socket_dir_host = os.path.join(sandbox_base_host, _DOCKERD_SOCKET_SUBDIR)
            volumes[socket_dir_host] = {
                'bind': '/var/run/docker',
                'mode': 'rw',
            }

            # Add label for the oh-rootless-docker-manager daemon on the host
            # The daemon derives the sandbox directory from its configured base + container name
            labels[_OH_ROOTLESS_DOCKER_LABEL] = 'true'

            # Create symlink so tools that don't honor DOCKER_HOST still work
            startup_commands.append(
                'ln -sf /var/run/docker/docker.sock /var/run/docker.sock'
            )

            _logger.info(
                f'Rootless Docker enabled for sandbox {container_name}: '
                f'container_path={sandbox_base}, host_path={sandbox_base_host}'
            )

        # Determine network mode
        network_mode = 'host' if self.use_host_network else None

        if self.use_host_network:
            _logger.info(f'Starting sandbox {container_name} with host network mode')

        # Determine devices to pass through (e.g., /dev/kvm for hardware virtualization)
        devices = ['/dev/kvm:/dev/kvm:rwm'] if self.kvm_enabled else None

        if self.kvm_enabled:
            _logger.info(
                f'Starting sandbox {container_name} with KVM device passthrough'
            )

        try:
            # Create and start the container
            container = self.docker_client.containers.run(  # type: ignore[call-overload,misc]
                image=sandbox_spec.id,
                command=sandbox_spec.command,  # Use default command from image
                remove=False,
                name=container_name,
                environment=env_vars,
                ports=port_mappings,
                volumes=volumes,
                working_dir=sandbox_spec.working_dir,
                labels=labels,
                detach=True,
                # Use Docker's tini init process to ensure proper signal handling and reaping of
                # zombie child processes.
                init=True,
                # Allow agent-server containers to resolve host.docker.internal
                # and other custom hostnames for LAN deployments
                # Note: extra_hosts is not needed with host network mode
                extra_hosts=self.extra_hosts
                if self.extra_hosts and not self.use_host_network
                else None,
                # Network mode: 'host' for host networking, None for default bridge
                network_mode=network_mode,
                # Device passthrough for KVM hardware virtualization
                devices=devices,
            )

            # Run startup commands inside the container (as root for system-level setup)
            for cmd in startup_commands:
                result = container.exec_run(cmd, user='root')
                if result.exit_code != 0:
                    _logger.warning(
                        f'Startup command failed in {container_name}: {cmd!r} '
                        f'(exit code {result.exit_code}): {result.output.decode()}'
                    )

            sandbox_info = await self._container_to_sandbox_info(container)
            assert sandbox_info is not None
            return sandbox_info

        except APIError as e:
            raise SandboxError(f'Failed to start container: {e}')

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        """Resume a paused sandbox."""
        # Enforce sandbox limits by cleaning up old sandboxes
        await self.pause_old_sandboxes(self.max_num_sandboxes - 1)

        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return False
            container = self.docker_client.containers.get(sandbox_id)

            if container.status == 'paused':
                container.unpause()
            elif container.status == 'exited':
                container.start()

            return True
        except (NotFound, APIError):
            return False

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Pause a running sandbox."""
        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return False
            container = self.docker_client.containers.get(sandbox_id)

            if container.status == 'running':
                container.pause()

            return True
        except (NotFound, APIError):
            return False

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete a sandbox."""
        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return False
            container = self.docker_client.containers.get(sandbox_id)

            # Derive sandbox base dir from configured base + container name
            sandbox_base: str | None = None
            if self.rootless_docker_enabled or self.auto_workspace_dir:
                base = self.sandbox_dir_base or os.path.expanduser(
                    '~/.openhands/sandboxes'
                )
                sandbox_base = os.path.join(base, sandbox_id)

            # Get workspace dir from label for in-container cleanup
            workspace_label = container.labels.get(_OH_WORKSPACE_DIR_LABEL)

            # If workspace exists and container is running, clean up contents from inside
            # the container as root. Files created by the container user (UID 10001)
            # may not be deletable from the host due to permission issues.
            if workspace_label and container.status == 'running':
                # Find the mount destination for workspace inside the container
                mount_dest = next(
                    (
                        m.get('Destination')
                        for m in container.attrs.get('Mounts', [])
                        if m.get('Source') == workspace_label
                    ),
                    None,
                )
                if mount_dest:
                    try:
                        exit_code, output = container.exec_run(
                            ['find', mount_dest, '-mindepth', '1', '-depth', '-delete'],
                            user='root',
                        )
                        if exit_code != 0:
                            output_str = (
                                output.decode()
                                if isinstance(output, bytes)
                                else str(output)
                            )
                            _logger.warning(
                                f'In-container workspace cleanup exited {exit_code} '
                                f'for {sandbox_id}: {output_str}'
                            )
                    except Exception as exc:
                        _logger.debug(
                            f'In-container workspace cleanup failed for {sandbox_id}: {exc}'
                        )

            # Stop the container if it's running
            if container.status in ['running', 'paused']:
                container.stop(timeout=10)

            # Remove the container
            container.remove()

            # Remove associated volume
            try:
                volume_name = f'openhands-workspace-{sandbox_id}'
                volume = self.docker_client.volumes.get(volume_name)
                volume.remove()
            except (NotFound, APIError):
                # Volume might not exist or already removed
                pass

            # Remove the entire sandbox directory tree
            # This includes workspace/, dockerd-socket/, and dockerd-user-home/
            if sandbox_base and os.path.exists(sandbox_base):
                shutil.rmtree(sandbox_base, ignore_errors=True)
                _logger.info(
                    f'Deleted sandbox directory for {sandbox_id}: {sandbox_base}'
                )

            return True
        except (NotFound, APIError):
            return False


class DockerSandboxServiceInjector(SandboxServiceInjector):
    """Dependency injector for docker sandbox services."""

    container_url_pattern: str = Field(
        default='http://localhost:{port}',
        description=(
            'URL pattern for exposed sandbox ports. Use {port} as placeholder. '
            'For remote access, set to your server IP (e.g., http://192.168.1.100:{port}). '
            'Configure via OH_SANDBOX_CONTAINER_URL_PATTERN environment variable.'
        ),
    )
    host_port: int = Field(
        default=3000,
        description=(
            'The port on which the main OpenHands app server is running. '
            'Used for webhook callbacks from agent-server containers. '
            'If running OpenHands on a non-default port, set this to match. '
            'Configure via OH_SANDBOX_HOST_PORT environment variable.'
        ),
    )
    container_name_prefix: str = 'oh-agent-server-'
    max_num_sandboxes: int = Field(
        default=5,
        description='Maximum number of sandboxes allowed to run simultaneously',
    )
    mounts: list[VolumeMount] = Field(default_factory=list)
    exposed_ports: list[ExposedPort] = Field(
        default_factory=lambda: [
            ExposedPort(
                name=AGENT_SERVER,
                description=(
                    'The port on which the agent server runs within the container'
                ),
                container_port=8000,
            ),
            ExposedPort(
                name=VSCODE,
                description=(
                    'The port on which the VSCode server runs within the container'
                ),
                container_port=8001,
            ),
            ExposedPort(
                name=WORKER_1,
                description=(
                    'The first port on which the agent should start application servers.'
                ),
                container_port=8011,
            ),
            ExposedPort(
                name=WORKER_2,
                description=(
                    'The second port on which the agent should start application servers.'
                ),
                container_port=8012,
            ),
        ]
    )
    health_check_path: str | None = Field(
        default='/health',
        description=(
            'The url path in the sandbox agent server to check to '
            'determine whether the server is running'
        ),
    )
    extra_hosts: dict[str, str] = Field(
        default_factory=lambda: {'host.docker.internal': 'host-gateway'},
        description=(
            'Extra hostname mappings to add to agent-server containers. '
            'This allows containers to resolve hostnames like host.docker.internal '
            'for LAN deployments and MCP connections. '
            'Format: {"hostname": "ip_or_gateway"}'
        ),
    )
    startup_grace_seconds: int = Field(
        default=STARTUP_GRACE_SECONDS,
        description=(
            'Number of seconds were no response from the agent server is acceptable'
            'before it is considered an error'
        ),
    )
    use_host_network: bool = Field(
        default_factory=_get_use_host_network_default,
        description=(
            'Whether to use host networking mode for agent-server containers. '
            'When enabled, containers share the host network namespace, '
            'making all container ports directly accessible on the host. '
            'This is useful for reverse proxy setups where dynamic port mapping '
            'is problematic. Configure via AGENT_SERVER_USE_HOST_NETWORK environment variable.'
        ),
    )
    kvm_enabled: bool = Field(
        default_factory=_get_kvm_enabled_default,
        description=(
            'Whether to pass through /dev/kvm to sandbox containers for hardware '
            'virtualization support. When enabled, sandboxes can run KVM-accelerated '
            'virtual machines instead of using slower emulation. Requires the host '
            'to have KVM available (/dev/kvm must exist and be accessible). '
            'Configure via SANDBOX_KVM_ENABLED environment variable.'
        ),
    )
    rootless_docker_enabled: bool = Field(
        default_factory=_get_rootless_docker_enabled_default,
        description=(
            'Whether to enable rootless Docker per sandbox. When enabled, each sandbox '
            'gets its own isolated Docker daemon running as a dedicated unprivileged user. '
            'This requires the oh-rootless-docker-manager daemon to be running on the host. '
            'More secure than Docker socket passthrough and more stable than sysbox. '
            'Configure via SANDBOX_ROOTLESS_DOCKER_ENABLED environment variable.'
        ),
    )
    sandbox_dir_base: str = Field(
        default_factory=_get_sandbox_dir_base_default,
        description=(
            'Base directory for per-sandbox directories (container path). Each sandbox '
            'gets a directory at {base}/{container_name}/ containing subdirectories for '
            'Docker socket, user home, and optionally workspace data. This is the path '
            'as seen from inside the OpenHands container. '
            'Configure via SANDBOX_DIR_BASE environment variable.'
        ),
    )
    sandbox_dir_base_host: str = Field(
        default_factory=_get_sandbox_dir_base_host_default,
        description=(
            'Base directory for per-sandbox directories (host path). When creating '
            'sandbox containers, bind mount paths must be HOST paths. This setting '
            'specifies the host path that corresponds to SANDBOX_DIR_BASE. '
            'Configure via SANDBOX_DIR_BASE_HOST environment variable.'
        ),
    )
    auto_workspace_dir: bool = Field(
        default_factory=_get_auto_workspace_dir_default,
        description=(
            'Automatically create a unique workspace directory on the host per sandbox '
            'and mount it at /workspace in the container. This enables workspace data '
            'persistence across sandbox restarts and allows rootless Docker volume mounts '
            'to work correctly. Required when using rootless_docker_enabled with volume mounts. '
            'Configure via SANDBOX_AUTO_WORKSPACE_DIR environment variable.'
        ),
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        # Define inline to prevent circular lookup
        from openhands.app_server.config import (
            get_global_config,
            get_httpx_client,
            get_sandbox_spec_service,
        )

        # Get web_url and permitted_cors_origins from global config
        config = get_global_config()
        web_url = config.web_url

        async with (
            get_httpx_client(state) as httpx_client,
            get_sandbox_spec_service(state) as sandbox_spec_service,
        ):
            yield DockerSandboxService(
                sandbox_spec_service=sandbox_spec_service,
                container_name_prefix=self.container_name_prefix,
                host_port=self.host_port,
                container_url_pattern=self.container_url_pattern,
                mounts=self.mounts,
                exposed_ports=self.exposed_ports,
                health_check_path=self.health_check_path,
                httpx_client=httpx_client,
                max_num_sandboxes=self.max_num_sandboxes,
                web_url=web_url,
                permitted_cors_origins=config.permitted_cors_origins,
                extra_hosts=self.extra_hosts,
                startup_grace_seconds=self.startup_grace_seconds,
                use_host_network=self.use_host_network,
                kvm_enabled=self.kvm_enabled,
                rootless_docker_enabled=self.rootless_docker_enabled,
                sandbox_dir_base=self.sandbox_dir_base,
                sandbox_dir_base_host=self.sandbox_dir_base_host,
                auto_workspace_dir=self.auto_workspace_dir,
            )
