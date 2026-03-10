#!/usr/bin/env bash
# Uninstall Space Router Home Node from macOS.
#
# Usage: sudo bash packaging/macos/uninstall.sh

set -e

echo "=== Uninstalling Space Router Home Node ==="

# Stop and unload the service
if launchctl list com.spacerouter.homenode &>/dev/null; then
    echo "Stopping service..."
    launchctl unload /Library/LaunchDaemons/com.spacerouter.homenode.plist 2>/dev/null || true
fi

# Remove launchd plist
if [ -f /Library/LaunchDaemons/com.spacerouter.homenode.plist ]; then
    echo "Removing launchd plist..."
    rm -f /Library/LaunchDaemons/com.spacerouter.homenode.plist
fi

# Remove binary and data
if [ -d /opt/spacerouter ]; then
    echo "Removing /opt/spacerouter..."
    rm -rf /opt/spacerouter
fi

# Note: preserve config for potential reinstall
if [ -d /etc/spacerouter ]; then
    echo ""
    echo "Configuration preserved at /etc/spacerouter/"
    echo "To remove it: sudo rm -rf /etc/spacerouter"
fi

# Forget the package receipt
pkgutil --forget com.spacerouter.homenode 2>/dev/null || true

echo ""
echo "=== Space Router Home Node uninstalled ==="
