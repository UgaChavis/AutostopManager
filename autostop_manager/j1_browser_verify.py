"""Release-time attestation for the isolated J1 browser stack.

This module is intentionally separate from the J1 worker.  The worker can
only consume a root-owned attestation; this verifier is the bounded release
gate that creates one after checking the running Docker topology and one
public renderer request.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
from typing import Any

from . import j1_browser


PROJECT_NAME = "autostop-j1-browser"
RENDERER_SERVICE = "renderer"
PROXY_SERVICE = "proxy"
CONTROL_NETWORK = f"{PROJECT_NAME}_j1_browser_control"
EGRESS_NETWORK = f"{PROJECT_NAME}_j1_browser_egress"
RENDERER_CONTROL_IP = "172.31.250.2"
PROXY_CONTROL_IP = "172.31.250.3"
PROBE_URL = "https://example.com/"
IMAGE_REVISION_LABEL = "org.opencontainers.image.revision"
IMAGE_REPOSITORIES = {
    RENDERER_SERVICE: "autostop-j1-browser-renderer",
    PROXY_SERVICE: "autostop-j1-browser-proxy",
}
_DOCKER_TIMEOUT_SECONDS = 25
_RENDER_TIMEOUT_SECONDS = 20
_RENDERER_PIDS_LIMIT = 128
_PROXY_PIDS_LIMIT = 64
_RENDERER_NOFILE_LIMIT = 1024
_PROXY_NOFILE_LIMIT = 256


class VerificationError(RuntimeError):
    """A bounded, non-sensitive browser isolation failure."""


Command = Callable[[list[str], int], str]


def _command_output(argv: list[str], timeout_seconds: int = _DOCKER_TIMEOUT_SECONDS) -> str:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationError("browser_docker_unavailable") from exc
    if completed.returncode != 0:
        raise VerificationError("browser_docker_unavailable")
    return completed.stdout


def _json_object(raw: str, *, error: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VerificationError(error) from exc
    if not isinstance(payload, dict):
        raise VerificationError(error)
    return payload


def _container_labels(container: dict[str, Any]) -> dict[str, Any]:
    config = container.get("Config") or {}
    labels = (config.get("Labels") or {}) if isinstance(config, dict) else {}
    return labels if isinstance(labels, dict) else {}


def _container_service(container: dict[str, Any]) -> str:
    labels = _container_labels(container)
    return str(labels.get("com.docker.compose.service") or "")


def _container_id(container: dict[str, Any]) -> str:
    return str(container.get("Id") or "")


def _container_networks(container: dict[str, Any]) -> dict[str, Any]:
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    return networks if isinstance(networks, dict) else {}


def _no_published_ports(container: dict[str, Any]) -> bool:
    host = container.get("HostConfig") or {}
    network = container.get("NetworkSettings") or {}
    configured = host.get("PortBindings") or {}
    observed = network.get("Ports") or {}
    return not any(configured.values()) and not any(observed.values())


def _hardened_container(container: dict[str, Any]) -> bool:
    config = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    caps = {str(item).upper() for item in (host.get("CapDrop") or [])}
    security = {str(item) for item in (host.get("SecurityOpt") or [])}
    return bool(
        config.get("User") == "10001:10001"
        and host.get("ReadonlyRootfs") is True
        and host.get("Privileged") is False
        and "ALL" in caps
        and "no-new-privileges:true" in security
    )


def _has_nofile_limit(host: dict[str, Any], expected: int) -> bool:
    for limit in host.get("Ulimits") or []:
        if not isinstance(limit, dict) or str(limit.get("Name") or "") != "nofile":
            continue
        soft = limit.get("Soft")
        hard = limit.get("Hard")
        if not isinstance(soft, int) or isinstance(soft, bool) or not isinstance(hard, int) or isinstance(hard, bool):
            return False
        return soft == expected and hard == expected
    return False


def _renderer_runtime_hardened(container: dict[str, Any]) -> bool:
    host = container.get("HostConfig") or {}
    return bool(
        host.get("Init") is True
        and host.get("PidsLimit") == _RENDERER_PIDS_LIMIT
        and _has_nofile_limit(host, _RENDERER_NOFILE_LIMIT)
    )


def _proxy_runtime_hardened(container: dict[str, Any]) -> bool:
    host = container.get("HostConfig") or {}
    return bool(host.get("PidsLimit") == _PROXY_PIDS_LIMIT and _has_nofile_limit(host, _PROXY_NOFILE_LIMIT))


def _network_is_internal(network: dict[str, Any], *, expected: bool) -> bool:
    return network.get("Internal") is expected


def _verify_container_images(containers: dict[str, dict[str, Any]], expected_revision: str) -> None:
    if not j1_browser.attestation_content(expected_revision):
        raise VerificationError("browser_container_image_revision_invalid")
    for service, repository in IMAGE_REPOSITORIES.items():
        container = containers.get(service) or {}
        config = container.get("Config") or {}
        image = str(config.get("Image") or "") if isinstance(config, dict) else ""
        label = str(_container_labels(container).get(IMAGE_REVISION_LABEL) or "")
        if image != f"{repository}:{expected_revision}" or label != expected_revision:
            raise VerificationError("browser_container_image_revision_invalid")


def verify_topology(
    containers: dict[str, dict[str, Any]],
    networks: dict[str, dict[str, Any]],
    *,
    expected_revision: str,
) -> None:
    """Validate the exact two-service browser topology from Docker inspect JSON."""

    if set(containers) != {RENDERER_SERVICE, PROXY_SERVICE}:
        raise VerificationError("browser_container_topology_invalid")
    renderer = containers[RENDERER_SERVICE]
    proxy = containers[PROXY_SERVICE]
    if _container_service(renderer) != RENDERER_SERVICE or _container_service(proxy) != PROXY_SERVICE:
        raise VerificationError("browser_container_topology_invalid")
    if not _container_id(renderer) or not _container_id(proxy):
        raise VerificationError("browser_container_topology_invalid")
    if not _no_published_ports(renderer) or not _no_published_ports(proxy):
        raise VerificationError("browser_ports_published")
    if not _hardened_container(renderer) or not _hardened_container(proxy):
        raise VerificationError("browser_container_hardening_invalid")
    if not _renderer_runtime_hardened(renderer):
        raise VerificationError("browser_renderer_runtime_invalid")
    if not _proxy_runtime_hardened(proxy):
        raise VerificationError("browser_proxy_runtime_invalid")
    _verify_container_images(containers, expected_revision)

    renderer_networks = _container_networks(renderer)
    proxy_networks = _container_networks(proxy)
    if set(renderer_networks) != {CONTROL_NETWORK} or set(proxy_networks) != {CONTROL_NETWORK, EGRESS_NETWORK}:
        raise VerificationError("browser_network_topology_invalid")
    if str((renderer_networks[CONTROL_NETWORK] or {}).get("IPAddress") or "") != RENDERER_CONTROL_IP:
        raise VerificationError("browser_network_topology_invalid")
    if str((proxy_networks[CONTROL_NETWORK] or {}).get("IPAddress") or "") != PROXY_CONTROL_IP:
        raise VerificationError("browser_network_topology_invalid")

    control = networks.get(CONTROL_NETWORK)
    egress = networks.get(EGRESS_NETWORK)
    if not isinstance(control, dict) or not isinstance(egress, dict):
        raise VerificationError("browser_network_topology_invalid")
    if not _network_is_internal(control, expected=True) or not _network_is_internal(egress, expected=False):
        raise VerificationError("browser_network_topology_invalid")
    control_members = control.get("Containers") or {}
    egress_members = egress.get("Containers") or {}
    if not isinstance(control_members, dict) or not isinstance(egress_members, dict):
        raise VerificationError("browser_network_topology_invalid")
    if set(control_members) != {_container_id(renderer), _container_id(proxy)} or set(egress_members) != {
        _container_id(proxy)
    }:
        raise VerificationError("browser_network_topology_invalid")


def _load_running_topology(
    command: Command = _command_output,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    ids = [
        value.strip()
        for value in command(
            ["docker", "ps", "--filter", f"label=com.docker.compose.project={PROJECT_NAME}", "--format", "{{.ID}}"],
            _DOCKER_TIMEOUT_SECONDS,
        ).splitlines()
        if value.strip()
    ]
    if len(ids) != 2 or len(set(ids)) != 2:
        raise VerificationError("browser_container_topology_invalid")
    containers: dict[str, dict[str, Any]] = {}
    for identifier in ids:
        container = _json_object(
            command(["docker", "inspect", "--format", "{{json .}}", identifier], _DOCKER_TIMEOUT_SECONDS),
            error="browser_docker_invalid",
        )
        service = _container_service(container)
        if service in containers or service not in {RENDERER_SERVICE, PROXY_SERVICE}:
            raise VerificationError("browser_container_topology_invalid")
        containers[service] = container
    networks = {
        name: _json_object(
            command(["docker", "network", "inspect", "--format", "{{json .}}", name], _DOCKER_TIMEOUT_SECONDS),
            error="browser_docker_invalid",
        )
        for name in (CONTROL_NETWORK, EGRESS_NETWORK)
    }
    return containers, networks


def _verify_proxy_private_dns(proxy_id: str, command: Command = _command_output) -> None:
    """Exercise the active proxy code's private-DNS rejection without public egress."""

    probe = (
        "from autostop_manager.j1_browser_proxy import ProxyRequestError, resolve_public_addresses; "
        "\ntry:\n resolve_public_addresses('127.0.0.1', 443)\n"
        "except ProxyRequestError:\n print('blocked')\n"
        "else:\n raise SystemExit(1)"
    )
    output = command(
        ["docker", "exec", proxy_id, "/usr/bin/python3", "-c", probe],
        _DOCKER_TIMEOUT_SECONDS,
    )
    if output.strip() != "blocked":
        raise VerificationError("browser_private_dns_guard_invalid")


def _render_probe(socket_path: str) -> None:
    """Use the root-only local socket to render one fixed, bounded public page."""

    request = {
        "schema": j1_browser.SCHEMA,
        "operation": "render",
        "url": PROBE_URL,
        "max_chars": 512,
        "timeout_seconds": _RENDER_TIMEOUT_SECONDS,
    }
    try:
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_RENDER_TIMEOUT_SECONDS + 2)
            connection.connect(socket_path)
            connection.sendall(payload)
            raw = j1_browser._read_message(connection)
        response = json.loads(raw.decode("utf-8"))
    except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("browser_render_probe_failed") from exc
    normalized = j1_browser._normalize_response(response, requested_url=PROBE_URL, max_chars=512)
    if normalized.get("ok") is not True or len(str(normalized.get("text") or "")) > 512:
        raise VerificationError("browser_render_probe_failed")


def _sealed_directory(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022


def _remove_old_attestation(marker_path: str) -> None:
    marker = Path(marker_path)
    try:
        info = marker.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise VerificationError("browser_attestation_path_invalid") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise VerificationError("browser_attestation_path_invalid")
    try:
        marker.unlink()
    except OSError as exc:
        raise VerificationError("browser_attestation_path_invalid") from exc


def _write_attestation(marker_path: str, revision: str) -> None:
    marker = Path(marker_path)
    if not marker.is_absolute() or not _sealed_directory(marker.parent):
        raise VerificationError("browser_attestation_path_invalid")
    content = j1_browser.attestation_content(revision)
    if not content:
        raise VerificationError("browser_release_revision_unavailable")
    _remove_old_attestation(marker_path)
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, 0, 0)
        finally:
            os.close(descriptor)
        os.replace(temporary, marker)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise VerificationError("browser_attestation_write_failed") from exc
    if j1_browser._attestation_revision(marker_path) != revision:
        raise VerificationError("browser_attestation_write_failed")


def _socket_is_ready(socket_path: str) -> bool:
    return j1_browser._socket_ready(socket_path)


def verify_and_attest(
    *,
    release_root: str | None = None,
    socket_path: str | None = None,
    marker_path: str | None = None,
    command: Command = _command_output,
) -> dict[str, Any]:
    """Verify the active browser stack and atomically attest it for this revision."""

    root = release_root or os.environ.get("AUTOSTOP_J1_BROWSER_RELEASE_ROOT", j1_browser.DEFAULT_RELEASE_ROOT)
    revision, reason = j1_browser.active_release_revision(root)
    if not revision:
        raise VerificationError(reason)
    socket_candidate = socket_path or os.environ.get("AUTOSTOP_J1_BROWSER_SOCKET", j1_browser.DEFAULT_SOCKET_PATH)
    marker_candidate = marker_path or os.environ.get(
        "AUTOSTOP_J1_BROWSER_ISOLATION_MARKER", j1_browser.DEFAULT_ISOLATION_MARKER
    )
    marker_parent = Path(marker_candidate).parent
    if not Path(marker_candidate).is_absolute() or not _sealed_directory(marker_parent):
        raise VerificationError("browser_attestation_path_invalid")
    # A verifier run is an all-or-nothing release gate. Remove a former marker
    # before checking the current stack so a later failed probe cannot leave
    # an older attestation authorizing browser traffic.
    _remove_old_attestation(marker_candidate)
    if not _socket_is_ready(socket_candidate):
        raise VerificationError("browser_socket_unavailable")
    containers, networks = _load_running_topology(command)
    verify_topology(containers, networks, expected_revision=revision)
    _verify_proxy_private_dns(_container_id(containers[PROXY_SERVICE]), command)
    _render_probe(socket_candidate)
    _write_attestation(marker_candidate, revision)
    return {
        "ok": True,
        "schema": "autostop.j1.browser.attestation.v1",
        "browser_ready": True,
        "browser_reason": "",
        "release_revision": revision,
    }


def probe_browser_stack(command: Command = _command_output) -> dict[str, Any]:
    """Read the current attestation and container topology without egress or mutation."""

    browser_status = j1_browser.browser_status()
    result: dict[str, Any] = {
        "ok": True,
        "schema": "autostop.j1.browser.health.v1",
        **browser_status,
        "browser_containers_ready": False,
        "browser_container_reason": "",
        "browser_containers": {},
    }
    revision = str(browser_status.get("browser_release_revision") or "")
    if not revision:
        result["browser_container_reason"] = str(
            browser_status.get("browser_reason") or "browser_release_revision_unavailable"
        )
        return result
    try:
        containers, networks = _load_running_topology(command)
        verify_topology(containers, networks, expected_revision=revision)
    except VerificationError as exc:
        result["browser_container_reason"] = str(exc)
        return result
    states: dict[str, str] = {}
    for service, container in containers.items():
        states[service] = str((container.get("State") or {}).get("Status") or "unknown")[:32]
    result.update(
        {
            "browser_containers_ready": True,
            "browser_containers": states,
        }
    )
    return result


def _verifier_runs_from_active_release(release_root: str) -> bool:
    try:
        return Path(__file__).resolve().is_relative_to(Path(release_root).resolve(strict=True))
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify and attest the isolated J1 browser stack")
    parser.add_argument("command", choices=("attest", "probe"))
    parser.add_argument("--release-root", default=j1_browser.DEFAULT_RELEASE_ROOT)
    parser.add_argument("--socket", default=j1_browser.DEFAULT_SOCKET_PATH)
    parser.add_argument("--marker", default=j1_browser.DEFAULT_ISOLATION_MARKER)
    args = parser.parse_args()
    if os.geteuid() != 0 or not _verifier_runs_from_active_release(args.release_root):
        print(json.dumps({"ok": False, "error": "browser_verifier_release_required"}), file=sys.stderr)
        raise SystemExit(2)
    if args.command == "probe":
        print(json.dumps(probe_browser_stack(), ensure_ascii=False))
        return
    try:
        result = verify_and_attest(
            release_root=args.release_root,
            socket_path=args.socket,
            marker_path=args.marker,
        )
    except VerificationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
