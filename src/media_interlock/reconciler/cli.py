"""One-shot Reconciler entry point."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from ..adapters.radarr import RadarrAdapter
from ..adapters.sonarr import SonarrAdapter
from ..cli import render_result
from ..config import ConfigError, load_config
from .fence_client import UnixFenceClient
from .model import AttemptPolicy, SearchIntent
from .service import ReconcilerService
from .store import ReconcilerStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media-interlock-reconciler")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--source", choices=("radarr", "sonarr"), required=True)
    parser.add_argument("--entity", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        if config.reconciler is None or config.fence is None:
            raise ConfigError("Reconciler requires reconciler and fence components")
        arr_config = config.adapters[arguments.source]
        adapter_class = RadarrAdapter if arguments.source == "radarr" else SonarrAdapter
        adapter = adapter_class(arr_config.base_url, arr_config.secrets["api_key"], staging_root=config.fence.staging_root)
        store = ReconcilerStore.open(config.reconciler.state_dir)
    except (ConfigError, KeyError, OSError) as exc:
        print(render_result("invalid_contract", str(exc), as_json=arguments.json))
        return 2
    try:
        state = store.load()
        policy_config = config.reconciler.movie if arguments.source == "radarr" else config.reconciler.episode
        policy = AttemptPolicy(policy_config.cooldown_seconds, policy_config.max_attempts)
        if not state.eligible(arguments.source, arguments.entity, policy, now=0, force=arguments.force):
            result = "pending"
        else:
            intent = SearchIntent(str(uuid.uuid4()), arguments.source, arguments.entity, arguments.force, arguments.checkpoint)
            service = ReconcilerService(state, store, {arguments.source: adapter}, UnixFenceClient(config.fence.socket_path), config.fence.categories)
            result = service.execute(intent, now=0)
    except (OSError, ValueError):
        result = "unavailable"
    finally:
        store.close()
    status = "ok" if result == "bound" else "inhibited" if result == "inhibited" else "unavailable" if result == "unavailable" else "ok"
    print(render_result(status, result, as_json=arguments.json))
    return 0 if result in {"bound", "pending"} else 1
