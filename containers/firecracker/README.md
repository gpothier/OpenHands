# Firecracker Sandbox Setup

This directory contains resources for running OpenHands agent sandboxes in Firecracker microVMs instead of Docker containers.

## Overview

Firecracker microVMs provide hardware-level isolation using KVM while maintaining:
- Fast startup times (~125ms)
- Low memory overhead (<5 MiB per VM)
- Strong security isolation

## Prerequisites

1. **Linux host with KVM support**
   ```bash
   # Check if KVM is available
   ls -la /dev/kvm
   # Should show: crw-rw---- 1 root kvm ...
   ```

2. **KVM access for your user**
   ```bash
   # Add user to kvm group
   sudo usermod -aG kvm $USER
   # Log out and back in for changes to take effect
   ```

## Quick Start

### 1. Download Kernel Image

Firecracker provides pre-built kernels in their CI:

```bash
# Create directory for Firecracker resources
mkdir -p /var/lib/firecracker

# Download kernel (x86_64)
ARCH=$(uname -m)
curl -L -o /var/lib/firecracker/vmlinux \
  "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/${ARCH}/vmlinux-6.1.155"
```

### 2. Build Rootfs Image

The rootfs needs to contain the OpenHands agent-server. Use the build script:

```bash
cd containers/firecracker
./build-rootfs.sh
```

This creates `/var/lib/firecracker/rootfs.ext4` with:
- Base Ubuntu/Debian system
- OpenHands agent-server
- Required dependencies
- Network configuration

### 3. Configure docker-compose.yml

Edit `docker-compose.yml` to enable Firecracker:

```yaml
services:
  openhands:
    environment:
      - RUNTIME=firecracker
      - FIRECRACKER_VCPU_COUNT=2
      - FIRECRACKER_MEM_SIZE_MIB=1024
    volumes:
      - /var/lib/firecracker/vmlinux:/var/lib/firecracker/vmlinux:ro
      - /var/lib/firecracker/rootfs.ext4:/var/lib/firecracker/rootfs.ext4:ro
    devices:
      - /dev/kvm:/dev/kvm
    cap_add:
      - NET_ADMIN
```

### 4. Start OpenHands

```bash
docker compose up
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RUNTIME` | `docker` | Set to `firecracker` to enable VM sandboxes |
| `FIRECRACKER_BIN` | `/usr/local/bin/firecracker` | Path to firecracker binary |
| `FIRECRACKER_KERNEL_PATH` | `/var/lib/firecracker/vmlinux` | Path to guest kernel |
| `FIRECRACKER_ROOTFS_PATH` | `/var/lib/firecracker/rootfs.ext4` | Path to base rootfs |
| `FIRECRACKER_VCPU_COUNT` | `2` | vCPUs per VM |
| `FIRECRACKER_MEM_SIZE_MIB` | `1024` | Memory per VM in MiB |

## Troubleshooting

### "KVM not available"

Ensure:
1. Host supports hardware virtualization (Intel VT-x or AMD-V)
2. KVM module is loaded: `lsmod | grep kvm`
3. `/dev/kvm` exists and is accessible

### "Permission denied on /dev/kvm"

```bash
# Check permissions
ls -la /dev/kvm

# Add user to kvm group
sudo usermod -aG kvm $USER

# Or use ACL
sudo setfacl -m u:$USER:rw /dev/kvm
```

### "Failed to create TAP device"

The container needs `NET_ADMIN` capability:
```yaml
cap_add:
  - NET_ADMIN
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ OpenHands Container                                         │
│                                                             │
│  ┌──────────────┐    ┌──────────────────────────────────┐  │
│  │ App Server   │    │ Firecracker MicroVM              │  │
│  │              │───▶│  ┌────────────────────────────┐  │  │
│  │              │    │  │ Agent Server (port 8000)   │  │  │
│  └──────────────┘    │  └────────────────────────────┘  │  │
│         │            │              │                    │  │
│         │            └──────────────┼────────────────────┘  │
│         │                           │                       │
│     REST API              TAP network (172.16.x.x/30)       │
│                                     │                       │
└─────────────────────────────────────┼───────────────────────┘
                                      │
                                 /dev/kvm
                            (host kernel KVM)
```

## Security Considerations

Firecracker provides stronger isolation than containers:
- Each sandbox runs in its own VM with separate kernel
- Hardware-enforced memory isolation via KVM
- Minimal attack surface (only 5 emulated devices)
- No shared kernel with host or other sandboxes

However, the OpenHands container still needs elevated privileges:
- `/dev/kvm` access for VM creation
- `NET_ADMIN` for TAP device management

## References

- [Firecracker Documentation](https://github.com/firecracker-microvm/firecracker/tree/main/docs)
- [Getting Started with Firecracker](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)
- [Firecracker Kernel Requirements](https://github.com/firecracker-microvm/firecracker/blob/main/docs/kernel-policy.md)
