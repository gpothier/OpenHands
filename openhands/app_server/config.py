"""Configuration for the OpenHands App Server."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncContextManager, AsyncGenerator

import httpx
from fastapi import Depends, Request
from pydantic import Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

# Import the event_callback module to ensure all processors are registered
import openhands.app_server.event_callback  # noqa: F401
from openhands.agent_server.env_parser import from_env
from openhands.app_server.app_conversation.app_conversation_info_service import (
    AppConversationInfoService,
    AppConversationInfoServiceInjector,
)
from openhands.app_server.app_conversation.app_conversation_service import (
    AppConversationService,
    AppConversationServiceInjector,
)
from openhands.app_server.app_conversation.app_conversation_start_task_service import (
    AppConversationStartTaskService,
    AppConversationStartTaskServiceInjector,
)
from openhands.app_server.app_lifespan.app_lifespan_service import AppLifespanService
from openhands.app_server.app_lifespan.oss_app_lifespan_service import (
    OssAppLifespanService,
)
from openhands.app_server.event.event_service import EventService, EventServiceInjector
from openhands.app_server.event_callback.event_callback_service import (
    EventCallbackService,
    EventCallbackServiceInjector,
)
from openhands.app_server.pending_messages.pending_message_service import (
    PendingMessageService,
    PendingMessageServiceInjector,
)
from openhands.app_server.sandbox.sandbox import Sandbox
from openhands.app_server.sandbox.sandbox_service import (
    SandboxService,
    SandboxServiceInjector,
)
from openhands.app_server.sandbox.sandbox_service_registry import SandboxRegistry
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    SandboxSpecServiceInjector,
)
from openhands.app_server.services.db_session_injector import (
    DbSessionInjector,
)
from openhands.app_server.services.httpx_client_injector import HttpxClientInjector
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.services.jwt_service import JwtService, JwtServiceInjector
from openhands.app_server.user.user_context import UserContext, UserContextInjector
from openhands.app_server.web_client.default_web_client_config_injector import (
    DefaultWebClientConfigInjector,
)
from openhands.app_server.web_client.web_client_config_injector import (
    WebClientConfigInjector,
)
from openhands.sdk.utils.models import OpenHandsModel
from openhands.server.types import AppMode
from openhands.utils.environment import StorageProvider, get_storage_provider

# Module-level storage for the sandbox registry
# This is stored separately from AppServerConfig because SandboxRegistry
# contains Sandbox (an ABC) which Pydantic cannot serialize
_sandbox_registry: SandboxRegistry | None = None


def get_default_persistence_dir() -> Path:
    # Recheck env because this function is also used to generate other defaults
    persistence_dir = os.getenv('OH_PERSISTENCE_DIR')

    # Legacy V0 fallback variable
    if persistence_dir is None:
        persistence_dir = os.getenv('FILE_STORE_PATH')

    if persistence_dir:
        result = Path(persistence_dir)
    else:
        result = Path.home() / '.openhands'

    result.mkdir(parents=True, exist_ok=True)
    return result


def get_default_web_url() -> str | None:
    """Get legacy web host parameter.

    If present, we assume we are running under https.
    """
    web_host = os.getenv('WEB_HOST')
    if not web_host:
        return None
    return f'https://{web_host}'


def get_default_permitted_cors_origins() -> list[str]:
    """Get permitted CORS origins, falling back to legacy PERMITTED_CORS_ORIGINS env var.

    The preferred configuration is via OH_PERMITTED_CORS_ORIGINS_0, _1, etc.
    (handled by the pydantic from_env parser). This fallback supports the legacy
    comma-separated PERMITTED_CORS_ORIGINS environment variable.
    """
    legacy = os.getenv('PERMITTED_CORS_ORIGINS', '')
    if legacy:
        return [o.strip() for o in legacy.split(',') if o.strip()]
    return []


def get_openhands_provider_base_url() -> str | None:
    """Return the base URL for the OpenHands provider, if configured.

    Falls back to LLM_BASE_URL for backward compatibility.
    """
    return os.getenv('OPENHANDS_PROVIDER_BASE_URL') or os.getenv('LLM_BASE_URL') or None


def _get_default_lifespan():
    # Check legacy parameters for saas mode. If we are in SAAS mode do not apply
    # OpenHands alembic migrations
    if 'saas' in (os.getenv('OPENHANDS_CONFIG_CLS') or '').lower():
        return None
    return OssAppLifespanService()


class AppServerConfig(OpenHandsModel):
    persistence_dir: Path = Field(default_factory=get_default_persistence_dir)
    web_url: str | None = Field(
        default_factory=get_default_web_url,
        description='The URL where OpenHands is running (e.g., http://localhost:3000)',
    )
    permitted_cors_origins: list[str] = Field(
        default_factory=get_default_permitted_cors_origins,
        description=(
            'Additional permitted CORS origins for both the app server and agent '
            'server containers. Configure via OH_PERMITTED_CORS_ORIGINS_0, _1, etc. '
            'Falls back to legacy PERMITTED_CORS_ORIGINS env var.'
        ),
    )
    openhands_provider_base_url: str | None = Field(
        default_factory=get_openhands_provider_base_url,
        description='Base URL for the OpenHands provider',
    )
    # Dependency Injection Injectors
    event: EventServiceInjector | None = None
    event_callback: EventCallbackServiceInjector | None = None
    sandbox: SandboxServiceInjector | None = None
    sandbox_spec: SandboxSpecServiceInjector | None = None
    app_conversation_info: AppConversationInfoServiceInjector | None = None
    app_conversation_start_task: AppConversationStartTaskServiceInjector | None = None
    app_conversation: AppConversationServiceInjector | None = None
    pending_message: PendingMessageServiceInjector | None = None
    user: UserContextInjector | None = None
    jwt: JwtServiceInjector | None = None
    httpx: HttpxClientInjector = Field(default_factory=HttpxClientInjector)
    db_session: DbSessionInjector = Field(
        default_factory=lambda: DbSessionInjector(
            persistence_dir=get_default_persistence_dir()
        )
    )
    # Services
    lifespan: AppLifespanService | None = Field(default_factory=_get_default_lifespan)
    app_mode: AppMode = AppMode.OPENHANDS
    web_client: WebClientConfigInjector = Field(
        default_factory=DefaultWebClientConfigInjector
    )


def config_from_env() -> AppServerConfig:
    # Import defaults...
    from openhands.app_server.app_conversation.live_status_app_conversation_service import (  # noqa: E501
        LiveStatusAppConversationServiceInjector,
    )
    from openhands.app_server.app_conversation.sql_app_conversation_info_service import (  # noqa: E501
        SQLAppConversationInfoServiceInjector,
    )
    from openhands.app_server.app_conversation.sql_app_conversation_start_task_service import (  # noqa: E501
        SQLAppConversationStartTaskServiceInjector,
    )
    from openhands.app_server.event.aws_event_service import (
        AwsEventServiceInjector,
    )
    from openhands.app_server.event.filesystem_event_service import (
        FilesystemEventServiceInjector,
    )
    from openhands.app_server.event.google_cloud_event_service import (
        GoogleCloudEventServiceInjector,
    )
    from openhands.app_server.event_callback.sql_event_callback_service import (
        SQLEventCallbackServiceInjector,
    )
    from openhands.app_server.user.auth_user_context import (
        AuthUserContextInjector,
    )

    config: AppServerConfig = from_env(AppServerConfig, 'OH')  # type: ignore

    if config.event is None:
        provider = get_storage_provider()

        if provider == StorageProvider.AWS:
            # AWS S3 storage configuration
            bucket_name = os.environ.get('FILE_STORE_PATH')
            if not bucket_name:
                raise ValueError(
                    'FILE_STORE_PATH environment variable is required for S3 storage'
                )
            config.event = AwsEventServiceInjector(bucket_name=bucket_name)
        elif provider == StorageProvider.GCP:
            # Google Cloud storage configuration
            bucket_name = os.environ.get('FILE_STORE_PATH')
            if not bucket_name:
                raise ValueError(
                    'FILE_STORE_PATH environment variable is required for Google Cloud storage'
                )
            config.event = GoogleCloudEventServiceInjector(bucket_name=bucket_name)
        else:
            config.event = FilesystemEventServiceInjector()

    if config.event_callback is None:
        config.event_callback = SQLEventCallbackServiceInjector()

    # Note: config.sandbox and config.sandbox_spec are legacy injectors
    # The registry (initialized at startup) provides all sandbox types
    # These may remain None - the depends_* functions prefer the registry

    if config.app_conversation_info is None:
        config.app_conversation_info = SQLAppConversationInfoServiceInjector()

    if config.app_conversation_start_task is None:
        config.app_conversation_start_task = (
            SQLAppConversationStartTaskServiceInjector()
        )

    if config.app_conversation is None:
        tavily_api_key = None
        tavily_api_key_str = os.getenv('TAVILY_API_KEY') or os.getenv('SEARCH_API_KEY')
        if tavily_api_key_str:
            tavily_api_key = SecretStr(tavily_api_key_str)
        config.app_conversation = LiveStatusAppConversationServiceInjector(
            tavily_api_key=tavily_api_key
        )

    if config.pending_message is None:
        from openhands.app_server.pending_messages.pending_message_service import (
            SQLPendingMessageServiceInjector,
        )

        config.pending_message = SQLPendingMessageServiceInjector()

    if config.user is None:
        config.user = AuthUserContextInjector()

    if config.jwt is None:
        config.jwt = JwtServiceInjector(persistence_dir=config.persistence_dir)

    return config


_global_config: AppServerConfig | None = None


def get_global_config() -> AppServerConfig:
    """Get the default local server config shared across the server."""
    global _global_config
    if _global_config is None:
        # Load configuration from environment...
        _global_config = config_from_env()

    return _global_config  # type: ignore


def get_event_service(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[EventService]:
    injector = get_global_config().event
    assert injector is not None
    return injector.context(state, request)


def get_event_callback_service(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[EventCallbackService]:
    injector = get_global_config().event_callback
    assert injector is not None
    return injector.context(state, request)


@asynccontextmanager
async def get_sandbox_service(
    state: InjectorState, request: Request | None = None
) -> AsyncGenerator[SandboxRegistry, None]:
    """Get the sandbox registry for sandbox operations.

    This function is maintained for API compatibility - it returns the registry
    which provides access to all available sandbox types.
    """
    registry = get_sandbox_registry()
    if registry is None:
        raise RuntimeError('Sandbox registry not initialized')
    yield registry


@asynccontextmanager
async def get_sandbox_spec_service(
    state: InjectorState, request: Request | None = None
) -> AsyncGenerator[SandboxSpecService, None]:
    """Get the sandbox spec service for spec operations.

    This function is maintained for API compatibility - it returns an adapter
    that provides specs from all registered sandbox types.
    """
    registry = get_sandbox_registry()
    if registry is None:
        raise RuntimeError('Sandbox registry not initialized')

    class RegistrySpecAdapter:
        def __init__(self, registry: SandboxRegistry):
            self._registry = registry

        async def search_sandbox_specs(self, page_id=None, limit=100):
            return await self._registry.search_all_specs(page_id=page_id, limit=limit)

        async def get_sandbox_spec(self, spec_id: str):
            return await self._registry.get_spec(spec_id)

        async def batch_get_sandbox_specs(self, spec_ids: list[str]):
            return [await self.get_sandbox_spec(sid) for sid in spec_ids]

    yield RegistrySpecAdapter(registry)  # type: ignore


def get_sandbox_registry() -> SandboxRegistry | None:
    """Get the sandbox registry if configured.

    The registry provides unified access to all sandbox types (Docker, Firecracker, etc.)
    and their specs. Use this instead of separate sandbox_service and sandbox_spec_service
    when you need to work with multiple sandbox types.
    """
    return _sandbox_registry


def set_sandbox_registry(registry: SandboxRegistry | None) -> None:
    """Set the sandbox registry.

    Called by the lifespan service during app startup.
    """
    global _sandbox_registry
    _sandbox_registry = registry


def get_sandbox(sandbox_type: 'SandboxType') -> Sandbox | None:
    """Get a specific sandbox implementation by type.

    Args:
        sandbox_type: The type of sandbox to get (DOCKER, FIRECRACKER, etc.)

    Returns:
        The Sandbox implementation, or None if not available.
    """
    registry = get_sandbox_registry()
    if registry is None:
        return None
    return registry.get(sandbox_type)


# Import here to avoid circular import at module level
from openhands.app_server.sandbox.sandbox_spec_models import SandboxType


def get_app_conversation_info_service(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[AppConversationInfoService]:
    injector = get_global_config().app_conversation_info
    assert injector is not None
    return injector.context(state, request)


def get_app_conversation_start_task_service(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[AppConversationStartTaskService]:
    injector = get_global_config().app_conversation_start_task
    assert injector is not None
    return injector.context(state, request)


def get_app_conversation_service(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[AppConversationService]:
    injector = get_global_config().app_conversation
    assert injector is not None
    return injector.context(state, request)


def get_pending_message_service(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[PendingMessageService]:
    injector = get_global_config().pending_message
    assert injector is not None
    return injector.context(state, request)


def get_user_context(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[UserContext]:
    injector = get_global_config().user
    assert injector is not None
    return injector.context(state, request)


def get_httpx_client(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[httpx.AsyncClient]:
    return get_global_config().httpx.context(state, request)


def get_jwt_service(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[JwtService]:
    injector = get_global_config().jwt
    assert injector is not None
    return injector.context(state, request)


def get_db_session(
    state: InjectorState, request: Request | None = None
) -> AsyncContextManager[AsyncSession]:
    return get_global_config().db_session.context(state, request)


def get_app_lifespan_service() -> AppLifespanService | None:
    config = get_global_config()
    return config.lifespan


def depends_event_service():
    injector = get_global_config().event
    assert injector is not None
    return Depends(injector.depends)


def depends_event_callback_service():
    injector = get_global_config().event_callback
    assert injector is not None
    return Depends(injector.depends)


def depends_sandbox_service():
    """Return a FastAPI dependency for sandbox operations.

    Returns the registry which provides all available sandbox types.
    """

    async def _get_sandbox_service():
        registry = get_sandbox_registry()
        if registry is None:
            raise RuntimeError('Sandbox registry not initialized')
        yield registry

    return Depends(_get_sandbox_service)


def depends_sandbox_spec_service():
    """Return a FastAPI dependency for sandbox spec operations.

    Returns specs from the registry which includes all available sandbox types.
    """
    from openhands.app_server.sandbox.sandbox_spec_models import SandboxSpecInfoPage

    class RegistrySpecAdapter:
        """Adapts SandboxRegistry to SandboxSpecService interface for spec operations."""

        def __init__(self, registry: SandboxRegistry):
            self._registry = registry

        async def search_sandbox_specs(
            self, page_id: str | None = None, limit: int = 100
        ) -> SandboxSpecInfoPage:
            return await self._registry.search_all_specs(page_id=page_id, limit=limit)

        async def get_sandbox_spec(self, spec_id: str) -> 'SandboxSpecInfo | None':
            from openhands.app_server.sandbox.sandbox_spec_models import SandboxSpecInfo

            return await self._registry.get_spec(spec_id)

        async def batch_get_sandbox_specs(
            self, spec_ids: list[str]
        ) -> list['SandboxSpecInfo | None']:
            from openhands.app_server.sandbox.sandbox_spec_models import SandboxSpecInfo

            return [await self.get_sandbox_spec(sid) for sid in spec_ids]

    async def _get_sandbox_spec_service():
        registry = get_sandbox_registry()
        if registry is None:
            raise RuntimeError('Sandbox registry not initialized')
        yield RegistrySpecAdapter(registry)

    return Depends(_get_sandbox_spec_service)


def depends_app_conversation_info_service():
    injector = get_global_config().app_conversation_info
    assert injector is not None
    return Depends(injector.depends)


def depends_app_conversation_start_task_service():
    injector = get_global_config().app_conversation_start_task
    assert injector is not None
    return Depends(injector.depends)


def depends_app_conversation_service():
    injector = get_global_config().app_conversation
    assert injector is not None
    return Depends(injector.depends)


def depends_pending_message_service():
    injector = get_global_config().pending_message
    assert injector is not None
    return Depends(injector.depends)


def depends_user_context():
    injector = get_global_config().user
    assert injector is not None
    return Depends(injector.depends)


def depends_httpx_client():
    return Depends(get_global_config().httpx.depends)


def depends_jwt_service():
    injector = get_global_config().jwt
    assert injector is not None
    return Depends(injector.depends)


def depends_db_session():
    return Depends(get_global_config().db_session.depends)
