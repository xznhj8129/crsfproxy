#!/usr/bin/env bash
set -euo pipefail

# Run from the repository root. Adjust the two TCP endpoints to the SITL UARTs
# assigned to the primary and secondary CRSF receivers.

python crsf_rx_sim.py \
    --name primary \
    --endpoint tcp://127.0.0.1:5763 \
    --host 127.0.0.1 --port 60000 \
    --control_host 127.0.0.1 --control_port 60001 \
    --rc_rate 150 --link_rate 10 \
    --telemetry_udp 127.0.0.1:40042 &
primary_pid=$!

python crsf_rx_sim.py \
    --name secondary \
    --endpoint tcp://127.0.0.1:5764 \
    --host 127.0.0.1 --port 60010 \
    --control_host 127.0.0.1 --control_port 60011 \
    --rc_rate 50 --link_rate 10 \
    --telemetry_udp 127.0.0.1:40043 &
secondary_pid=$!

trap 'kill "$primary_pid" "$secondary_pid" 2>/dev/null || true' EXIT INT TERM
wait
