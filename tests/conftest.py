import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "network: requires Qdrant/Ollama")
    config.addinivalue_line("markers", "integration: requires running Erudito service")
    config.addinivalue_line("markers", "redis: requires Redis")
