"""Shared provider configuration for LLM, speech and embedding services."""

import os
from pathlib import Path

import yaml


def configuration():
    with Path(os.getenv("PROVIDERS_CONFIG", "config/providers.yaml")).open() as source:
        return yaml.safe_load(source)


def llm_profiles():
    return configuration().get("llm", {}).get("profiles", {})
