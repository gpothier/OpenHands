# OpenHands Sandbox/Runtime Architecture Analysis

## Executive Summary

This document analyzes the current sandbox/runtime implementation in OpenHands to understand what's already implemented and what would be needed to support VM-based (KVM) sandboxes that can be selected from the UI.

## Current Architecture

### 1. Legacy V0 Runtime System (Deprecated)

**Location**: `openhands/runtime/`

The legacy runtime system provides direct runtime implementations used by the agent core. These are scheduled for removal on April 1, 2026.

**Base Class**: `Runtime` in `openhands/runtime/base.py`
- Abstract base class with methods for shell execution, file operations, browsing, etc.
- Implementations are selected via `get_runtime_cls(name)` function

**Built-in Implementations**:
| Name | Class | Description |
|------|-------|-------------|
| `docker` | `DockerRuntime` | Containerized execution using Docker |
| `remote` | `RemoteRuntime` | Remote execution via API |
| `local` | `LocalRuntime` | Local execution for development |
| `kubernetes` | `KubernetesRuntime` | Kubernetes pod-based execution |
| `cli` | `CLIRuntime` | Command-line interface runtime |

**Third-Party Runtimes** (`third_party/runtime/impl/`):
- `e2b` - E2B Firecracker microVMs (already VM-based!)
- `modal` - Modal.com serverless infrastructure
- `daytona` - Daytona development environments
- `runloop` - Runloop service

### 2. V1 Application Server Sandbox System (Current)

**Location**: `openhands/app_server/sandbox/`

The V1 system uses a more flexible dependency injection pattern.

#### Core Abstractions

**`SandboxService`** (`sandbox_service.py`):
```python
class SandboxService(ABC):
    async def search_sandboxes(...) -> SandboxPage
    async def get_sandbox(sandbox_id: str) -> SandboxInfo | None
    async def start_sandbox(sandbox_spec_id: str | None = None) -> SandboxInfo
    async def resume_sandbox(sandbox_id: str) -> bool
    async def pause_sandbox(sandbox_id: str) -> bool
    async def delete_sandbox(sandbox_id: str) -> bool
```

**`SandboxSpecService`** (`sandbox_spec_service.py`):
```python
class SandboxSpecService(ABC):
    async def search_sandbox_specs(...) -> SandboxSpecInfoPage
    async def get_sandbox_spec(sandbox_spec_id: str) -> SandboxSpecInfo | None
    async def get_default_sandbox_spec() -> SandboxSpecInfo
```

#### Current Implementations

| Runtime Type | Sandbox Service | Spec Service |
|-------------|-----------------|--------------|
| Docker (default) | `DockerSandboxService` | `DockerSandboxSpecServiceInjector` |
| Remote | `RemoteSandboxService` | `RemoteSandboxSpecServiceInjector` |
| Process/Local | `ProcessSandboxService` | `ProcessSandboxSpecServiceInjector` |

#### Configuration

Runtime type is selected via **environment variables** in `openhands/app_server/config.py`:

```python
if os.getenv('RUNTIME') == 'remote':
    config.sandbox = RemoteSandboxServiceInjector(...)
elif os.getenv('RUNTIME') in ('local', 'process'):
    config.sandbox = ProcessSandboxServiceInjector()
else:
    config.sandbox = DockerSandboxServiceInjector(...)
```

### 3. Existing KVM Support

**Key Finding**: The `DockerSandboxService` already supports KVM device passthrough!

In `docker_sandbox_service.py`:
```python
kvm_enabled: bool = Field(
    default_factory=_get_kvm_enabled_default,
    description=(
        'Whether to pass through /dev/kvm to sandbox containers for hardware '
        'virtualization support. When enabled, sandboxes can run KVM-accelerated '
        'virtual machines instead of using slower emulation.'
    ),
)

# In start_sandbox():
devices = ['/dev/kvm:/dev/kvm:rwm'] if self.kvm_enabled else None
```

**Environment Variable**: `SANDBOX_KVM_ENABLED=true`

This means **VMs can already run inside Docker containers** with hardware acceleration.

### 4. Frontend Integration

**Location**: `frontend/src/api/sandbox-service/`

The frontend interacts with sandboxes via:
- `GET /api/v1/sandboxes` - List/batch get sandboxes
- `POST /api/v1/sandboxes` - Start a sandbox (accepts optional `sandbox_spec_id`)
- `POST /api/v1/sandboxes/{id}/pause` - Pause sandbox
- `POST /api/v1/sandboxes/{id}/resume` - Resume sandbox

**Current Limitations**:
- No UI for selecting sandbox type
- Sandbox type is determined by backend configuration
- No visibility into available sandbox specs

## Data Models

### SandboxInfo
```typescript
interface SandboxInfo {
    id: string;
    created_by_user_id: string | null;
    sandbox_spec_id: string;  // <-- References the spec/type
    status: "MISSING" | "STARTING" | "RUNNING" | "PAUSED" | "ERROR";
    session_api_key: string | null;
    exposed_urls: ExposedUrl[] | null;
    created_at: string;
}
```

### SandboxSpecInfo
```python
class SandboxSpecInfo(BaseModel):
    id: str                                    # e.g., Docker image name
    command: list[str] | None
    created_at: datetime
    initial_env: dict[str, str]
    working_dir: str = '/home/openhands/workspace'
```

## Options for VM-Based Sandboxes

### Option A: VMs Inside Docker Containers (Easiest)

**Already Supported!** Just enable KVM passthrough:
```bash
export SANDBOX_KVM_ENABLED=true
```

The agent-server container can then use QEMU/KVM to run VMs internally.

**Pros**:
- No new backend code needed
- Leverages existing Docker infrastructure
- Works with current UI (no changes needed)

**Cons**:
- Nested virtualization overhead
- Container still manages the outer layer
- May not meet strict isolation requirements

### Option B: Direct KVM/libvirt Sandbox Service (More Work)

Create a new `KVMSandboxService` that uses libvirt to manage VMs directly.

**Required Components**:

1. **New Backend Services**:
   - `KVMSandboxService` - VM lifecycle management via libvirt
   - `KVMSandboxSpecService` - VM image/template management

2. **VM Image Management**:
   - Support for qcow2/raw disk images
   - Image building similar to container builds
   - Pre-built images for common configurations

3. **Network Configuration**:
   - Virtual networking for VM-host communication
   - Port mapping similar to Docker

4. **API Extensions**:
   - New sandbox spec types for VMs
   - VM-specific configuration options

### Option C: Hybrid with UI Selection (Recommended)

Extend the existing system to support multiple sandbox types with UI selection.

**Required Changes**:

1. **Backend - Extend SandboxSpecInfo**:
```python
class SandboxSpecInfo(BaseModel):
    id: str
    name: str                    # Human-readable name
    type: str                    # "docker" | "vm" | "remote"
    description: str | None      # For UI display
    command: list[str] | None
    initial_env: dict[str, str]
    working_dir: str
    # VM-specific fields (optional)
    vm_config: VMConfig | None
```

2. **Backend - Multiple Spec Services**:
   - Allow registering multiple sandbox spec services
   - Each provides specs for different types

3. **Frontend - Sandbox Type Selection**:
   - Add API endpoint: `GET /api/v1/sandbox-specs/search`
   - Add UI component for selecting sandbox spec/type
   - Store user preference for default sandbox type

4. **Configuration UI**:
   - Settings page section for sandbox preferences
   - Display available sandbox types
   - Allow setting default sandbox type per user

## Implementation Recommendations

### Phase 1: UI Selection for Existing Types
1. Add `/api/v1/sandbox-specs/search` endpoint usage in frontend
2. Create sandbox type selector component
3. Pass selected `sandbox_spec_id` when starting conversations

### Phase 2: Enhanced Sandbox Specs
1. Extend `SandboxSpecInfo` with `type` and display fields
2. Support multiple spec services
3. Add VM-type specs using KVM-enabled Docker containers

### Phase 3: Native VM Sandbox (If Needed)
1. Implement `KVMSandboxService` using libvirt
2. Create VM image build pipeline
3. Add VM-specific configuration options

## API Endpoints Summary

### Existing
- `GET /api/v1/sandboxes/search` - Search sandboxes
- `GET /api/v1/sandboxes?id=...` - Batch get sandboxes
- `POST /api/v1/sandboxes` - Start sandbox
- `POST /api/v1/sandboxes/{id}/pause` - Pause sandbox
- `POST /api/v1/sandboxes/{id}/resume` - Resume sandbox
- `DELETE /api/v1/sandboxes/{id}` - Delete sandbox
- `GET /api/v1/sandbox-specs/search` - Search specs
- `GET /api/v1/sandbox-specs?id=...` - Batch get specs

### To Be Added for UI Selection
- Frontend integration with sandbox-specs endpoints
- User preference storage for default sandbox type

## Conclusion

The OpenHands architecture already supports multiple sandbox types through its flexible dependency injection system. The key findings are:

1. **KVM support exists** - Docker sandboxes can already pass through `/dev/kvm`
2. **Multiple sandbox services** can be configured via environment variables
3. **The API supports spec selection** - `sandbox_spec_id` parameter exists
4. **Missing piece is UI** - Frontend needs components to select sandbox type

The recommended approach is to start with **Phase 1** (UI selection) which requires mostly frontend work, then expand to more sophisticated VM support as needed.
