"""Publisher daemon entry point; deployment owns supervision and scheduling."""

from __future__ import annotations

import argparse
import asyncio
import socket
import stat
import uuid
from pathlib import Path

from .. import __version__
from ..cli import render_result
from ..config import ConfigError, ProductConfig, load_config
from ..contracts import Envelope, StatusCode, status_response
from ..adapters.radarr import RadarrAdapter
from ..adapters.sonarr import SonarrAdapter
from ..adapters.jellyfin import JellyfinAdapter
from ..adapters.bazarr import BazarrAdapter
from ..adapters.seerr import SeerrAdapter
from ..reconciler.fence_client import UnixFenceClient
from .generation import AssetGenerationPublisher, CanonicalWriterLock
from .daemon import PublisherDaemon
from .model import PublisherState
from .observability import PublisherObservability
from .service import AssetPublisherWorkProcessor, PathTranslation, PublisherService, verified_bundle_from_manifest
from .filesystem import BundleVerifier
from .store import PublisherStore


def _runtime(config: ProductConfig) -> tuple[PublisherStore, PublisherDaemon, tuple[CanonicalWriterLock, ...]]:
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
    profiles = tuple(config.publisher.sources.values())
    roots_ready = all(path.exists() and path.is_dir() and not path.is_symlink() for profile in profiles for path in (profile.staging_root, profile.canonical_root))
    writer_locks = tuple(CanonicalWriterLock.acquire(profile.canonical_root) for profile in profiles) if roots_ready else ()
    service = PublisherService(state, store)
    correlations = {
        "radarr": RadarrAdapter(radarr_config.base_url, radarr_config.secrets["api_key"], staging_root=config.publisher.sources["radarr"].staging_root),
        "sonarr": SonarrAdapter(sonarr_config.base_url, sonarr_config.secrets["api_key"], staging_root=config.publisher.sources["sonarr"].staging_root),
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
    readiness = lambda: roots_ready and catalog.ready() and all(adapter.ready() for adapter in optional)
    processors = {
        profile.name: AssetPublisherWorkProcessor(
            service,
            {profile.name: correlations[profile.name]},
            BundleVerifier(
                profile.staging_root,
                settle_seconds=profile.bundle_settle_seconds,
                sidecar_extensions=profile.bundle_sidecar_extensions,
                required_languages=profile.bundle_required_languages,
                language_aliases=dict(profile.bundle_language_aliases),
                required_container_evidence=profile.bundle_required_container_evidence,
            ),
            AssetGenerationPublisher(profile.staging_root, profile.canonical_root, namespace=profile.namespace),
            catalog,
            PathTranslation(profile.canonical_root, profile.namespace, profile.jellyfin_path_prefix),
            library_id=profile.jellyfin_library_id,
            freeze=None if config.fence is None else UnixFenceClient(config.fence.socket_path).freeze,
        )
        for profile in profiles
    }
    def process(operation_id: str) -> bool:
        publication = state.publication(operation_id)
        processor = processors.get(publication.source)
        if processor is not None:
            return processor(operation_id)
        return False

    def retry_pending() -> None:
        for record in tuple(state.records()):
            process(str(record["operation_id"]))

    def intake(envelope: Envelope) -> bool:
        if envelope.kind == "publisher_assisted_intent":
            service.record_assisted_intent(
                operation_id=envelope.operation_id,
                source=str(envelope.body["source"]),
                upstream_id=str(envelope.body["upstream_id"]),
                media_id=str(envelope.body["media_id"]),
                expected_bytes=int(envelope.body["expected_bytes"]),
                manifest_digest=str(envelope.body["manifest_sha256"]),
            )
            return True
        manifest = envelope.body["manifest"]
        if not isinstance(manifest, dict):
            return False
        source = manifest.get("source")
        processor = processors.get(source) if isinstance(source, str) else None
        if processor is None:
            return False
        bundle = verified_bundle_from_manifest(manifest, processor._inspection)
        asset_slot = manifest["asset_slot"]
        item_type = manifest["item_type"]
        providers = manifest["provider_ids"]
        if not isinstance(asset_slot, str) or not isinstance(item_type, str) or providers is not None and not isinstance(providers, dict):
            return False
        expected_path = processor._translation.to_jellyfin(processor._translation.logical_payload(asset_slot, bundle.payload.relative_path))
        if manifest.get("expected_catalog_path") != expected_path:
            return False
        arguments = dict(
            operation_id=envelope.operation_id,
            source=source,
            upstream_id=str(manifest["upstream_id"]),
            media_id=str(manifest["media_id"]),
            asset_slot=asset_slot,
            item_type=item_type,
            provider_ids={} if providers is None else {str(key): str(value) for key, value in providers.items()},
            bundle=bundle,
            manifest_digest=str(envelope.body["manifest_sha256"]),
        )
        if envelope.kind == "publisher_bootstrap":
            service.bootstrap_bundle(**arguments)
        else:
            publication = state.publication(envelope.operation_id)
            correlation = correlations.get(source)
            derive = getattr(correlation, "candidate_identity", None)
            identity = derive(publication.upstream_id, publication.media_id) if callable(derive) else None
            if (
                identity is None
                or identity.relative_path != bundle.payload.relative_path
                or identity.asset_slot != arguments["asset_slot"]
                or identity.item_type != arguments["item_type"]
                or dict(identity.provider_ids) != arguments["provider_ids"]
            ):
                return False
            service.complete_assisted_bundle(
                operation_id=envelope.operation_id,
                asset_slot=arguments["asset_slot"],
                item_type=arguments["item_type"],
                provider_ids=arguments["provider_ids"],
                bundle=arguments["bundle"],
                manifest_digest=arguments["manifest_digest"],
            )
        process(envelope.operation_id)
        return True

    daemon = PublisherDaemon(
        service,
        PublisherObservability(state),
        readiness=readiness,
        process=process,
        intake=intake,
        retry=retry_pending,
    )
    return store, daemon, writer_locks


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
    parser.add_argument("--config", type=Path)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.version:
        print(__version__)
        return 0
    if arguments.config is None:
        parser.error("--config is required")
    try:
        config = load_config(arguments.config)
        if arguments.check_config:
            print(render_result("ok", "configuration valid", as_json=arguments.json))
            return 0
        if arguments.status:
            ready = _component_ready(config.publisher.socket_path) if config.publisher is not None else False
            print(render_result("ok" if ready else "inhibited", "ready" if ready else "unavailable", as_json=arguments.json))
            return 0 if ready else 1
        store, daemon, writer_locks = _runtime(config)
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
        for writer_lock in writer_locks:
            writer_lock.close()
        store.close()
    return 0
