"""Pytest configuration for Erudito tests."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-network", action="store_true", default=False,
        help="Run tests that require Qdrant + Ollama",
    )
    parser.addoption(
        "--run-integration", action="store_true", default=False,
        help="Run tests that require Erudito service running",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "network: requires Qdrant + Ollama")
    config.addinivalue_line("markers", "integration: requires Erudito service on :8095")


def pytest_collection_modifyitems(config, items):
    run_network = config.getoption("--run-network")
    run_integration = config.getoption("--run-integration")

    skip_network = pytest.mark.skip(reason="Need --run-network to run")
    skip_integration = pytest.mark.skip(reason="Need --run-integration to run")

    for item in items:
        if "network" in item.keywords and not run_network:
            item.add_marker(skip_network)
        if "integration" in item.keywords and not run_integration:
            item.add_marker(skip_integration)
