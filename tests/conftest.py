"""Configuração comum dos testes."""

import pytest


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    # Nos testes, a nova tentativa após uma falha acontece sem espera.
    monkeypatch.setenv("RETRY_DELAY_SECONDS", "0")
