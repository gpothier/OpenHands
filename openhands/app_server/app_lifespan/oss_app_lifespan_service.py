from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config

from openhands.app_server.app_lifespan.app_lifespan_service import AppLifespanService

if TYPE_CHECKING:
    from openhands.app_server.sandbox.sandbox_service_registry import SandboxRegistry

_logger = logging.getLogger(__name__)


class OssAppLifespanService(AppLifespanService):
    run_alembic_on_startup: bool = True
    _registry_context: object | None = None
    _registry: 'SandboxRegistry | None' = None

    async def __aenter__(self):
        if self.run_alembic_on_startup:
            self.run_alembic()

        # Always initialize sandbox registry - it provides all available sandbox types
        # (Docker, Firecracker when available, etc.)
        # Only skip if explicitly in single-sandbox modes (remote, process)
        sandbox_type = os.environ.get('SANDBOX_TYPE', '').lower()
        if sandbox_type not in ('remote', 'process', 'local'):
            await self._init_sandbox_registry()

        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        # Clean up sandbox registry
        if self._registry_context is not None:
            from openhands.app_server.config import set_sandbox_registry

            await self._registry_context.__aexit__(exc_type, exc_value, traceback)
            self._registry_context = None
            self._registry = None
            set_sandbox_registry(None)

    async def _init_sandbox_registry(self):
        """Initialize the sandbox registry and store it globally."""
        from openhands.app_server.config import set_sandbox_registry
        from openhands.app_server.sandbox.sandbox_service_registry import (
            create_sandbox_registry,
        )

        _logger.info('Initializing sandbox registry...')
        self._registry_context = create_sandbox_registry()
        self._registry = await self._registry_context.__aenter__()

        # Store in module-level variable (not in Pydantic model)
        set_sandbox_registry(self._registry)

        _logger.info(
            f'Sandbox registry initialized with types: {self._registry.available_types}'
        )

    def run_alembic(self):
        # Run alembic upgrade head to ensure database is up to date
        alembic_dir = Path(__file__).parent / 'alembic'
        alembic_ini = alembic_dir / 'alembic.ini'

        # Create alembic config with absolute paths
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option('script_location', str(alembic_dir))

        # Change to alembic directory for the command execution
        original_cwd = os.getcwd()
        try:
            os.chdir(str(alembic_dir.parent))
            command.upgrade(alembic_cfg, 'head')
        finally:
            os.chdir(original_cwd)
