#!/bin/bash
# mdadm PROGRAM hook: $1=event, $2=md device (e.g. /dev/md127), $3=component device (optional)
DEVICE=$(basename "$2")
curl -fsS -m 5 -X POST "http://127.0.0.1:8477/trigger-event" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"raw_id": sys.argv[1], "message": sys.argv[2]}))' \
        "$DEVICE" "mdadm on $(hostname): $1 $2 $3")"
