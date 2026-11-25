# -*- coding: utf-8 -*-
"""Host configuration and resource limits for enterprise onboarding."""

import json
import math
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from ._business import definition_digest

MAX_INPUT = 8 * 1024 * 1024
PUBLICATION_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class EnterpriseLimits:
    """Defaults preserve the previous onboarding and response bounds."""

    max_input_bytes: int = MAX_INPUT
    max_source_path_chars: int = 4096
    max_discovery_candidates: int = 1800
    max_operations: int = 1500
    summary_chars: int = 160
    evidence_chars: int = 200
    evidence_per_record: int = 2
    diagnostic_count: int = 8
    page_size: int = 20
    concurrent_probes: int = 4
    max_cached_credentials: int = 256
    publication_poll_seconds: float = PUBLICATION_POLL_SECONDS

    def __post_init__(self) -> None:
        for definition in fields(self):
            value = getattr(self, definition.name)
            if definition.name == "publication_poll_seconds":
                valid = (
                    type(value) in (int, float)
                    and 0 < value <= 5
                    and math.isfinite(value)
                )
            else:
                valid = type(value) is int and 0 < value <= definition.default
            if not valid:
                raise ValueError(
                    f"Invalid enterprise limit {definition.name}."
                )

    def check_input(self, source: str) -> None:
        if not source or len(source.encode()) > self.max_input_bytes:
            raise ValueError(
                f"Provide an OpenAPI definition or source path up to {self.max_input_bytes} bytes."
            )


@dataclass(frozen=True)
class EnterpriseReadAPI:
    """A deployment-approved API and sample arguments for verified reads."""

    document: Path
    base_url: str
    samples: dict[str, dict]
    token: str | None = field(default=None, repr=False)
    success: dict | None = None

    @property
    def key(self) -> str:
        return definition_digest([str(self.document), self.base_url])


def load_enterprise_config() -> dict:
    """Load optional host-managed source and read endpoint grants."""
    config_path = os.environ.get("AGENTSCOPE_ENTERPRISE_BUSINESS_CONFIG")
    config = json.loads(Path(config_path).read_text()) if config_path else {}
    return {
        "directory": Path(
            config.get("directory", "~/.agentscope/enterprise-business")
        ),
        "limits": EnterpriseLimits(**config.get("limits", {})),
        "local_user": config.get("local_user"),
        "source_roots": {
            user: [Path(root) for root in roots]
            for user, roots in config.get("source_roots", {}).items()
        },
        "api_documents": {
            user: {url: Path(path) for url, path in docs.items()}
            for user, docs in config.get("api_documents", {}).items()
        },
        "read_apis": {
            user: [
                EnterpriseReadAPI(
                    Path(api["document"]),
                    api["base_url"],
                    api["samples"],
                    os.environ[api["token_env"]]
                    if api.get("token_env")
                    else None,
                    api.get("success"),
                )
                for api in apis
            ]
            for user, apis in config.get("read_apis", {}).items()
        },
    }
