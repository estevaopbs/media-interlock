"""One-shot Reconciler entry point."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from .. import __version__
from ..adapters.radarr import RadarrAdapter
from ..adapters.sonarr import SonarrAdapter
from ..cli import render_result
from ..config import ConfigError, load_config
from .fence_client import UnixFenceClient
from .model import AttemptPolicy, SearchIntent
from .service import ReconcilerService, ReconcilerSource
from .store import ReconcilerStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media-interlock-reconciler")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--source", choices=("radarr", "sonarr"))
    parser.add_argument("--entity")
    parser.add_argument("--checkpoint")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.version:
        print(__version__)
        return 0
    if arguments.config is None:
        parser.error("--config is required")
    if arguments.check_config:
        try:
            load_config(arguments.config)
        except ConfigError as exc:
            print(render_result("invalid_contract", str(exc), as_json=arguments.json))
            return 2
        print(render_result("ok", "configuration valid", as_json=arguments.json))
        return 0
    if arguments.source is None or arguments.entity is None or arguments.checkpoint is None:
        parser.error("--source, --entity, and --checkpoint are required")
    try:
        config = load_config(arguments.config)
        if config.reconciler is None or config.fence is None:
            raise ConfigError("Reconciler requires reconciler and fence components")
        adapters = {}
        for source, adapter_class in (("radarr", RadarrAdapter), ("sonarr", SonarrAdapter)):
            arr_config = config.adapters.get(source)
            profile = config.reconciler.sources.get(source)
            if arr_config is not None and profile is not None:
                adapters[source] = adapter_class(arr_config.base_url, arr_config.secrets["api_key"], staging_root=None)
        if arguments.source not in adapters:
            raise ConfigError(f"Reconciler requires a configured {arguments.source} adapter")
        store = ReconcilerStore.open(config.reconciler.state_dir)
    except (ConfigError, KeyError, OSError) as exc:
        print(render_result("invalid_contract", str(exc), as_json=arguments.json))
        return 2
    try:
        state = store.load()
        service = ReconcilerService(state, store, adapters, UnixFenceClient(config.fence.socket_path), {name: ReconcilerSource(profile.category, profile.download_client_id) for name, profile in config.reconciler.sources.items()})
        service.recover(now=0)
        policy_config = config.reconciler.movie if arguments.source == "radarr" else config.reconciler.episode
        policy = AttemptPolicy(policy_config.cooldown_seconds, policy_config.max_attempts)
        if not state.eligible(arguments.source, arguments.entity, policy, now=0, force=arguments.force):
            result = "pending"
        else:
            intent = SearchIntent(str(uuid.uuid4()), arguments.source, arguments.entity, arguments.force, arguments.checkpoint)
            result = service.execute(intent, now=0)
    except (OSError, ValueError):
        result = "unavailable"
    finally:
        store.close()
    status = "ok" if result == "bound" else "inhibited" if result == "inhibited" else "unavailable" if result == "unavailable" else "ok"
    print(render_result(status, result, as_json=arguments.json))
    return 0 if result in {"bound", "pending"} else 1
