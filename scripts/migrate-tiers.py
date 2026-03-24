#!/usr/bin/env python3
"""One-time migration: compute tiers for all existing projects and distill Tier 2/3.

Usage: python scripts/migrate-tiers.py [--dry-run]
"""
import asyncio
import os
import sys
import shutil
import yaml
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.distiller import compute_tier
from core.registry import Registry

DATA_DIR = os.getenv("DATA_DIR", "data")
REGISTRY_YAML = os.path.join(DATA_DIR, "registry.yaml")
CURATED_DIR = os.path.join(DATA_DIR, "curated")


def main():
    dry_run = "--dry-run" in sys.argv

    # Step 1: Backup
    backup_path = f"{REGISTRY_YAML}.pre-tiers"
    if not os.path.exists(backup_path):
        shutil.copy2(REGISTRY_YAML, backup_path)
        print(f"Backup: {backup_path}")
    else:
        print(f"Backup already exists: {backup_path}")

    # Step 2: Load registry
    registry = Registry(yaml_path=REGISTRY_YAML)
    projects = registry.list_all()
    print(f"Found {len(projects)} projects")

    tier_counts = {1: [], 2: [], 3: []}

    for name in projects:
        entry = registry.get(name)
        if not entry:
            continue

        curated_dir = os.path.join(CURATED_DIR, name)
        tier = compute_tier(name, curated_dir, entry)
        feature_count = len(list(Path(curated_dir).glob("*.md"))) if Path(curated_dir).exists() else 0

        # Check if project already has NLM notes (nlm_baseline)
        has_nlm = entry.get("status") in ("synced", "validated") and entry.get("nlm_source_count", 0) > 0

        tier_counts[tier].append(name)
        print(f"  {name}: tier={tier} features={feature_count} nlm_baseline={has_nlm}")

        if not dry_run:
            registry.update_fields(
                name,
                computed_tier=tier,
                nlm_baseline=has_nlm,
            )

    # Step 3: Migrate Qdrant payloads (add distill_source to existing nlm_notes)
    if not dry_run:
        print("\n=== Migrating Qdrant nlm_notes payloads ===")
        try:
            from core.indexer import _qdrant_request, COLLECTION_NLM_NOTES
            migrated = 0
            offset = None  # Qdrant scroll pagination
            while True:
                scroll_body = {"limit": 100, "with_payload": True}
                if offset is not None:
                    scroll_body["offset"] = offset
                result = _qdrant_request(
                    f"/collections/{COLLECTION_NLM_NOTES}/points/scroll",
                    data=scroll_body,
                )
                points = result.get("result", {}).get("points", [])
                next_offset = result.get("result", {}).get("next_page_offset")

                for point in points:
                    payload = point.get("payload", {})
                    if payload.get("from_nlm") and not payload.get("distill_source"):
                        _qdrant_request(
                            f"/collections/{COLLECTION_NLM_NOTES}/points/payload",
                            data={
                                "points": [point["id"]],
                                "payload": {"distill_source": "nlm", "canonical": True},
                            },
                            method="PUT",
                        )
                        migrated += 1

                if not next_offset or not points:
                    break
                offset = next_offset

            print(f"  Migrated {migrated} points (added distill_source='nlm')")
        except Exception as e:
            print(f"  WARNING: Qdrant migration failed: {e}")
            print(f"  Old payloads will still work via from_nlm fallback in query.py")

    print(f"\nTier distribution:")
    print(f"  Tier 1 (NLM):    {len(tier_counts[1])} — {', '.join(tier_counts[1]) or 'none'}")
    print(f"  Tier 2 (LLM):    {len(tier_counts[2])} — {', '.join(tier_counts[2]) or 'none'}")
    print(f"  Tier 3 (Direct): {len(tier_counts[3])} — {', '.join(tier_counts[3]) or 'none'}")

    if dry_run:
        print("\n[DRY RUN] No changes made.")
    else:
        print(f"\nRegistry updated. Tier 2/3 projects ready for distill loop.")
        print(f"Run: curl -X POST http://localhost:8095/curate/{{project}} for each Tier 2/3 project.")


if __name__ == "__main__":
    main()
