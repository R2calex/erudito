"""Seed the project catalog with known AI-Lab projects.

Run: python seed_catalog.py
"""

from catalog import upsert_project

PROJECTS = [
    {
        "name": "infra-mcp",
        "description": "Keystone Infrastructure Mesh — leader/satellite replication, governance engine, reconciliation, placement engine, node awareness",
        "type": "infrastructure",
        "stack": ["python", "fastapi", "docker", "qdrant"],
        "status": "active",
        "node_primary": "hanzo",
        "port": 8082,
        "github": "https://github.com/R2calex/infra-mcp",
        "tags": ["keystone", "mesh", "mcp", "governance", "orchestration"],
        "version": "0.7.0",
    },
    {
        "name": "devops-agent",
        "description": "Autonomous DevOps agent — LLM-powered infrastructure task execution with governance pre-flight checks",
        "type": "infrastructure",
        "stack": ["python", "fastapi", "docker", "litellm"],
        "status": "active",
        "node_primary": "hanzo",
        "port": 8090,
        "github": "https://github.com/R2calex/devops-agent",
        "tags": ["devops", "automation", "llm", "docker"],
        "version": "0.2.0",
    },
    {
        "name": "mesh-monitor",
        "description": "Health monitoring with auto-remediation, Telegram alerts, Redis event bus, and n8n webhook integration",
        "type": "infrastructure",
        "stack": ["python", "redis", "telegram"],
        "status": "active",
        "node_primary": "hanzo",
        "github": "https://github.com/R2calex/mesh-monitor",
        "tags": ["monitoring", "alerting", "auto-remediation", "telegram"],
    },
    {
        "name": "event-bus",
        "description": "Redis Streams event bus for push-based mesh events — service state changes, task completions",
        "type": "infrastructure",
        "stack": ["python", "redis"],
        "status": "active",
        "node_primary": "hanzo",
        "github": "https://github.com/R2calex/event-bus",
        "tags": ["events", "redis", "streams", "pubsub"],
    },
    {
        "name": "qdrant-mcp",
        "description": "MCP server for semantic search over Qdrant — RAG knowledge base access for LLM tools",
        "type": "infrastructure",
        "stack": ["python", "fastapi", "qdrant", "ollama"],
        "status": "active",
        "node_primary": "hanzo",
        "port": 8083,
        "github": "https://github.com/R2calex/qdrant-mcp",
        "tags": ["rag", "mcp", "qdrant", "search", "embeddings"],
    },
    {
        "name": "node-reporter",
        "description": "Node hardware profiling and heartbeat reporter for Keystone Node Awareness",
        "type": "infrastructure",
        "stack": ["python"],
        "status": "active",
        "node_primary": "hanzo",
        "github": "https://github.com/R2calex/node-reporter",
        "tags": ["hardware", "monitoring", "heartbeat", "node-awareness"],
    },
    {
        "name": "erudito",
        "description": "Knowledge Orchestration Agent — intelligent RAG curation with LLM analysis, anti-poison, and product catalog",
        "type": "infrastructure",
        "stack": ["python", "fastapi", "qdrant", "ollama", "litellm"],
        "status": "active",
        "node_primary": "hanzo",
        "port": 8095,
        "tags": ["knowledge", "rag", "curation", "llm", "catalog"],
    },
    {
        "name": "openclaw",
        "description": "AI agent gateway with Telegram bot integration — Kubo's primary user interface",
        "type": "infrastructure",
        "stack": ["nodejs", "telegram"],
        "status": "active",
        "node_primary": "kubo",
        "port": 18789,
        "tags": ["agent", "telegram", "gateway", "kubo"],
    },
]

if __name__ == "__main__":
    for p in PROJECTS:
        pid = upsert_project(p)
        print(f"  {p['name']:20s} -> {pid}")
    print(f"\nSeeded {len(PROJECTS)} projects into catalog.")
