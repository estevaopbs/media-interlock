"""Publisher daemon entry point; deployment owns supervision and scheduling."""

from __future__ import annotations

import argparse
import asyncio
import socket
import stat
import uuid
from pathlib import Path

from ..cli import render_result
from ..config import ConfigError, ProductConfig, load_config
from ..contracts import Envelope, StatusCode, status_response
from ..adapters.radarr import RadarrAdapter
from ..adapters.sonarr import SonarrAdapter
from ..adapters.jellyfin import JellyfinAdapter
from ..adapters.bazarr import BazarrAdapter
from ..adapters.seerr import SeerrAdapter
from .generation import AssetGenerationPublisher, CanonicalWriterLock
from .daemon import PublisherDaemon
from .model import PublisherState
from .observability import PublisherObservability
from .service import AssetPublisherWorkProcessor, PathTranslation, PublisherService
from .filesystem import CandidateVerifier
from .store import PublisherStore


def _runtime(config: ProductConfig) -> tuple[PublisherStore, PublisherDaemon, CanonicalWriterLock | None]:
    if config.publisher is None:
        raise ConfigError("configuration has no publisher component")
    try:
        radarr_config = config.adapters["radarr"]
        sonarr_config = config.adapters["sonarr"]
        jellyfin_config = config.adapters["jellyfin"]
    except KeyError as exc:
        raise ConfigError("Publisher requires Radarr, Sonarr, and Jellyfin adapters") from exc
    store = PublisherStore.open(config.publisher.state_dir)
    state = store.load()
    roots_ready = all(path.exists() and path.is_dir() and not path.is_symlink() for path in (config.publisher.staging_root, config.publisher.canonical_root))
    writer_lock = CanonicalWriterLock.acquire(config.publisher.canonical_root) if roots_ready else None
    service = PublisherService(state, store)
    generations = AssetGenerationPublisher(config.publisher.staging_root, config.publisher.canonical_root, namespace=config.publisher.namespace)
    correlations = {
        "radarr": RadarrAdapter(radarr_config.base_url, radarr_config.secrets["api_key"], staging_root=config.publisher.staging_root),
        "sonarr": SonarrAdapter(sonarr_config.base_url, sonarr_config.secrets["api_key"], staging_root=config.publisher.staging_root),
    }
    catalog = JellyfinAdapter(jellyfin_config.base_url, jellyfin_config.secrets["api_key"])
    optional = [
        BazarrAdapter(adapter.base_url, adapter.secrets["api_key"])
        for adapter in (config.adapters.get("bazarr"),)
        if adapter is not None
    ] + [
        SeerrAdapter(adapter.base_url, adapter.secrets["api_key"])
        for adapter in (config.adapters.get("seerr"),)
        if adapter is not None
    ]
    translation = PathTranslation(config.publisher.canonical_root, config.publisher.namespace, config.publisher.jellyfin_path_prefix)
    readiness = lambda: roots_ready and catalog.ready() and all(adapter.ready() for adapter in optional)
    if readiness():
        service.recover_assets(generations, catalog, translation, library_id=config.publisher.jellyfin_library_id, correlations=correlations, inspection=CandidateVerifier(config.publisher.staging_root))
        service.garbage_collect_assets(generations)
    processor = AssetPublisherWorkProcessor(
        service,
        correlations,
        CandidateVerifier(config.publisher.staging_root),
        generations,
        catalog,
        translation,
        library_id=config.publisher.jellyfin_library_id,
    )
    def retry_pending() -> None:
        service.recover_assets(generations, catalog, translation, library_id=config.publisher.jellyfin_library_id, correlations=correlations, inspection=CandidateVerifier(config.publisher.staging_root))
        service.garbage_collect_assets(generations)

    daemon = PublisherDaemon(
        service,
        PublisherObservability(state),
        readiness=readiness,
        process=processor,
        retry=retry_pending,
    )
    return store, daemon, writer_lock


def _component_ready(socket_path: Path) -> bool:
    request = status_response(str(uuid.uuid4()), StatusCode.OK, "readiness")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(str(socket_path))
            client.sendall(request.encode())
            frame = bytearray()
            while len(frame) < 64 * 1024:
                chunk = client.recv(1)
                if not chunk:
                    break
                frame.extend(chunk)
                if chunk == b"\n":
                    break
            response = Envelope.decode(bytes(frame))
    except (OSError, ValueError):
        return False
    return response.operation_id == request.operation_id and response.kind == "status" and response.body.get("code") == StatusCode.OK.value


async def _serve(socket_path: Path, daemon: PublisherDaemon) -> None:
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        mode = socket_path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(mode):
            raise OSError("Publisher socket path exists and is not a socket")
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.close()
            await writer.wait_closed()
        except (ConnectionRefusedError, FileNotFoundError):
            socket_path.unlink()
        else:
            raise OSError("Publisher socket is already active")
    server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
    async with server:
        async with asyncio.TaskGroup() as group:
            group.create_task(server.serve_forever())
            group.create_task(daemon.retry_loop())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media-interlock-publisher")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        if arguments.status:
            ready = _component_ready(config.publisher.socket_path) if config.publisher is not None else False
            print(render_result("ok" if ready else "inhibited", "ready" if ready else "unavailable", as_json=arguments.json))
            return 0 if ready else 1
        store, daemon, writer_lock = _runtime(config)
    except (ConfigError, OSError) as exc:
        print(render_result("invalid_contract", str(exc), as_json=arguments.json))
        return 2
    try:
        assert config.publisher is not None
        asyncio.run(_serve(config.publisher.socket_path, daemon))
    except OSError as exc:
        print(render_result("unavailable", str(exc), as_json=arguments.json))
        return 1
    finally:
        if writer_lock is not None:
            writer_lock.close()
        store.close()
    return 0
