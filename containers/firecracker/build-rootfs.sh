#!/bin/bash
# Build a Firecracker rootfs image with OpenHands agent-server
#
# This script creates an ext4 filesystem image containing:
# - Minimal Debian/Ubuntu base system
# - OpenHands agent-server and dependencies
# - Network configuration for Firecracker TAP networking
#
# Usage: ./build-rootfs.sh [output_path]
#
# Requirements:
# - Docker (to build in isolated environment)
# - sudo access (for mounting and filesystem operations)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUTPUT_PATH="${1:-/var/lib/firecracker/rootfs.ext4}"
ROOTFS_SIZE_MB="${ROOTFS_SIZE_MB:-2048}"
AGENT_SERVER_IMAGE="${AGENT_SERVER_IMAGE:-ghcr.io/openhands/agent-server:latest}"

echo "Building Firecracker rootfs image..."
echo "  Output: $OUTPUT_PATH"
echo "  Size: ${ROOTFS_SIZE_MB}MB"
echo "  Agent Server Image: $AGENT_SERVER_IMAGE"

# Create temporary directory for building
BUILD_DIR=$(mktemp -d)
trap "rm -rf $BUILD_DIR" EXIT

echo ""
echo "Step 1: Export agent-server container filesystem..."
docker pull "$AGENT_SERVER_IMAGE"
CONTAINER_ID=$(docker create "$AGENT_SERVER_IMAGE")
docker export "$CONTAINER_ID" > "$BUILD_DIR/rootfs.tar"
docker rm "$CONTAINER_ID" > /dev/null

echo ""
echo "Step 2: Create ext4 filesystem image..."
# Create sparse file
truncate -s "${ROOTFS_SIZE_MB}M" "$BUILD_DIR/rootfs.ext4"
# Format as ext4
mkfs.ext4 -F "$BUILD_DIR/rootfs.ext4"

echo ""
echo "Step 3: Mount and populate filesystem..."
MOUNT_DIR="$BUILD_DIR/mnt"
mkdir -p "$MOUNT_DIR"
sudo mount -o loop "$BUILD_DIR/rootfs.ext4" "$MOUNT_DIR"

# Extract container filesystem
sudo tar -xf "$BUILD_DIR/rootfs.tar" -C "$MOUNT_DIR"

echo ""
echo "Step 4: Configure guest system..."

# Create init script for network configuration
sudo tee "$MOUNT_DIR/etc/init.d/network-config" > /dev/null << 'EOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          network-config
# Required-Start:    $local_fs
# Required-Stop:
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: Configure network from kernel cmdline
### END INIT INFO

# Parse IP from kernel command line
# Format: ip=<guest_ip>:::<gateway>:<prefix>:<iface>:off
CMDLINE=$(cat /proc/cmdline)
IP_CONFIG=$(echo "$CMDLINE" | grep -oE 'ip=[^ ]+' | head -1 | cut -d= -f2)

if [ -n "$IP_CONFIG" ]; then
    GUEST_IP=$(echo "$IP_CONFIG" | cut -d: -f1)
    GATEWAY=$(echo "$IP_CONFIG" | cut -d: -f4)
    PREFIX=$(echo "$IP_CONFIG" | cut -d: -f5)
    IFACE=$(echo "$IP_CONFIG" | cut -d: -f6)

    [ -z "$IFACE" ] && IFACE="eth0"
    [ -z "$PREFIX" ] && PREFIX="30"

    ip addr add "${GUEST_IP}/${PREFIX}" dev "$IFACE" 2>/dev/null || true
    ip link set "$IFACE" up
    ip route add default via "$GATEWAY" 2>/dev/null || true

    echo "Network configured: ${GUEST_IP}/${PREFIX} via $GATEWAY on $IFACE"
fi

# Parse session API key from kernel command line
SESSION_KEY=$(echo "$CMDLINE" | grep -oE 'OH_SESSION_API_KEY=[^ ]+' | head -1 | cut -d= -f2)
if [ -n "$SESSION_KEY" ]; then
    export OH_SESSION_API_KEYS_0="$SESSION_KEY"
fi
EOF
sudo chmod +x "$MOUNT_DIR/etc/init.d/network-config"

# Create systemd service for network config (if systemd is present)
if [ -d "$MOUNT_DIR/etc/systemd/system" ]; then
    sudo tee "$MOUNT_DIR/etc/systemd/system/network-config.service" > /dev/null << 'EOF'
[Unit]
Description=Configure network from kernel cmdline
After=local-fs.target
Before=network.target

[Service]
Type=oneshot
ExecStart=/etc/init.d/network-config
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    sudo ln -sf /etc/systemd/system/network-config.service "$MOUNT_DIR/etc/systemd/system/multi-user.target.wants/" 2>/dev/null || true
fi

# Create agent-server startup script
sudo tee "$MOUNT_DIR/start-agent-server.sh" > /dev/null << 'EOF'
#!/bin/bash
# Start the OpenHands agent server

# Source environment from kernel cmdline
CMDLINE=$(cat /proc/cmdline)
SESSION_KEY=$(echo "$CMDLINE" | grep -oE 'OH_SESSION_API_KEY=[^ ]+' | head -1 | cut -d= -f2)
if [ -n "$SESSION_KEY" ]; then
    export OH_SESSION_API_KEYS_0="$SESSION_KEY"
fi

# Start agent server
cd /app
exec python -m openhands.agent_server.listen --port 8000
EOF
sudo chmod +x "$MOUNT_DIR/start-agent-server.sh"

# Configure DNS
echo "nameserver 8.8.8.8" | sudo tee "$MOUNT_DIR/etc/resolv.conf" > /dev/null
echo "nameserver 8.8.4.4" | sudo tee -a "$MOUNT_DIR/etc/resolv.conf" > /dev/null

# Set hostname
echo "openhands-sandbox" | sudo tee "$MOUNT_DIR/etc/hostname" > /dev/null

echo ""
echo "Step 5: Unmount and finalize..."
sudo umount "$MOUNT_DIR"

# Move to final location
sudo mkdir -p "$(dirname "$OUTPUT_PATH")"
sudo mv "$BUILD_DIR/rootfs.ext4" "$OUTPUT_PATH"
sudo chmod 644 "$OUTPUT_PATH"

echo ""
echo "✅ Rootfs image created: $OUTPUT_PATH"
echo ""
echo "To use with Firecracker:"
echo "  1. Download a compatible kernel:"
echo "     curl -L -o /var/lib/firecracker/vmlinux \\"
echo "       'https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/\$(uname -m)/vmlinux-6.1.155'"
echo ""
echo "  2. Enable Firecracker in docker-compose.yml:"
echo "     environment:"
echo "       - RUNTIME=firecracker"
echo "     devices:"
echo "       - /dev/kvm:/dev/kvm"
echo "     cap_add:"
echo "       - NET_ADMIN"
