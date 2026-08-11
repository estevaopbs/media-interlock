"""One-shot and product-owned scheduled Reconciler entry point."""

from __future__ import annotations

import argparse
import signal
import threading
import time
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
from .scheduler import UpgradeScheduler
from .store import ReconcilerStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media-interlock-reconciler")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--source", choices=("radarr", "sonarr"))
    parser.add_argument("--entity")
    parser.add_argument("--checkpoint")
    parser.add_argument("--force", action="store_true")
    automatic_modes = parser.add_mutually_exclusive_group()
    automatic_modes.add_argument("--run-due", action="store_true")
    automatic_modes.add_argument("--daemon", action="store_true")
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
    automatic = arguments.run_due or arguments.daemon
    manual = arguments.source is not None or arguments.entity is not None or arguments.checkpoint is not None
    if automatic and manual:
        parser.error("automatic and explicit entity modes are mutually exclusive")
    if not automatic and (arguments.source is None or arguments.entity is None or arguments.checkpoint is None):
        parser.error("--source, --entity, and --checkpoint are required outside automatic mode")
    try:
        config = load_config(arguments.config)
        if config.reconciler is None or config.fence is None:
            raise ConfigError("Reconciler requires reconciler and fence components")
        adapters = {}
        for source, adapter_class in (("radarr", RadarrAdapter), ("sonarr", SonarrAdapter)):
            arr_config = config.adapters.get(source)
            profile = config.reconciler.sources.get(source)
            if arr_config is not None and profile is not None:
                adapters[source] = adapter_class(
                    arr_config.base_url,
                    arr_config.secrets["api_key"],
                    staging_root=None,
                    timeout_seconds=90,
                )
        if not automatic and arguments.source not in adapters:
            raise ConfigError(f"Reconciler requires a configured {arguments.source} adapter")
        store = ReconcilerStore.open(config.reconciler.state_dir)
    except (ConfigError, KeyError, OSError) as exc:
        print(render_result("invalid_contract", str(exc), as_json=arguments.json))
        return 2
    try:
        state = store.load()
        service = ReconcilerService(state, store, adapters, UnixFenceClient(config.fence.socket_path), {name: ReconcilerSource(profile.category, profile.download_client_id) for name, profile in config.reconciler.sources.items()})
        if automatic:
            schedule = store.load_schedule()
            scheduler = UpgradeScheduler(
                schedule,
                state,
                store.save_schedule,
                adapters,
                {"radarr": config.reconciler.movie, "sonarr": config.reconciler.episode},
                service,
            )
            stop = threading.Event()
            if arguments.daemon:
                signal.signal(signal.SIGTERM, lambda *_: stop.set())
                signal.signal(signal.SIGINT, lambda *_: stop.set())
            while True:
                run = scheduler.run(now=int(time.time()))
                result = (
                    f"searched={run.searched},grabbed={run.grabbed},"
                    f"no_candidate={run.no_candidate},unavailable={run.unavailable},pending={run.pending}"
                )
                status = "unavailable" if run.unavailable else "ok"
                print(render_result(status, result, as_json=arguments.json), flush=True)
                if not arguments.daemon:
                    return 0 if not run.unavailable else 1
                if stop.wait(config.reconciler.poll_interval_seconds):
                    return 0
        now = int(time.time())
        service.recover(now=now)
        assert arguments.source is not None and arguments.entity is not None and arguments.checkpoint is not None
        policy_config = config.reconciler.movie if arguments.source == "radarr" else config.reconciler.episode
        policy = AttemptPolicy(policy_config.cooldown_seconds, policy_config.max_attempts)
        if not state.eligible(arguments.source, arguments.entity, policy, now=now, force=arguments.force):
            result = "pending"
        else:
            intent = SearchIntent(str(uuid.uuid4()), arguments.source, arguments.entity, arguments.force, arguments.checkpoint)
            result = service.execute(intent, now=now)
    except (OSError, ValueError):
        result = "unavailable"
    finally:
        store.close()
    status = "ok" if result == "bound" else "inhibited" if result == "inhibited" else "unavailable" if result == "unavailable" else "ok"
    print(render_result(status, result, as_json=arguments.json))
    return 0 if result in {"bound", "pending"} else 1
