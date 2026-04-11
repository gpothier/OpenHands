# Docker Socket Passthrough

> [!CAUTION]
> ## ⚠️ SECURITY WARNING ⚠️
>
> **Docker socket passthrough is inherently insecure and should be considered equivalent to granting root access to the host machine.**
>
> While this document describes OPA (Open Policy Agent) as a potential mitigation, **we have not yet found a way to fully secure Docker socket access with OPA**. The OPA policies described below can be bypassed in various ways, including but not limited to:
>
> - Building malicious images that execute code during build
> - Using `docker cp` to read/write arbitrary files
> - Exploiting race conditions in policy evaluation
> - Other attack vectors that may not be covered by policies
>
> **DO NOT use this feature in production or multi-tenant environments.**
>
> Only enable this in fully trusted, isolated development environments where all users have equivalent host root access anyway.

---

This document describes how to configure Docker socket passthrough for OpenHands sandboxes and how to secure it using OPA (Open Policy Agent) authorization.

## Overview

The `SANDBOX_DOCKER_SOCKET_PASSTHROUGH=true` option mounts the host Docker socket (`/var/run/docker.sock`) into sandbox containers, allowing them to interact with the host Docker daemon directly.

**WARNING**: Without an authorization plugin (see below) or similar measure, this grants sandboxes full access to the host Docker daemon, which is equivalent to root access on the host machine. Only enable this in trusted environments.

## Basic Usage

```bash
export SANDBOX_DOCKER_SOCKET_PASSTHROUGH=true
```

When enabled, sandboxes can run Docker commands that interact with the host Docker daemon:
- Build images
- Run containers
- Pull/push images
- Manage networks and volumes

## Security with OPA Docker Authorization

For production deployments, we strongly recommend using the [OPA Docker authorization plugin](https://github.com/open-policy-agent/opa-docker-authz) to restrict what operations sandboxes can perform through the Docker API.

### How OPA Authorization Works

OPA acts as an authorization middleware that intercepts every Docker API request before it reaches the daemon. It can inspect request parameters (including the JSON body) and allow or deny operations based on policy rules.

### Prerequisites

- Docker Engine 18.06.0-ce or newer
- Docker API version 1.38 or newer
- `root` or `sudo` access on the Docker host

### Installation Steps (Simple Local Policy File)

These steps use a **local policy file** - no web server or bundles required.

#### 1. Create the OPA Policy File

Create `/etc/docker/authz.rego` with your security policy:

```rego
package docker.authz

default allow := false

# Note: The plugin provides these parsed fields:
# - input.PathPlain: URL path without query string
# - input.PathArr: PathPlain split by '/'
# - input.Body: Parsed JSON request body

# ============================================================================
# READ-ONLY OPERATIONS - Always allowed
# ============================================================================

allow if {
    input.Method == "GET"
}

allow if {
    input.Method == "HEAD"
}

# ============================================================================
# CONTAINER CREATION - Allowed with security restrictions
# ============================================================================

# Allow creating containers, but block dangerous configurations
allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/containers/create$`, input.PathPlain)
    not privileged_container
    not disallowed_binds
    not host_network
    not host_pid
    not dangerous_capabilities
}

# ============================================================================
# CONTAINER LIFECYCLE - Allow start, stop, attach, etc.
# ============================================================================

allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/containers/[^/]+/(start|stop|kill|restart|pause|unpause|wait|resize)$`, input.PathPlain)
}

# Allow attach (needed for docker run)
allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/containers/[^/]+/attach`, input.PathPlain)
}

# Allow container delete (needed for docker run --rm)
allow if {
    input.Method == "DELETE"
    regex.match(`^/v[0-9.]+/containers/[^/]+$`, input.PathPlain)
}

# ============================================================================
# IMAGE OPERATIONS - Pull, build, delete
# ============================================================================

# Allow pulling images
allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/images/create$`, input.PathPlain)
}

# Allow building images
allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/build$`, input.PathPlain)
}

# Allow deleting images
allow if {
    input.Method == "DELETE"
    regex.match(`^/v[0-9.]+/images/[^/]+$`, input.PathPlain)
}

# Allow tagging images
allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/images/[^/]+/tag$`, input.PathPlain)
}

# ============================================================================
# EXEC OPERATIONS - Allow exec into containers
# ============================================================================

allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/containers/[^/]+/exec$`, input.PathPlain)
}

allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/exec/[^/]+/(start|resize)$`, input.PathPlain)
}

# ============================================================================
# NETWORK & VOLUME OPERATIONS
# ============================================================================

allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/(networks|volumes)/create$`, input.PathPlain)
}

allow if {
    input.Method == "DELETE"
    regex.match(`^/v[0-9.]+/(networks|volumes)/[^/]+$`, input.PathPlain)
}

allow if {
    input.Method == "POST"
    regex.match(`^/v[0-9.]+/networks/[^/]+/(connect|disconnect)$`, input.PathPlain)
}

# ============================================================================
# DANGEROUS CONFIGURATION DETECTION
# ============================================================================

# Block privileged containers
privileged_container if {
    input.Body.HostConfig.Privileged == true
}

# Block volume mounts that are not in the whitelist
# We use a whitelist approach - only explicitly allowed paths can be mounted
disallowed_binds if {
    bind := input.Body.HostConfig.Binds[_]
    not allowed_bind_path(bind)
}

disallowed_binds if {
    mount := input.Body.HostConfig.Mounts[_]
    mount.Type == "bind"
    not allowed_mount_source(mount.Source)
}

# Also check resolved paths (handles symlink attacks)
disallowed_binds if {
    resolved := input.BindMounts[_]
    resolved.Resolved != ""
    not allowed_mount_source(resolved.Resolved)
}

# Parse bind string and check if source is allowed
allowed_bind_path(bind) if {
    # Bind format is "source:dest" or "source:dest:mode"
    parts := split(bind, ":")
    source := parts[0]
    allowed_mount_source(source)
}

# Whitelist of allowed mount sources
allowed_mount_source(source) if {
    startswith(source, "/tmp")
}

allowed_mount_source(source) if {
    startswith(source, "/mnt")
}

allowed_mount_source(source) if {
    startswith(source, "/home")
}

allowed_mount_source(source) if {
    source == "/var/run/docker.sock"
}

# Block host network mode
host_network if {
    input.Body.HostConfig.NetworkMode == "host"
}

# Block host PID namespace
host_pid if {
    input.Body.HostConfig.PidMode == "host"
}

# Block dangerous capabilities
dangerous_capabilities if {
    cap := input.Body.HostConfig.CapAdd[_]
    cap == "SYS_ADMIN"
}

dangerous_capabilities if {
    cap := input.Body.HostConfig.CapAdd[_]
    cap == "NET_ADMIN"
}
```

#### 2. Install the OPA Docker Authorization Plugin

Install the plugin and point it to your local policy file:

```bash
docker plugin install --alias opa-docker-authz \
  ghcr.io/open-policy-agent/opa-docker-authz:v0.10 \
  opa-args="-policy-file /opa/authz.rego"
```

> **Note:** The plugin mounts `/etc/docker` as `/opa`, so `/etc/docker/authz.rego` is accessible as `/opa/authz.rego` inside the plugin.

#### 3. Configure Docker Daemon

Add the authorization plugin to your Docker daemon configuration:

```bash
sudo cat > /etc/docker/daemon.json <<EOF
{
    "authorization-plugins": ["opa-docker-authz"]
}
EOF
```

#### 4. Reload Docker Daemon

```bash
sudo kill -HUP $(pidof dockerd)
```

### Testing the Policy

After installation, test that the policy is working:

```bash
# This should SUCCEED (normal container, no mounts)
docker run --rm hello-world

# This should SUCCEED (mount from allowed path /tmp)
docker run --rm -v /tmp/test:/data hello-world

# This should SUCCEED (mount from allowed path /home)
docker run --rm -v /home/user/project:/workspace hello-world

# This should SUCCEED (mount docker socket)
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock hello-world

# This should FAIL (privileged container)
docker run --rm --privileged hello-world
# Expected: authorization denied by plugin opa-docker-authz

# This should FAIL (mount from non-whitelisted path)
docker run --rm -v /etc/passwd:/etc/passwd hello-world
# Expected: authorization denied by plugin opa-docker-authz

# This should FAIL (mounting root filesystem)
docker run --rm -v /:/host hello-world
# Expected: authorization denied by plugin opa-docker-authz

# This should FAIL (host network)
docker run --rm --network host hello-world
# Expected: authorization denied by plugin opa-docker-authz

# This should FAIL (dangerous capability)
docker run --rm --cap-add SYS_ADMIN hello-world
# Expected: authorization denied by plugin opa-docker-authz

# This should FAIL (host PID namespace)
docker run --rm --pid host hello-world
# Expected: authorization denied by plugin opa-docker-authz
```

### Troubleshooting

If you get `403` errors, check the Docker daemon logs for OPA decision details:

```bash
# View recent authorization decisions
sudo journalctl -u docker --since "5 minutes ago" | grep "OPA policy decision"
```

The logs show the full `input` document including `HostConfig` with `Privileged`, `Binds`, `Mounts`, `NetworkMode`, `PidMode`, and `CapAdd` fields. Look at these to understand why a request was denied.

**Note:** The error message shown to users ("request rejected by administrative policy") is hardcoded in the plugin and cannot be customized.

### Policy Customization

The example policy above:

**Blocks:**
- **Privileged containers** (`--privileged`)
- **Host network mode** (`--network host`)
- **Host PID namespace** (`--pid host`)
- **Dangerous capabilities** (`--cap-add SYS_ADMIN`, `--cap-add NET_ADMIN`)

**Allows volume mounts only from (whitelist):**
- `/tmp/*`
- `/mnt/*`
- `/home/*`
- `/var/run/docker.sock` (exactly)

**Protects against symlink attacks** via `input.BindMounts[_].Resolved` path checking.

You can customize the whitelist by adding more `allowed_mount_source` rules:
```rego
# Example: also allow /data directory
allowed_mount_source(source) if {
    startswith(source, "/data")
}
```

### Docker Buildx

The `docker-container` driver for buildx requires privileged mode and will be blocked by this policy. Use the default driver instead:

```bash
docker buildx use default
```

This setting is persistent (stored in `~/.docker/buildx/current`). The default driver uses the Docker daemon's built-in BuildKit and works without privileged containers.

### Updating Policies

To update the policy, simply edit `/etc/docker/authz.rego`. The plugin reloads policy on each request when using `-policy-file`, so changes take effect immediately without restarting Docker.

## Quick Setup Summary

```bash
# 1. Create policy file (copy the example above)
sudo tee /etc/docker/authz.rego << 'EOF'
package docker.authz
# ... paste policy content here ...
EOF

# 2. Install the plugin
docker plugin install --alias opa-docker-authz \
  ghcr.io/open-policy-agent/opa-docker-authz:v0.10 \
  opa-args="-policy-file /opa/authz.rego"

# 3. Configure Docker daemon
sudo tee /etc/docker/daemon.json << 'EOF'
{
    "authorization-plugins": ["opa-docker-authz"]
}
EOF

# 4. Reload Docker
sudo kill -HUP $(pidof dockerd)
```

## References

- [OPA Docker Authorization Plugin](https://github.com/open-policy-agent/opa-docker-authz) - GitHub repository with full documentation
- [OPA Docker Tutorial](https://www.openpolicyagent.org/docs/docker-authorization) - Official OPA documentation (uses bundles, but the plugin supports local files too)
- [Docker Authorization Plugin Documentation](https://docs.docker.com/engine/extend/plugins_authorization/)
