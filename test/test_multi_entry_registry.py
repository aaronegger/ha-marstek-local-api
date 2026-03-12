#!/usr/bin/env python3
"""Regression harness for multi-entry coordinator UDP client behavior.

This script validates that:
1) Any number of clients can share the same UDP port concurrently.
2) Disconnecting one client does not break other clients.
3) A client re-registers itself if registry state drifts.
4) Timeout handling triggers reconnect and recovers automatically.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import MethodType
from typing import Any


def _load_module(module_name: str, file_path: Path):
    """Load a module directly from a file path."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_api_module():
    """Load the integration API module without importing Home Assistant."""
    integration_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "marstek_local_api"
    )

    package_name = "custom_components.marstek_local_api"

    custom_components_pkg = sys.modules.get("custom_components")
    if custom_components_pkg is None:
        custom_components_pkg = type(sys)("custom_components")
        custom_components_pkg.__path__ = [str(integration_path.parent)]
        sys.modules["custom_components"] = custom_components_pkg

    marstek_pkg = sys.modules.get(package_name)
    if marstek_pkg is None:
        marstek_pkg = type(sys)(package_name)
        marstek_pkg.__path__ = [str(integration_path)]
        sys.modules[package_name] = marstek_pkg

    _load_module(f"{package_name}.const", integration_path / "const.py")
    return _load_module(f"{package_name}.api", integration_path / "api.py")


class _FakeTransport:
    """Minimal asyncio DatagramTransport replacement for deterministic tests."""

    def __init__(self, network: "_FakeNetwork", protocol: Any) -> None:
        self._network = network
        self._protocol = protocol
        self._closed = False

    def get_extra_info(self, name: str) -> Any:
        if name != "socket":
            return None

        class _Sock:
            def getsockname(self):
                return ("0.0.0.0", 30000)

        return _Sock()

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._closed:
            return
        self._network.handle_send(data, addr, self._protocol)

    def close(self) -> None:
        self._closed = True


class _FakeNetwork:
    """In-memory UDP responder keyed by destination host."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.endpoint_creations = 0
        self.drop_next: dict[str, int] = {}
        self.online_hosts: set[str] = set()

    def handle_send(self, data: bytes, addr: tuple[str, int], protocol: Any) -> None:
        host, port = addr

        # Ignore broadcast traffic for this harness.
        if host == "255.255.255.255":
            return

        payload = json.loads(data.decode("utf-8"))
        if host not in self.online_hosts:
            return

        drop_count = self.drop_next.get(host, 0)
        if drop_count > 0:
            self.drop_next[host] = drop_count - 1
            return

        response = {
            "id": payload.get("id"),
            "result": {
                "host": host,
                "method": payload.get("method"),
            },
        }
        response_data = json.dumps(response).encode("utf-8")
        self.loop.call_soon(protocol.datagram_received, response_data, (host, port))


class _FakeLoop:
    """Loop shim used by MarstekUDPClient.connect()."""

    def __init__(self, network: _FakeNetwork) -> None:
        self.network = network

    async def create_datagram_endpoint(self, factory, **_kwargs):
        self.network.endpoint_creations += 1
        protocol = factory()
        transport = _FakeTransport(self.network, protocol)
        return transport, protocol


async def _poll_all(clients, *, timeout: float = 0.05) -> None:
    """Assert all clients can poll and get host-specific responses."""
    results = await asyncio.gather(
        *(
            client.get_es_status(timeout=timeout, max_attempts=2)
            for client in clients
        )
    )
    for client, result in zip(clients, results):
        assert result is not None, f"No result for {client.host}"
        assert result.get("host") == client.host, (
            f"Wrong host routing for {client.host}: {result}"
        )


async def main() -> None:
    """Run the regression checks."""
    api = _load_api_module()

    # Reset shared globals for deterministic behavior.
    api._shared_transports.clear()
    api._shared_protocols.clear()
    api._transport_refcounts.clear()
    api._clients_by_port.clear()

    loop = asyncio.get_running_loop()
    network = _FakeNetwork(loop)
    fake_loop = _FakeLoop(network)
    real_get_event_loop = asyncio.get_event_loop
    asyncio.get_event_loop = lambda: fake_loop

    hosts = [f"10.0.0.{idx}" for idx in range(10, 16)]
    network.online_hosts.update(hosts)
    clients = [
        api.MarstekUDPClient(None, host=host, port=30000, remote_port=30000)
        for host in hosts
    ]

    try:
        # Simulate many entries connecting simultaneously.
        await asyncio.gather(*(client.connect() for client in clients))
        assert 30000 in api._clients_by_port, "No client registry for port 30000"
        assert len(api._clients_by_port[30000]) == len(clients), (
            f"Expected {len(clients)} clients, got {len(api._clients_by_port[30000])}"
        )
        assert network.endpoint_creations == 1, (
            f"Expected one shared transport, got {network.endpoint_creations}"
        )
        await _poll_all(clients)

        # Unload one entry; others must keep updating.
        unloaded = clients.pop(0)
        await unloaded.disconnect()
        assert len(api._clients_by_port[30000]) == len(clients), (
            "Disconnecting one client altered registry size incorrectly"
        )
        await _poll_all(clients)

        # Simulate registry drift and verify automatic re-register on next poll.
        drift_client = clients[0]
        api._clients_by_port[30000].discard(drift_client)
        api._transport_refcounts[30000] = len(api._clients_by_port[30000])
        drift_result = await drift_client.get_es_status(timeout=0.05, max_attempts=2)
        assert drift_result is not None and drift_result.get("host") == drift_client.host
        assert drift_client in api._clients_by_port[30000], (
            "Client failed to re-register after registry drift"
        )

        # Simulate a temporary timeout and verify auto-recovery path reconnects.
        recovery_client = clients[1]
        counters = {"connect": 0, "disconnect": 0}
        original_connect = recovery_client.connect
        original_disconnect = recovery_client.disconnect

        async def _counted_connect(self):
            counters["connect"] += 1
            return await original_connect()

        async def _counted_disconnect(self):
            counters["disconnect"] += 1
            return await original_disconnect()

        recovery_client.connect = MethodType(_counted_connect, recovery_client)
        recovery_client.disconnect = MethodType(_counted_disconnect, recovery_client)

        network.drop_next[recovery_client.host] = 1
        recovery_result = await recovery_client.get_es_status(timeout=0.02, max_attempts=2)
        assert recovery_result is not None and recovery_result.get("host") == recovery_client.host, (
            "Client did not recover after temporary timeout"
        )
        assert counters["disconnect"] >= 1 and counters["connect"] >= 1, (
            "Timeout did not trigger reconnect/re-register path"
        )

        await _poll_all(clients)
        print("PASS: multi-entry shared UDP registry regression checks")
    finally:
        asyncio.get_event_loop = real_get_event_loop
        await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
