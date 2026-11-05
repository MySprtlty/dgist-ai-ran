#!/bin/bash
# core.sh up | down | status - on/off switch of the Open5GS core network on this host (18 systemd units + NAT for the UE subnet).
# up   = start the units, wait for ogstun 10.45.0.1, add the UE NAT rules (core/routing.sh)
# down = remove the NAT rules (core/routing_restore.sh), stop the units
# Needs sudo for systemctl/iptables: the nvidia account is asked for its password. Implementation: scripts/core.sh (settings: .env UE_SUBNET)
exec bash "$(cd "$(dirname "$0")" && pwd)/scripts/core.sh" "${1:-status}"
