#!/usr/bin/env bash
# Example: send a Slack webhook notification.
# Usage in notify.yaml:
#
#   - match_type: task_error
#     command: bash /path/to/examples/slack-notify.sh
#
# Set SLACK_WEBHOOK_URL in your environment.

MESSAGE="${MESSAGE:-$(cat)}"
TEXT=$(echo "$MESSAGE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('body',{}).get('text',''))")

curl -s -X POST "$SLACK_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"[$TYPE] $TEXT\"}"
