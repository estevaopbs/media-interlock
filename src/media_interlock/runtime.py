"""One-process composition and shared durable state for MediaInterlock."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Callable
import uuid

from ._infra.state import SqliteStore
from .config import ConfigError, ProductConfig, VideoCandidateHealthConfig
from ._infra.advisory_lease import AdvisoryLease
from .adapters.bazarr import BazarrAdapter
from .adapters.jellyfin import JellyfinAdapter
from .adapters.prowlarr import ProwlarrAdapter
from .adapters.qbittorrent import QbittorrentAdapter
from .adapters.radarr import RadarrAdapter
from .adapters.seerr import SeerrAdapter
from .adapters.sonarr import SonarrAdapter
from .fence.daemon import FenceDaemon
from .fence.headroom import HeadroomPool, PhysicalHeadroom, statvfs_free_bytes
from .fence.model import FencePolicy
from .fence.observability import FenceObservability
from .fence.service import FenceService, FenceSource
from .fence.store import FenceStore
from .publisher.daemon import PublisherDaemon
from .publisher.filesystem import BundleVerifier
from .publisher.generation import AssetGenerationPublisher, CanonicalWriterLock
from .publisher.observability import PublisherObservability
from .publisher.service import AssetPublisherWorkProcessor, PathTranslation, PublisherService, verified_bundle_from_manifest
from .publisher.store import PublisherStore
from .reconciler.model import SearchIntent
from .reconciler.scheduler import UpgradeScheduler
from .reconciler.service import ReconcilerService, ReconcilerSource
from .reconciler.store import ReconcilerStore
from .contracts import ContractError, Envelope


@dataclass
class RuntimeState:
    """Own the one SQLite connection used by all in-process product roles."""

    _store: SqliteStore
    database_path: Path
    fence: FenceStore
    publisher: PublisherStore
    reconciler: ReconcilerStore

    _ADOPTION_MARKER = "media-interlock.legacy-adoption.v1"
    _LEGACY_KEYS = {
        "fence": ("fence.reservations.v2",),
        "publisher": ("publisher.publications.v3",),
        "reconciler": ("reconciler.intents.v1", "reconciler.schedule.v1"),
    }

    @classmethod
    def open(cls, state_dir: Path) -> "RuntimeState":
        store = SqliteStore.open(state_dir, "media-interlock")
        try:
            cls._adopt_legacy_once(state_dir, store)
            return cls(
                store,
                state_dir / "state.sqlite3",
                FenceStore.from_store(store),
                PublisherStore.from_store(store),
                ReconcilerStore.from_store(store),
            )
        except BaseException:
            store.close()
            raise

    @classmethod
    def _adopt_legacy_once(cls, state_dir: Path, store: SqliteStore) -> None:
        if store.get(cls._ADOPTION_MARKER) is not None:
            return
        records: dict[str, str] = {}
        for component, keys in cls._LEGACY_KEYS.items():
            records.update(cls._read_legacy_records(state_dir / component, component, keys))
        values = {
            key: value
            for key, value in records.items()
            if store.get(key) is None
        }
        values[cls._ADOPTION_MARKER] = "complete"
        store.put_many(values)

    @staticmethod
    def _read_legacy_records(state_dir: Path, owner: str, keys: tuple[str, ...]) -> dict[str, str]:
        database = state_dir / "state.sqlite3"
        if not database.exists():
            return {}
        if SqliteStore._read_owner_marker(state_dir) != owner:
            raise RuntimeError("legacy MediaInterlock state has an unexpected owner")
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                stored_owner = connection.execute("SELECT value FROM metadata WHERE key = 'owner'").fetchone()
                if stored_owner is None or stored_owner[0] != owner:
                    raise RuntimeError("legacy MediaInterlock state has an unexpected owner")
                return {
                    key: str(row[0])
                    for key in keys
                    if (row := connection.execute("SELECT value FROM values_store WHERE key = ?", (key,)).fetchone()) is not None
                }
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise RuntimeError("legacy MediaInterlock state cannot be read") from exc

    def close(self) -> None:
        self._store.close()


@dataclass
class MediaInterlockRuntime:
    """Composed product runtime; all long-running roles live in this process."""

    state: RuntimeState
    fence: FenceDaemon
    publisher: PublisherDaemon
    scheduler: UpgradeScheduler
    lease: AdvisoryLease
    writer_locks: tuple[CanonicalWriterLock, ...]
    _reconciler_interval_seconds: int
    _import_interval_seconds: int
    _reconcile_imports: Callable[[], None]
    _video_candidate_health: VideoCandidateHealthConfig

    @classmethod
    def from_config(cls, config: ProductConfig) -> "MediaInterlockRuntime":
        if config.fence is None or config.publisher is None or config.reconciler is None:
            raise ConfigError("MediaInterlock requires Fence, Publisher, and Reconciler")
        try:
            qbittorrent_config = config.adapters["qbittorrent"]
            radarr_config = config.adapters["radarr"]
            sonarr_config = config.adapters["sonarr"]
            jellyfin_config = config.adapters["jellyfin"]
        except KeyError as exc:
            raise ConfigError("MediaInterlock requires qBittorrent, Radarr, Sonarr, and Jellyfin adapters") from exc

        state = RuntimeState.open(config.runtime.state_dir)
        lease: AdvisoryLease | None = None
        writer_locks: tuple[CanonicalWriterLock, ...] = ()
        try:
            qbittorrent = QbittorrentAdapter(
                qbittorrent_config.base_url,
                qbittorrent_config.secrets.get("username"),
                qbittorrent_config.secrets.get("password"),
                api_key=qbittorrent_config.secrets.get("api_key"),
            )
            prowlarr_config = config.adapters.get("prowlarr")
            prowlarr = None if prowlarr_config is None else ProwlarrAdapter(prowlarr_config.base_url, prowlarr_config.secrets["api_key"])
            lease = AdvisoryLease.open(config.fence.mutation_lock.path, timeout_ms=config.fence.mutation_lock.timeout_ms)
            fence_state = state.fence.load(FencePolicy(config.fence.capacity_bytes, config.fence.max_inflight))
            observers = {
                "radarr": RadarrAdapter(radarr_config.base_url, radarr_config.secrets["api_key"], staging_root=None),
                "sonarr": SonarrAdapter(sonarr_config.base_url, sonarr_config.secrets["api_key"], staging_root=None),
            }
            pools = {
                name: HeadroomPool(name, pool.minimum_free_bytes, pool.safety_margin_bytes, pool.probe_path)
                for name, pool in config.capacity_pools.items()
            }
            fence_sources = {
                name: FenceSource(profile.category, profile.qbittorrent_save_path, profile.download_client_id, profile.download_pool, profile.staging_pool, profile.canonical_pool)
                for name, profile in config.fence.sources.items()
            }

            def source_ready() -> bool:
                try:
                    return qbittorrent.ready() and all(
                        observers[name].stopped_qbittorrent_client(profile.category, profile.download_client_id)
                        for name, profile in config.fence.sources.items()
                    )
                except Exception:
                    return False

            fence_service = FenceService(
                fence_state,
                state.fence,
                qbittorrent,
                prowlarr,
                sources=fence_sources,
                observers=observers,
                headroom=PhysicalHeadroom(pools, free_bytes=statvfs_free_bytes),
                lease=lease,
                resume_ready=source_ready,
                video_candidate_health=config.fence.video_candidate_health,
            )

            publisher_state = state.publisher.load()
            profiles = tuple(config.publisher.sources.values())
            roots_ready = all(
                path.exists() and path.is_dir() and not path.is_symlink()
                for profile in profiles
                for path in (profile.staging_root, profile.canonical_root)
            )
            writer_locks = tuple(CanonicalWriterLock.acquire(profile.canonical_root) for profile in profiles) if roots_ready else ()
            publisher_service = PublisherService(publisher_state, state.publisher)
            correlations = {
                "radarr": RadarrAdapter(
                    radarr_config.base_url,
                    radarr_config.secrets["api_key"],
                    staging_root=config.publisher.sources["radarr"].staging_root,
                    arr_import_path_prefix=config.publisher.sources["radarr"].arr_import_path_prefix,
                ),
                "sonarr": SonarrAdapter(
                    sonarr_config.base_url,
                    sonarr_config.secrets["api_key"],
                    staging_root=config.publisher.sources["sonarr"].staging_root,
                    arr_import_path_prefix=config.publisher.sources["sonarr"].arr_import_path_prefix,
                ),
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
            publisher_ready = lambda: roots_ready and catalog.ready() and all(adapter.ready() for adapter in optional)
            processors = {
                profile.name: AssetPublisherWorkProcessor(
                    publisher_service,
                    {profile.name: correlations[profile.name]},
                    BundleVerifier(
                        profile.staging_root,
                        settle_seconds=profile.bundle_settle_seconds,
                        sidecar_extensions=profile.bundle_sidecar_extensions,
                        required_languages=profile.bundle_required_languages,
                        required_subtitle_languages=profile.bundle_required_subtitle_languages,
                        language_aliases=dict(profile.bundle_language_aliases),
                        required_container_evidence=profile.bundle_required_container_evidence,
                    ),
                    AssetGenerationPublisher(profile.staging_root, profile.canonical_root, namespace=profile.namespace),
                    catalog,
                    PathTranslation(profile.canonical_root, profile.namespace, profile.jellyfin_path_prefix),
                    library_id=profile.jellyfin_library_id,
                    freeze=fence_service.freeze,
                )
                for profile in profiles
            }

            def process(operation_id: str) -> bool:
                publication = publisher_state.publication(operation_id)
                processor = processors.get(publication.source)
                return False if processor is None else processor(operation_id)

            def retry_pending() -> None:
                for record in tuple(publisher_state.records()):
                    process(str(record["operation_id"]))

            def reconcile_imports() -> None:
                """Adopt only exact historical Arr imports still in staging.

                History IDs become deterministic publication operation IDs, so
                a restart between durable adoption and cursor advancement is
                idempotent and does not scan a media tree.
                """
                cursors = state.publisher.load_import_cursors()
                for source, profile in config.publisher.sources.items():
                    observation = correlations[source].imported_after(
                        cursors.get(source, 0),
                        maximum=config.publisher.import_reconciliation.max_imports_per_poll,
                    )
                    if observation is None:
                        continue
                    next_cursor, imports = observation
                    for imported in imports:
                        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"media-interlock/import/{source}/{imported.history_id}"))
                        bundle = processors[source]._inspection.verify(imported.relative_path)
                        identity = correlations[source].candidate_identity(imported.download_id, imported.media_id)
                        if identity is None or identity.relative_path != imported.relative_path:
                            continue
                        manifest_digest = hashlib.sha256(
                            json.dumps(
                                {"source": source, "history_id": imported.history_id, "download_id": imported.download_id, "media_id": imported.media_id},
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                        publisher_service.bootstrap_bundle(
                            operation_id=operation_id,
                            source=source,
                            upstream_id=imported.download_id,
                            media_id=imported.media_id,
                            asset_slot=identity.asset_slot,
                            item_type=identity.item_type,
                            provider_ids=identity.provider_ids,
                            bundle=bundle,
                            manifest_digest=manifest_digest,
                        )
                        process(operation_id)
                    if next_cursor > cursors.get(source, 0):
                        state.publisher.save_import_cursor(source, next_cursor)

            def intake(envelope: Envelope) -> bool:
                if envelope.kind == "publisher_assisted_intent":
                    publisher_service.record_assisted_intent(
                        operation_id=envelope.operation_id,
                        source=str(envelope.body["source"]),
                        upstream_id=str(envelope.body["upstream_id"]),
                        media_id=str(envelope.body["media_id"]),
                        expected_bytes=int(envelope.body["expected_bytes"]),
                        manifest_digest=str(envelope.body["manifest_sha256"]),
                    )
                    return True
                manifest = envelope.body.get("manifest")
                if not isinstance(manifest, dict):
                    return False
                source = manifest.get("source")
                processor = processors.get(source) if isinstance(source, str) else None
                if processor is None:
                    return False
                bundle = verified_bundle_from_manifest(manifest, processor._inspection)
                asset_slot = manifest.get("asset_slot")
                item_type = manifest.get("item_type")
                providers = manifest.get("provider_ids")
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
                    publisher_service.bootstrap_bundle(**arguments)
                    return True
                publication = publisher_state.publication(envelope.operation_id)
                derive = getattr(correlations.get(source), "candidate_identity", None)
                identity = derive(publication.upstream_id, publication.media_id) if callable(derive) else None
                if identity is None or identity.relative_path != bundle.payload.relative_path or identity.asset_slot != asset_slot or identity.item_type != item_type or dict(identity.provider_ids) != arguments["provider_ids"]:
                    return False
                publisher_service.complete_assisted_bundle(
                    operation_id=envelope.operation_id,
                    asset_slot=asset_slot,
                    item_type=item_type,
                    provider_ids=arguments["provider_ids"],
                    bundle=bundle,
                    manifest_digest=arguments["manifest_digest"],
                )
                return True

            publisher = PublisherDaemon(
                publisher_service,
                PublisherObservability(publisher_state),
                readiness=publisher_ready,
                process=process,
                intake=intake,
                retry=retry_pending,
            )
            fence = FenceDaemon(
                fence_service,
                FenceObservability(fence_state, lease_probe=lease.probe),
                readiness=lambda: (source_ready(), prowlarr is None or prowlarr.ready(), publisher_ready()),
            )
            reconciliation = state.reconciler.load()
            reconciler_adapters = {
                "radarr": RadarrAdapter(radarr_config.base_url, radarr_config.secrets["api_key"], staging_root=None),
                "sonarr": SonarrAdapter(sonarr_config.base_url, sonarr_config.secrets["api_key"], staging_root=None),
            }
            reconciler = ReconcilerService(
                reconciliation,
                state.reconciler,
                reconciler_adapters,
                fence_service,
                {name: ReconcilerSource(profile.category, profile.download_client_id) for name, profile in config.reconciler.sources.items()},
            )
            scheduler = UpgradeScheduler(
                state.reconciler.load_schedule(),
                reconciliation,
                state.reconciler.save_schedule,
                reconciler_adapters,
                {"radarr": config.reconciler.movie, "sonarr": config.reconciler.episode},
                reconciler,
            )
            runtime = cls(
                state,
                fence,
                publisher,
                scheduler,
                lease,
                writer_locks,
                config.reconciler.poll_interval_seconds,
                config.publisher.import_reconciliation.poll_interval_seconds,
                reconcile_imports,
                config.fence.video_candidate_health,
            )
            return runtime
        except BaseException:
            for writer_lock in writer_locks:
                writer_lock.close()
            if lease is not None:
                lease.close()
            state.close()
            raise

    def _fence_tick(self) -> None:
        self.fence.tick()
        now = int(time.time())
        for source, entity_id, initial_delay in self.fence.poll_candidate_health(now=now):
            self.scheduler.record_candidate_invalidated(
                source,
                entity_id,
                now=now,
                initial_delay_seconds=initial_delay,
                multiplier=self._video_candidate_health.replacement_multiplier,
                maximum_delay_seconds=self._video_candidate_health.replacement_max_delay_seconds,
            )
        for terminal in self.fence.pending_terminals():
            receipt = self.publisher._dispatch(terminal)
            if receipt.kind == "custody_receipt":
                self.fence.accept_custody(receipt)

    async def run(self) -> None:
        await asyncio.to_thread(self.fence.recover)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_name in ("SIGINT", "SIGTERM"):
            try:
                import signal
                loop.add_signal_handler(getattr(signal, signal_name), stop.set)
            except (AttributeError, NotImplementedError, RuntimeError):
                pass

        async def periodic(interval_seconds: float, operation: Callable[[], object]) -> None:
            while not stop.is_set():
                try:
                    # All adapters use synchronous standard-library I/O.  A
                    # slow Arr/qBittorrent/Jellyfin request must not freeze
                    # signal delivery or the other in-process roles.
                    await asyncio.to_thread(operation)
                except (ContractError, KeyError, OSError, RuntimeError, ValueError):
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                except TimeoutError:
                    continue

        tasks = (
            asyncio.create_task(periodic(5.0, self._fence_tick)),
            asyncio.create_task(periodic(1.0, self.publisher.retry_once)),
            asyncio.create_task(periodic(float(self._import_interval_seconds), self._reconcile_imports)),
            asyncio.create_task(periodic(float(self._reconciler_interval_seconds), lambda: self.scheduler.run(now=int(time.time())))),
        )
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        for writer_lock in self.writer_locks:
            writer_lock.close()
        self.lease.close()
        self.state.close()
