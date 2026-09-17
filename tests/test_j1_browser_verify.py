from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager import j1_browser, j1_browser_verify as verify


def _container(service: str, identifier: str, networks: dict[str, str]) -> dict[str, object]:
    return {
        "Id": identifier,
        "Config": {"Labels": {"com.docker.compose.service": service}, "User": "10001:10001"},
        "HostConfig": {
            "PortBindings": {},
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "NetworkSettings": {
            "Ports": {"18890/tcp": None},
            "Networks": {name: {"IPAddress": address} for name, address in networks.items()},
        },
    }


def _topology() -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    renderer = _container(verify.RENDERER_SERVICE, "renderer-id", {verify.CONTROL_NETWORK: verify.RENDERER_CONTROL_IP})
    proxy = _container(
        verify.PROXY_SERVICE,
        "proxy-id",
        {verify.CONTROL_NETWORK: verify.PROXY_CONTROL_IP, verify.EGRESS_NETWORK: "172.30.0.2"},
    )
    return (
        {verify.RENDERER_SERVICE: renderer, verify.PROXY_SERVICE: proxy},
        {
            verify.CONTROL_NETWORK: {"Internal": True, "Containers": {"renderer-id": {}, "proxy-id": {}}},
            verify.EGRESS_NETWORK: {"Internal": False, "Containers": {"proxy-id": {}}},
        },
    )


def test_attestation_is_bound_to_the_exact_active_release(monkeypatch: pytest.MonkeyPatch) -> None:
    revision = "a" * 40
    monkeypatch.setattr(j1_browser, "active_release_revision", lambda *_args: (revision, ""))
    monkeypatch.setattr(j1_browser, "_attestation_revision", lambda *_args: revision)
    monkeypatch.setattr(j1_browser, "_socket_ready", lambda *_args: True)
    assert j1_browser.browser_status() == {
        "browser_ready": True,
        "browser_reason": "",
        "browser_release_revision": revision,
        "browser_attestation_revision": revision,
        "browser_socket_ready": True,
    }

    monkeypatch.setattr(j1_browser, "_attestation_revision", lambda *_args: "b" * 40)
    assert j1_browser.browser_status() == {
        "browser_ready": False,
        "browser_reason": "browser_isolation_stale",
        "browser_release_revision": revision,
        "browser_attestation_revision": "b" * 40,
        "browser_socket_ready": True,
    }


def test_marker_reader_rejects_non_0600_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    revision = "a" * 40
    monkeypatch.setattr(j1_browser.os, "open", lambda *_args: 7)
    monkeypatch.setattr(j1_browser.os, "close", lambda *_args: None)
    monkeypatch.setattr(j1_browser.os, "read", lambda *_args: j1_browser.attestation_content(revision))
    monkeypatch.setattr(
        j1_browser.os,
        "fstat",
        lambda _descriptor: type("Info", (), {"st_mode": 0o100644, "st_uid": 0})(),
    )
    assert j1_browser._attestation_revision("/run/attestation") == ""


def test_verifier_accepts_exact_two_container_topology() -> None:
    containers, networks = _topology()
    verify.verify_topology(containers, networks)


@pytest.mark.parametrize("mutation", ("port", "renderer_egress", "private_control"))
def test_verifier_rejects_topology_escape(mutation: str) -> None:
    containers, networks = _topology()
    changed_containers = deepcopy(containers)
    changed_networks = deepcopy(networks)
    if mutation == "port":
        changed_containers[verify.PROXY_SERVICE]["NetworkSettings"]["Ports"]["18890/tcp"] = [{"HostPort": "18890"}]  # type: ignore[index]
        expected = "browser_ports_published"
    elif mutation == "renderer_egress":
        changed_containers[verify.RENDERER_SERVICE]["NetworkSettings"]["Networks"][verify.EGRESS_NETWORK] = {
            "IPAddress": "172.30.0.3"
        }  # type: ignore[index]
        expected = "browser_network_topology_invalid"
    else:
        changed_networks[verify.CONTROL_NETWORK]["Internal"] = False
        expected = "browser_network_topology_invalid"
    with pytest.raises(verify.VerificationError, match=expected):
        verify.verify_topology(changed_containers, changed_networks)


def test_attestation_content_rejects_non_sha_revision() -> None:
    assert j1_browser.attestation_content("not-a-revision") == b""
    assert b"revision=" + b"a" * 40 in j1_browser.attestation_content("a" * 40)


def test_verifier_clears_an_old_marker_before_any_live_check(monkeypatch: pytest.MonkeyPatch) -> None:
    revision = "a" * 40
    sequence: list[str] = []
    containers, networks = _topology()
    monkeypatch.setattr(j1_browser, "active_release_revision", lambda *_args: (revision, ""))
    monkeypatch.setattr(verify, "_sealed_directory", lambda *_args: True)
    monkeypatch.setattr(verify, "_socket_is_ready", lambda *_args: True)
    monkeypatch.setattr(verify, "_load_running_topology", lambda *_args: (containers, networks))
    monkeypatch.setattr(verify, "_verify_proxy_private_dns", lambda *_args: sequence.append("dns"))
    monkeypatch.setattr(verify, "_render_probe", lambda *_args: sequence.append("render"))
    monkeypatch.setattr(verify, "_remove_old_attestation", lambda *_args: sequence.append("clear"))
    monkeypatch.setattr(verify, "_write_attestation", lambda *_args: sequence.append("write"))

    result = verify.verify_and_attest(
        release_root="/opt/autostop-manager-releases/current",
        socket_path="/run/autostop-j1-browser/renderer.sock",
        marker_path="/run/autostop-j1-browser-attestation/isolation-ready",
    )

    assert result["browser_ready"] is True
    assert sequence == ["clear", "dns", "render", "write"]


def test_health_probe_reports_container_state_without_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    containers, networks = _topology()
    containers[verify.RENDERER_SERVICE]["State"] = {"Status": "running"}
    containers[verify.PROXY_SERVICE]["State"] = {"Status": "running"}
    monkeypatch.setattr(
        j1_browser,
        "browser_status",
        lambda: {
            "browser_ready": True,
            "browser_reason": "",
            "browser_release_revision": "a" * 40,
            "browser_attestation_revision": "a" * 40,
            "browser_socket_ready": True,
        },
    )
    monkeypatch.setattr(verify, "_load_running_topology", lambda *_args: (containers, networks))

    result = verify.probe_browser_stack()

    assert result["browser_containers_ready"] is True
    assert result["browser_containers"] == {"renderer": "running", "proxy": "running"}
