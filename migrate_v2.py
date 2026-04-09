"""One-shot migration: Erudito v2 scan_state.json → v3 registry.yaml.

Usage: python migrate_v2.py [--dry-run]

Reads data/scan_state.json, converts to registry.yaml format.
Shows preview diff and requires confirmation before writing.
"""
import json
import os
import sys
import yaml

# Alias map for ambiguous paths (basename collision or non-obvious names)
ALIAS_MAP = {
    "profiles": "jasper-profiles",
    "claude_contracts": "claude-contracts",
    "opencode_contracts": "opencode-contracts",
}

# Node mapping
NODE_MAP = {
    "ai-lab": "hanzo",
    "desarrollos_openclaw": "hanzo",
}

# Repo mapping (known repos)
REPO_MAP = {
    "infra-mcp": "r0calex/infra-mcp",
    "devops-agent": "r0calex/devops-agent",
    "mesh-monitor": "r0calex/mesh-monitor",
    "erudito": "r0calex/erudito",
    "event-bus": "r0calex/event-bus",
    "node-reporter": "r0calex/node-reporter",
    "qdrant-mcp": "r0calex/qdrant-mcp",
}


def derive_name(path: str) -> str:
    """Derive project name from path, using alias map for ambiguous cases."""
    basename = os.path.basename(path.rstrip("/"))
    return ALIAS_MAP.get(basename, basename)


def derive_node(path: str) -> str:
    """Derive node from path."""
    for key, node in NODE_MAP.items():
        if key in path:
            return node
    return "hanzo"


def migrate(dry_run: bool = False):
    state_file = "data/scan_state.json"
    output_file = "data/registry.yaml"

    if not os.path.exists(state_file):
        print(f"Error: {state_file} not found")
        sys.exit(1)

    with open(state_file, "r") as f:
        v2_state = json.load(f)

    registry_data = {"version": 1, "projects": {}}

    print("=== Migration Preview ===\n")
    for path, state in v2_state.items():
        name = derive_name(path)
        node = derive_node(path)
        repo = REPO_MAP.get(name, "")

        entry = {
            "path": path,
            "node": node,
            "repo": repo,
            "notebook_id": None,
            "last_hash": state.get("last_commit"),
            "last_sync": state.get("last_scan"),
            "status": "synced",
            "coverage": 0.0,
            "auto_enriched": False,
            "doc_count": 0,
            "last_nlm_session": None,
            "nlm_source_count": 0,
        }
        registry_data["projects"][name] = entry
        print(f"  {path}")
        print(f"    → name: {name}, node: {node}, repo: {repo}")
        print(f"    → last_hash: {state.get('last_commit', 'N/A')[:8]}...")
        print()

    print(f"Total projects: {len(registry_data['projects'])}")
    print(f"Output: {output_file}")
    print()

    if dry_run:
        print("--- Dry run, not writing ---")
        print(yaml.dump(registry_data, default_flow_style=False))
        return

    confirm = input("Write registry.yaml? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        return

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w") as f:
        yaml.dump(registry_data, f, default_flow_style=False, sort_keys=False)
    print(f"Written to {output_file}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    migrate(dry_run=dry)
