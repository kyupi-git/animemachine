"""Signed-in-release endpoint registry; user settings may only extend it."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TRUST = {"official", "established_cdn", "community_mirror", "user_defined"}


@dataclass(frozen=True, slots=True)
class Endpoint:
    id: str
    service: str
    base_url: str
    trust: str
    credentials: str = "never"
    capabilities: tuple[str, ...] = ()
    enabled: bool = True

    @classmethod
    def from_dict(cls, value: dict) -> "Endpoint":
        trust = str(value.get("trust", "user_defined"))
        if trust not in TRUST:
            raise ValueError(f"invalid endpoint trust: {trust}")
        return cls(str(value["id"]), str(value["service"]), str(value["baseUrl"]).rstrip("/"),
                   trust, str(value.get("credentials", "never")),
                   tuple(map(str, value.get("capabilities", ()))), bool(value.get("enabled", True)))


def load(path: Path | None = None, additions: Iterable[dict] = ()) -> tuple[list[Endpoint], dict]:
    source = path or Path(__file__).resolve().parents[1] / "resources" / "network-sources.json"
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if payload.get("schemaVersion") != 1:
        raise ValueError("unsupported network source registry")
    by_id = {item.id: item for item in map(Endpoint.from_dict, payload.get("sources", []))}
    for raw in additions:
        endpoint = Endpoint.from_dict(raw)
        if endpoint.trust != "user_defined":
            raise ValueError("user endpoint additions must use user_defined trust")
        by_id[endpoint.id] = endpoint
    return list(by_id.values()), payload


def for_service(service: str, *, capability: str | None = None, path: Path | None = None) -> list[Endpoint]:
    endpoints, _ = load(path)
    return [item for item in endpoints if item.enabled and item.service == service
            and (not capability or capability in item.capabilities)]


def may_send_credentials(endpoint: Endpoint, explicitly_trusted: set[str] | None = None) -> bool:
    return endpoint.trust == "official" or (endpoint.trust == "user_defined" and endpoint.id in (explicitly_trusted or set()))
