from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize(
    "ready_after,active,expected_calls", [(1, True, 1), (3, True, 3), (51, True, 50), (1, False, 0)]
)
def test_installer_waits_for_listener_without_retrying_failed_service(ready_after, active, expected_calls):
    source = (Path(__file__).resolve().parents[1] / "scripts/install-manager-mcp.sh").read_text()
    function = source.split("wait_for_mcp_socket() {", 1)[1].split("\n}\n", 1)[0]
    function = "wait_for_mcp_socket() {" + function + "\n}\n"
    assert "(exec 3<>/dev/tcp/127.0.0.1/41931)" in function
    function = function.replace("(exec 3<>/dev/tcp/127.0.0.1/41931)", "listener_ready")
    script = (
        "set -u\nUNIT_NAME=test-manager.service\ncalls=0\n"
        f"systemctl() {{ return {0 if active else 3}; }}\n"
        "sleep() { :; }\n"
        f'listener_ready() {{ calls=$((calls + 1)); [[ "$calls" -ge {ready_after} ]]; }}\n'
        + function
        + 'wait_for_mcp_socket\nresult=$?\nprintf \'%s\\n\' "$calls"\nexit "$result"\n'
    )
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == (0 if active and ready_after <= 50 else 1)
    assert int(result.stdout) == expected_calls
    assert source.index("if ! wait_for_mcp_socket;") < source.index("-m autostop_manager.cli mcp-probe")
