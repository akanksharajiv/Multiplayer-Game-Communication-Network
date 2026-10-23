#!/bin/bash
set -e

if ! ovs-vsctl br-exists s1; then
    echo "ERROR: Start Mininet first."
    exit 1
fi

for PORT in s1-eth1 s1-eth2 s1-eth3
do
    EXISTING=$(ovs-vsctl get Port "$PORT" qos)

    if [ "$EXISTING" != "[]" ]; then
        echo "$PORT already has QoS. Skipping."
        continue
    fi

    ovs-vsctl \
    -- set Port "$PORT" qos=@qos \
    -- --id=@qos create QoS \
       type=linux-htb \
       other-config:max-rate=10000000 \
       queues:0=@q0 queues:1=@q1 \
    -- --id=@q0 create Queue \
       other-config:min-rate=2000000 \
       other-config:priority=5 \
    -- --id=@q1 create Queue \
       other-config:min-rate=8000000 \
       other-config:priority=0

    echo "QoS configured on $PORT"
done

echo "QoS setup complete!"
