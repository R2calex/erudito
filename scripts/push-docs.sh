#!/bin/bash
# push-docs.sh — Push .md documentation from a satellite node to Erudito
#
# Usage:
#   ./push-docs.sh <project_name> <docs_path> [node_name] [auto_curate]
#
# Examples:
#   ./push-docs.sh openclaw-kubo ~/ai-lab/openclaw/docs kubo true
#   ./push-docs.sh labforge ~/ai-lab/labforge sariatu false
#
# Requires: curl, jq (optional for pretty output)

set -e

PROJECT="${1:?Usage: push-docs.sh <project> <path> [node] [auto_curate]}"
DOCS_PATH="${2:?Usage: push-docs.sh <project> <path> [node] [auto_curate]}"
NODE="${3:-$(hostname)}"
AUTO_CURATE="${4:-false}"
ERUDITO_URL="${ERUDITO_URL:-http://100.119.223.20:8095}"

# Find all .md files
FILES_JSON="["
FIRST=true
COUNT=0
while IFS= read -r -d '' file; do
    # Skip excluded dirs
    case "$file" in
        *node_modules*|*.git/*|*__pycache__*|*venv/*|*.venv/*) continue ;;
    esac

    BASENAME=$(basename "$file")
    CONTENT=$(cat "$file" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")

    if [ "$FIRST" = true ]; then
        FIRST=false
    else
        FILES_JSON+=","
    fi
    FILES_JSON+="{\"path\":\"$BASENAME\",\"content\":$CONTENT}"
    COUNT=$((COUNT + 1))
done < <(find "$DOCS_PATH" -name "*.md" -print0)
FILES_JSON+="]"

echo "Pushing $COUNT .md files from $DOCS_PATH to Erudito ($ERUDITO_URL)"
echo "Project: $PROJECT, Node: $NODE, Auto-curate: $AUTO_CURATE"

# POST to Erudito
RESPONSE=$(curl -s -X POST "$ERUDITO_URL/ingest/$PROJECT" \
    -H "Content-Type: application/json" \
    -d "{\"files\":$FILES_JSON,\"node\":\"$NODE\",\"auto_curate\":$AUTO_CURATE}")

echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
