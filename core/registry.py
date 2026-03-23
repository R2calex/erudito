"""Triple map registry: project ↔ notebook ↔ repo.

Dual-write to YAML (human-readable, git-versionable) and Redis (crash recovery).
Version counter for conflict resolution on startup.
"""
import os
import logging
import threading
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("erudito.registry")

_DEFAULT_ENTRY = {
    "path": None,
    "node": None,
    "repo": None,
    "notebook_id": None,
    "last_hash": None,
    "last_sync": None,
    "status": "pending",
    "coverage": 0.0,
    "auto_enriched": False,
    "doc_count": 0,
    "last_nlm_session": None,
    "nlm_source_count": 0,
    "curation_status": "uncurated",
    "curated_at": None,
    "curated_files": 0,
    "nlm_consecutive_failures": 0,
}


class Registry:
    """Project registry with YAML + Redis dual-write."""

    def __init__(self, yaml_path: str = "data/registry.yaml", redis_url: str | None = None):
        self.yaml_path = yaml_path
        self._redis = None
        self._redis_url = redis_url
        self._data: dict = {"version": 0, "projects": {}}
        self._lock = threading.Lock()
        self._load()

    @property
    def version(self) -> int:
        return self._data["version"]

    def _load(self):
        """Load and reconcile YAML + Redis state."""
        yaml_data = self._load_yaml()
        redis_data = self._load_redis()

        if yaml_data and not redis_data:
            self._data = yaml_data
            self._save_redis()
        elif redis_data and not yaml_data:
            self._data = redis_data
            self._save_yaml()
        elif yaml_data and redis_data:
            # Highest version wins
            if redis_data.get("version", 0) > yaml_data.get("version", 0):
                self._data = redis_data
                self._save_yaml()
            else:
                self._data = yaml_data
                self._save_redis()
        # else: both empty, use defaults

    def _load_yaml(self) -> dict | None:
        try:
            with open(self.yaml_path, "r") as f:
                data = yaml.safe_load(f)
                if data and "projects" in data:
                    return data
        except (FileNotFoundError, yaml.YAMLError):
            pass
        return None

    def _save_yaml(self):
        Path(self.yaml_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.yaml_path, "w") as f:
            yaml.dump(self._data, f, default_flow_style=False, sort_keys=False)

    def _load_redis(self) -> dict | None:
        r = self._get_redis()
        if not r:
            return None
        try:
            import json
            raw = r.get("erudito:registry")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning(f"Redis load failed: {e}")
        return None

    def _save_redis(self):
        r = self._get_redis()
        if not r:
            return
        try:
            import json
            r.set("erudito:registry", json.dumps(self._data, default=str))
        except Exception as e:
            logger.warning(f"Redis save failed: {e}")

    def _get_redis(self):
        if self._redis_url is None:
            return None
        if self._redis is None:
            try:
                import redis as redis_lib
                self._redis = redis_lib.from_url(self._redis_url)
                self._redis.ping()
            except Exception as e:
                logger.warning(f"Redis connection failed: {e}")
                self._redis = None
        return self._redis

    def _persist(self):
        """Dual-write: YAML first, then Redis. Increment version. Thread-safe."""
        with self._lock:
            self._data["version"] += 1
            self._save_yaml()
            self._save_redis()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def register(self, name: str, path: str, node: str, repo: str):
        """Register a new project. Raises ValueError on duplicate name or path."""
        projects = self._data["projects"]
        if name in projects:
            raise ValueError(f"Project '{name}' already registered")
        for existing_name, entry in projects.items():
            if entry["path"] == path:
                raise ValueError(f"Path '{path}' already registered under '{existing_name}'")
        entry = dict(_DEFAULT_ENTRY)
        entry["path"] = path
        entry["node"] = node
        entry["repo"] = repo
        projects[name] = entry
        self._persist()

    def get(self, name: str) -> dict | None:
        entry = self._data["projects"].get(name)
        if entry:
            # Merge with defaults so old entries get new fields
            merged = dict(_DEFAULT_ENTRY)
            merged.update(entry)
            return merged
        return None

    def update_sync(
        self,
        name: str,
        new_hash: str,
        notebook_id: str | None = None,
        doc_count: int | None = None,
    ):
        project = self._data["projects"].get(name)
        if not project:
            raise KeyError(f"Project '{name}' not found")
        project["last_hash"] = new_hash
        project["last_sync"] = self._now()
        project["status"] = "synced"
        if notebook_id is not None:
            project["notebook_id"] = notebook_id
        if doc_count is not None:
            project["doc_count"] = doc_count
        self._persist()

    def mark_validated(self, name: str, coverage: float = 0.0):
        project = self._data["projects"].get(name)
        if not project:
            raise KeyError(f"Project '{name}' not found")
        project["status"] = "validated"
        project["coverage"] = coverage
        project["last_nlm_session"] = self._now()
        self._persist()

    def mark_stale(self, name: str):
        project = self._data["projects"].get(name)
        if not project:
            raise KeyError(f"Project '{name}' not found")
        project["status"] = "stale"
        self._persist()

    def update_fields(self, name: str, **fields):
        """Thread-safe update of arbitrary fields on a project entry."""
        with self._lock:
            project = self._data["projects"].get(name)
            if not project:
                raise KeyError(f"Project '{name}' not found")
            project.update(fields)
            self._data["version"] += 1
            self._save_yaml()
            self._save_redis()

    def register_remote(self, name: str, path: str, node: str):
        """Register a remote project (from satellite node). No path/git validation."""
        projects = self._data["projects"]
        if name not in projects:
            entry = dict(_DEFAULT_ENTRY)
            entry["path"] = path
            entry["node"] = node
            projects[name] = entry
            self._persist()

    def list_all(self) -> list[str]:
        return list(self._data["projects"].keys())

    def list_by_status(self, status: str) -> list[str]:
        return [
            name for name, entry in self._data["projects"].items()
            if entry["status"] == status
        ]

    def summary(self) -> dict:
        projects = self._data["projects"]
        total = len(projects)
        by_status = {}
        curation_curated = 0
        curation_pending = 0
        for entry in projects.values():
            s = entry["status"]
            by_status[s] = by_status.get(s, 0) + 1
            cs = entry.get("curation_status", "uncurated")
            if cs == "curated":
                curation_curated += 1
            elif cs in ("uncurated", "stale"):
                curation_pending += 1
        validated = by_status.get("validated", 0)
        synced = by_status.get("synced", 0)
        return {
            "total": total,
            "validated": validated,
            "synced": synced,
            "stale": by_status.get("stale", 0),
            "pending": by_status.get("pending", 0),
            "coverage_pct": round(validated / total * 100, 1) if total else 0.0,
            "coverage_partial_pct": round((validated + synced) / total * 100, 1) if total else 0.0,
            "curation_pct": round(curation_curated / total * 100, 1) if total else 0.0,
            "curation_pending": curation_pending,
        }
