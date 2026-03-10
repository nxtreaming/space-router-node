# Troubleshooting Guide

## UPnP Failures
If the node reports UPnP failure:
- Ensure UPnP is enabled on your router.
- Try manual port forwarding (default: 9090) to the node's local IP.

## Firewall Configuration
- **Linux:** `sudo ufw allow 9090/tcp`
- **macOS:** Check System Settings > Network > Firewall.
- **Windows:** Ensure the installer added the inbound rule for port 9090.

## Logs
- **Linux:** `journalctl -u space-router-node`
- **macOS:** `tail -f ~/Library/Logs/SpaceRouter/node.log`
- **Windows:** Check Event Viewer or `C:\ProgramData\SpaceRouter\Logs`.
