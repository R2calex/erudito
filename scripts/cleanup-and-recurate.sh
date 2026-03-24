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
echo "=== Step 3.5: Identifying Tier 1 (NLM) projects ==="
TIER1_PROJECTS=$(python3 -c "
import sys, os
sys.path.insert(0, '.')
from core.registry import Registry
r = Registry(yaml_path='data/registry.yaml')
for name in r.list_all():
    e = r.get(name)
    if e and (e.get('tier') or e.get('computed_tier', 3)) == 1:
        print(name)
")
echo "Tier 1 projects: $TIER1_PROJECTS"

echo ""
echo "=== Step 4: Re-curate Tier 1 projects via NLM (2 at a time) ==="

# Build pairs from Tier 1 projects
TIER1_ARRAY=($TIER1_PROJECTS)
i=0
while [ $i -lt ${#TIER1_ARRAY[@]} ]; do
    PAIR="${TIER1_ARRAY[$i]}"
    if [ $((i + 1)) -lt ${#TIER1_ARRAY[@]} ]; then
        PAIR="$PAIR ${TIER1_ARRAY[$((i + 1))]}"
    fi

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
        echo "WARNING: NLM rate limited. Stopping NLM curation. Resume tomorrow."
        echo "Remaining Tier 1 projects will be processed in the next run."
        break
    fi

    echo "  Waiting 30s before next pair..."
    sleep 30
    i=$((i + 2))
done

echo ""
echo "=== Step 4b: Distilling Tier 2 and 3 projects (no NLM needed) ==="
for project in $(python3 -c "
import sys, os
sys.path.insert(0, '.')
from core.registry import Registry
r = Registry(yaml_path='data/registry.yaml')
for name in r.list_all():
    e = r.get(name)
    if e and (e.get('tier') or e.get('computed_tier', 3)) >= 2:
        print(name)
"); do
    echo "Distilling (LLM/Direct): $project"
    curl -s -X POST "http://localhost:8095/curate/$project" > /dev/null 2>&1
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
