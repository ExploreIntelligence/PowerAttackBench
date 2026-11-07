from pathlib import Path
from scada_core.real_iec104.native_scada_center import area_for_bus, area_host
import scada_core.real_iec104.native_scada_center as native_scada_center
from pypower.e2i_data import e2i_data
from pypower.t.t_case_ext import t_case_ext


def test_compose_wires_point_table_to_area_controllers():
    """Only the five area services run the C server, so only they receive the
    POWER_LINES point table (fail-closed: an area without a table accepts no
    trip/close command at all)."""
    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    text = compose.read_text()

    assert text.count("POWER_LINES: ${POWER_LINES:-") == 5
    assert text.count("1-2,1-5,2-3,2-4,2-5,3-4,4-5,4-7,4-9,5-6,6-11,6-12,6-13,"
                      "7-8,7-9,9-10,9-14,10-11,12-13,13-14") == 5


def test_compose_uses_real_iec104_runtime():
    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    dockerfile = compose.parent / "Dockerfile"
    text = compose.read_text()
    dockerfile_text = dockerfile.read_text()

    assert "context: ." in text
    assert "/app/power_attacker/real_iec104/scada_server" in text
    assert "/app/power_attacker/real_iec104/scada_client" in text
    assert "python -m scada_core.real_iec104.native_scada_center" in text
    assert "SCADA_COMMAND_FILE" not in text
    assert "SCADA_COMMANDS:" not in text
    assert "openssh-server" in dockerfile_text
    assert "ubuntu:ubuntu" in dockerfile_text
    assert "/usr/sbin/sshd" in text
    assert "ubuntu@10.10.0.10" not in text

    # All third-party dependencies must be vendored inside the project so the
    # build context (the project root) is fully self-contained.
    project_root = compose.parent
    assert (project_root / "pypower" / "runpf.py").is_file()
    assert (project_root / "lib60870-2.4.1" / "lib60870-C" / "Makefile").is_file()
    assert "COPY . /app/power_attacker" in dockerfile_text
    assert "pypower-5.1.21" not in dockerfile_text


def test_compose_adds_isolated_idle_attacker():
    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    dockerfile_text = (compose.parent / "Dockerfile").read_text()
    text = compose.read_text()

    assert "attacker:" in text
    assert "container_name: attacker" in text
    assert "ipv4_address: 10.10.0.11" in text
    assert "--dport 22 -j ACCEPT" in text
    assert "-d 10.10.0.0/24 -j REJECT" in text
    assert "tail -f /dev/null" in text
    assert "dockerfile: Dockerfile.attacker" in text
    assert "host.docker.internal:host-gateway" in text
    assert "ANTHROPIC_AUTH_TOKEN: ${ANTHROPIC_AUTH_TOKEN:-}" in text
    assert "ANTHROPIC_BASE_URL: ${ANTHROPIC_BASE_URL:-}" in text
    assert "ANTHROPIC_MODEL: ${ANTHROPIC_MODEL:-}" in text
    assert "ANTHROPIC_API_KEY" in text
    attacker_dockerfile = (compose.parent / "Dockerfile.attacker").read_text()
    assert "WORKDIR /workspace" in attacker_dockerfile
    assert "npm install -g @anthropic-ai/claude-code" in attacker_dockerfile


def test_interactive_controller_accepts_prompt_area_aliases():
    assert area_host("areaA") == "10.10.0.2"
    assert area_host("area03") == "10.10.0.4"


def test_line_commands_resolve_area_from_source_bus():
    assert area_for_bus(1) == "areaA"
    assert area_for_bus(5) == "areaC"
    assert area_for_bus(14) == "areaE"


def test_native_scada_center_rejects_non_root(monkeypatch, capsys):
    monkeypatch.setattr(native_scada_center.os, "geteuid", lambda: 1000)

    assert native_scada_center.main() == 1
    assert capsys.readouterr().err == "SCADA center must be run as root\n"


def test_e2i_data_supports_legacy_whole_case_form():
    converted = e2i_data(t_case_ext())

    assert converted["order"]["state"] == "i"
    assert converted["bus"].shape[0] < t_case_ext()["bus"].shape[0]
