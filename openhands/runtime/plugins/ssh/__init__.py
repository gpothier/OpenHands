import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Optional

from openhands.core.logger import openhands_logger as logger
from openhands.events.action import Action
from openhands.events.observation import Observation
from openhands.runtime.plugins.requirement import Plugin, PluginRequirement
from openhands.runtime.utils.system import check_port_available

SSH_PORT = 2222


@dataclass
class SSHRequirement(PluginRequirement):
    name: str = 'ssh'


class SSHPlugin(Plugin):
    """Plugin to start SSH server for local VSCode Remote-SSH access."""

    name: str = 'ssh'
    ssh_port: Optional[int] = None
    sshd_process: Optional[asyncio.subprocess.Process] = None

    async def initialize(self, username: str, runtime_id: str | None = None) -> None:
        # Check if we're on Windows - SSH plugin is not supported on Windows
        if os.name == 'nt' or sys.platform == 'win32':
            self.ssh_port = None
            logger.warning(
                'SSH plugin is not supported on Windows. Plugin will be disabled.'
            )
            return

        try:
            self.ssh_port = int(os.environ.get('SSH_PORT', SSH_PORT))
        except (KeyError, ValueError):
            self.ssh_port = SSH_PORT
            logger.debug(
                f'SSH_PORT environment variable not set, using default port {SSH_PORT}'
            )

        if not check_port_available(self.ssh_port):
            logger.warning(
                f'Port {self.ssh_port} is not available. SSH plugin will be disabled.'
            )
            self.ssh_port = None
            return

        # Start sshd in the foreground (will be managed by this process)
        cmd = f'/usr/sbin/sshd -D -p {self.ssh_port}'

        logger.debug(f'Starting SSH server on port {self.ssh_port}')

        self.sshd_process = await asyncio.create_subprocess_shell(
            cmd,
            stderr=asyncio.subprocess.STDOUT,
            stdout=asyncio.subprocess.PIPE,
        )

        # Give sshd a moment to start
        await asyncio.sleep(0.5)

        # Check if sshd is running
        if self.sshd_process.returncode is not None:
            logger.warning(
                f'SSH server failed to start (exit code: {self.sshd_process.returncode})'
            )
            self.ssh_port = None
            return

        logger.info(f'SSH server started on port {self.ssh_port}')
        logger.info(
            f'Connect using: ssh -p {self.ssh_port} openhands@<host> (password: openhands)'
        )

    async def run(self, action: Action) -> Observation:
        """Run the plugin for a given action."""
        raise NotImplementedError('SSHPlugin does not support run method')
