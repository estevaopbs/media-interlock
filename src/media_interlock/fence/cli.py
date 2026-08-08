"""Fence daemon entry point; deployment owns scheduling and process supervision."""

from __future__ import annotations

import argparse
import asyncio
import socket
import stat
import uuid
from pathlib import Path

from ..adapters.prowlarr import ProwlarrAdapter
from ..adapters.qbittorrent import QbittorrentAdapter
from ..cli import render_result
from ..config import ConfigError, ProductConfig, load_config
from ..contracts import Envelope, StatusCode, status_response
from .daemon import FenceDaemon
from .model import FencePolicy
from .observability import FenceObservability
from .service import FenceService
from .store import FenceStore


def _component_ready(socket_path: Path) -> bool:
    """Ask the other single-writer process; failure is unavailable, never ready."""
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


def _runtime(config: ProductConfig) -> tuple[FenceStore, FenceDaemon, FenceObservability]:
    if config.fence is None:
        raise ConfigError("configuration has no fence component")
    try:
        qbittorrent_config = config.adapters["qbittorrent"]
    except KeyError as exc:
        raise ConfigError("Fence requires a configured qBittorrent adapter") from exc
    qbittorrent = QbittorrentAdapter(qbittorrent_config.base_url, qbittorrent_config.secrets["username"], qbittorrent_config.secrets["password"], staging_root=config.fence.staging_root, category=config.fence.qbittorrent_category)
    prowlarr_config = config.adapters.get("prowlarr")
    prowlarr = None if prowlarr_config is None else ProwlarrAdapter(prowlarr_config.base_url, prowlarr_config.secrets["api_key"])
    store = FenceStore.open(config.fence.state_dir)
    state = store.load(FencePolicy(config.fence.capacity_bytes, config.fence.max_inflight))
    service = FenceService(state, store, qbittorrent, prowlarr)
    observability = FenceObservability(state)
    daemon = FenceDaemon(service, observability, readiness=lambda: (qbittorrent.ready(), prowlarr is None or prowlarr.ready(), config.publisher is not None and _component_ready(config.publisher.socket_path)))
    return store, daemon, observability


async def _serve(socket_path: Path, daemon: FenceDaemon) -> None:
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        mode = socket_path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(mode):
            raise OSError("Fence socket path exists and is not a socket")
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.close()
            await writer.wait_closed()
        except (ConnectionRefusedError, FileNotFoundError):
            socket_path.unlink()
        else:
            raise OSError("Fence socket is already active")
    server = await asyncio.start_unix_server(daemon.handle, path=socket_path)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media-interlock-fence")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        if arguments.status:
            ready = _component_ready(config.fence.socket_path) if config.fence is not None else False
            print(render_result("ok" if ready else "inhibited", "ready" if ready else "unavailable", as_json=arguments.json))
            return 0 if ready else 1
        store, daemon, _ = _runtime(config)
    except (ConfigError, OSError) as exc:
        print(render_result("invalid_contract", str(exc), as_json=arguments.json))
        return 2
    try:
        assert config.fence is not None
        daemon.recover()
        asyncio.run(_serve(config.fence.socket_path, daemon))
    except OSError as exc:
        print(render_result("unavailable", str(exc), as_json=arguments.json))
        return 1
    finally:
        store.close()
    return 0
