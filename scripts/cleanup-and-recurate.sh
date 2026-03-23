#!/bin/bash
# cleanup-and-recurate.sh — Purge contaminated NLM notes and re-curate all projects
#
# Run this at the start of a session when NLM tokens have refreshed.
# Processes 2 projects at a time to stay within NLM rate limits.
#
# Usage: ./scripts/cleanup-and-recurate.sh

set -e
ERUDITO="http://localhost:8095"

echo "=== Step 1: Purge contaminated notes from Qdrant ==="
docker exec erudito python3 -c "
from core.indexer import _qdrant_request, COLLECTION_NLM_NOTES
result = _qdrant_request(f'/collections/{COLLECTION_NLM_NOTES}/points/scroll', data={
    'limit': 500, 'with_payload': True, 'with_vector': False,
})
points = result.get('result', {}).get('points', [])
dirty = [p['id'] for p in points if 'RESOURCE_EXHAUSTED' in str(p.get('payload',{}).get('text',''))[:200] or 'error' in str(p.get('payload',{}).get('text',''))[:30].lower()]
if dirty:
    _qdrant_request(f'/collections/{COLLECTION_NLM_NOTES}/points/delete', data={'points': dirty})
    print(f'Purged {len(dirty)} contaminated notes')
else:
    print('No contaminated notes found')
remaining = len(points) - len(dirty)
print(f'Remaining clean notes: {remaining}')
"

echo ""
echo "=== Step 2: Reset NLM circuit breaker ==="
curl -s -X POST "$ERUDITO/nlm/reset" | python3 -m json.tool

echo ""
echo "=== Step 3: Test NLM connectivity ==="
NLM_STATUS=$(curl -s "$ERUDITO/nlm/status" | python3 -c "import sys,json; print(json.load(sys.stdin)['tripped'])")
if [ "$NLM_STATUS" = "True" ]; then
    echo "ERROR: NLM still rate limited. Try again later."
    exit 1
fi

# Test with a simple query
docker exec erudito python3 -c "
import asyncio, sys
sys.path.insert(0, '/app')
async def test():
    from integrations.notebooklm import list_notebooks
    nbs = await list_notebooks()
    print(f'NLM OK: {len(nbs)} notebooks accessible')
asyncio.run(test())
"

echo ""
echo "=== Step 4: Re-curate projects (2 at a time) ==="

# Projects to re-curate (all except erudito which is clean)
PROJECTS=(
    "infra-mcp devops-agent"
    "mesh-monitor event-bus"
    "marker-mcp qdrant-mcp"
    "agent-eval opencode-contracts"
    "jasper jasper-profiles"
    "kubo-contracts sariatu-docs"
    "openclaw-kubo"
)

for PAIR in "${PROJECTS[@]}"; do
    echo ""
    echo "--- Processing: $PAIR ---"
    for PROJECT in $PAIR; do
        echo "  Curating $PROJECT..."
        RESULT=$(curl -s -X POST "$ERUDITO/curate/$PROJECT")
        STATUS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
        echo "  $PROJECT: $STATUS"
    done

    # Check if circuit breaker tripped
    TRIPPED=$(curl -s "$ERUDITO/nlm/status" | python3 -c "import sys,json; print(json.load(sys.stdin)['tripped'])" 2>/dev/null)
    if [ "$TRIPPED" = "True" ]; then
        echo ""
        echo "WARNING: NLM rate limited. Stopping. Resume tomorrow."
        echo "Remaining projects will be processed in the next run."
        exit 0
    fi

    echo "  Waiting 30s before next pair..."
    sleep 30
done

echo ""
echo "=== Done! Verifying ==="
curl -s "$ERUDITO/metrics" | python3 -c "
import sys,json
d = json.load(sys.stdin)
c = d['coverage']
print(f'Projects: {c[\"total\"]}, Validated: {c[\"validated\"]}')
print(f'Curation: {c[\"curation_pct\"]}%')
print(f'NLM circuit: {d.get(\"nlm_health_24h_pct\",\"?\")}%')
"
