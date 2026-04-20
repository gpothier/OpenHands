"""Unified sandbox interface combining specs and operations.

This module provides the base `Sandbox` class that each sandbox implementation
(Docker, Firecracker, Process, Remote) should implement. It unifies what was
previously split between SandboxService and SandboxSpecService.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from openhands.app_server.sandbox.sandbox_models import (
    SandboxInfo,
    SandboxPage,
    SandboxStartParams,
)
from openhands.app_server.sandbox.sandbox_spec_models import (
    SandboxSpecInfo,
    SandboxSpecInfoPage,
    SandboxType,
)


class Sandbox(ABC):
    """Base class for sandbox implementations.

    Each sandbox type (Docker, Firecracker, etc.) implements this interface,
    providing both the available specs and the operations to manage sandboxes.
    """

    @property
    @abstractmethod
    def sandbox_type(self) -> SandboxType:
        """Return the type of sandbox this implements."""

    # -------------------------------------------------------------------------
    # Spec methods (what sandbox configurations are available)
    # -------------------------------------------------------------------------

    @abstractmethod
    async def search_specs(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxSpecInfoPage:
        """Search for available sandbox specs."""

    @abstractmethod
    async def get_spec(self, spec_id: str) -> SandboxSpecInfo | None:
        """Get a single sandbox spec by ID, or None if not found."""

    async def get_default_spec(self) -> SandboxSpecInfo | None:
        """Get the default sandbox spec for this type."""
        page = await self.search_specs(limit=1)
        return page.items[0] if page.items else None

    # -------------------------------------------------------------------------
    # Sandbox lifecycle methods
    # -------------------------------------------------------------------------

    @abstractmethod
    async def search_sandboxes(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxPage:
        """Search for running sandboxes of this type."""

    @abstractmethod
    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get a sandbox by ID, or None if not found."""

    @abstractmethod
    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get a sandbox by session API key, or None if not found."""

    @abstractmethod
    async def start_sandbox(self, params: SandboxStartParams | None = None) -> SandboxInfo:
        """Start a new sandbox."""

    @abstractmethod
    async def resume_sandbox(self, sandbox_id: str) -> bool:
        """Resume a paused sandbox. Returns True if successful."""

    @abstractmethod
    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Pause a running sandbox. Returns True if successful."""

    @abstractmethod
    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete a sandbox. Returns True if successful."""

    # -------------------------------------------------------------------------
    # Optional methods with default implementations
    # -------------------------------------------------------------------------

    async def get_vscode_internal_url(self, short_sandbox_id: str) -> str | None:
        """Get the internal VS Code URL for a sandbox."""
        return None

    async def get_agent_server_internal_url(self, short_sandbox_id: str) -> str | None:
        """Get the internal agent-server URL for a sandbox."""
        return None


@dataclass
class SandboxAdapter(Sandbox):
    """Adapter that wraps legacy SandboxService + SandboxSpecService into Sandbox interface.

    This allows gradual migration from the old separate service/spec architecture
    to the new unified Sandbox interface.
    """

    _sandbox_type: SandboxType
    service: 'SandboxService'  # Forward reference to avoid circular import
    spec_service: 'SandboxSpecService'  # Forward reference

    @property
    def sandbox_type(self) -> SandboxType:
        return self._sandbox_type

    async def search_specs(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxSpecInfoPage:
        return await self.spec_service.search_sandbox_specs(page_id, limit)

    async def get_spec(self, spec_id: str) -> SandboxSpecInfo | None:
        return await self.spec_service.get_sandbox_spec(spec_id)

    async def get_default_spec(self) -> SandboxSpecInfo | None:
        return await self.spec_service.get_default_sandbox_spec()

    async def search_sandboxes(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxPage:
        return await self.service.search_sandboxes(page_id, limit)

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        return await self.service.get_sandbox(sandbox_id)

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        return await self.service.get_sandbox_by_session_api_key(session_api_key)

    async def start_sandbox(self, params: SandboxStartParams | None = None) -> SandboxInfo:
        return await self.service.start_sandbox(params)

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        return await self.service.resume_sandbox(sandbox_id)

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        return await self.service.pause_sandbox(sandbox_id)

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        return await self.service.delete_sandbox(sandbox_id)

    async def get_vscode_internal_url(self, short_sandbox_id: str) -> str | None:
        return await self.service.get_vscode_internal_url(short_sandbox_id)

    async def get_agent_server_internal_url(self, short_sandbox_id: str) -> str | None:
        return await self.service.get_agent_server_internal_url(short_sandbox_id)


# Type hints for forward references (actual imports would be circular)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openhands.app_server.sandbox.sandbox_service import SandboxService
    from openhands.app_server.sandbox.sandbox_spec_service import SandboxSpecService
