"""HTTP client for sanitizer-hanzo with local regex fallback."""
import os
import re
import logging
import httpx

logger = logging.getLogger("erudito.sanitizer")

SANITIZER_URL = os.getenv("SANITIZER_URL", "http://localhost:8086")

POISON_PATTERNS = [
    re.compile(r"password\s*[:=]\s*['\"]?[^\s'\"]{8,}", re.IGNORECASE),
    re.compile(r"api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"token\s*[:=]\s*['\"]?[a-zA-Z0-9._-]{20,}", re.IGNORECASE),
    re.compile(r"postgresql://[^:]+:[^@]+@"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"),
    re.compile(r"BEGIN\s+(RSA|DSA|EC|OPENSSH)\s+PRIVATE\s+KEY"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
]


def _local_poison_check(text: str) -> bool:
    return any(p.search(text) for p in POISON_PATTERNS)


def _local_mask(text: str) -> tuple[str, int]:
    count = 0
    result = text
    for p in POISON_PATTERNS:
        matches = p.findall(result)
        if matches:
            count += len(matches)
            result = p.sub("[MASKED]", result)
    return result, count


async def sanitize_text(
    text: str,
    sanitizer_url: str | None = None,
    context: str | None = None,
) -> dict:
    url = sanitizer_url or SANITIZER_URL
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{url}/sanitize",
                json={"text": text, "context": context},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "sanitized": data["sanitized"],
                "masked_count": data.get("masked_count", 0),
                "patterns_matched": data.get("patterns_matched", []),
                "fallback": False,
            }
    except Exception as e:
        logger.warning(f"Sanitizer unavailable ({e}), using local fallback")
        masked, count = _local_mask(text)
        return {
            "sanitized": masked,
            "masked_count": count,
            "patterns_matched": [],
            "fallback": True,
        }
