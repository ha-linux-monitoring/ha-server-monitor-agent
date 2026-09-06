#!/bin/bash
# smartd -M exec hook. The agent daemon resolves the raw device to its
# owning RAID array automatically if it's a member disk rather than its
# own physical device.
DEVICE=$(basename "${SMARTD_DEVICESTRING:-unknown}")
MESSAGE="smartd on $(hostname): ${SMARTD_SUBJECT:-alert} - ${SMARTD_MESSAGE:-check smartd}"
curl -fsS -m 5 -X POST "http://127.0.0.1:8477/trigger-event" \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"raw_id": sys.argv[1], "message": sys.argv[2]}))' \
        "$DEVICE" "$MESSAGE")"
