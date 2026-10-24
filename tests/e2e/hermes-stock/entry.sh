#!/bin/bash
# The test Hermes: its gateway, plus a stand-in for "the agent runs what the user pasted" (see the Dockerfile).
# `hermes gateway restart` (the connect script ends with one) replaces the gateway process, so this script is the
# container's long-lived process instead of the gateway.
hermes gateway run >/tmp/gateway.log 2>&1 &
mkdir -p /shared
while true; do
    if [ -f /shared/run.sh ]; then
        mv /shared/run.sh /shared/running.sh
        bash /shared/running.sh >/shared/run.out 2>&1
        echo "exit=$?" >>/shared/run.out
        mv /shared/running.sh /shared/done.sh
    fi
    sleep 1
done
