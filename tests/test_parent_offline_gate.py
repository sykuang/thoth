"""Synthetic canaries: no real secret, network, authentication or browser I/O."""
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def lifecycle_canary():
    from tests import parent_offline_gate as gate
    assert gate.active, "guard must precede fixture setup"
    yield
    assert gate.active, "guard must outlive fixture teardown"


def test_scope_is_exact():
    from tests import parent_offline_gate as gate
    assert gate.in_scope(SimpleNamespace(nodeid="tests/test_optional_diagnostic_logging.py::test_x"))
    assert not gate.in_scope(SimpleNamespace(nodeid="tests/test_bank_login_lifecycle.py::test_x"))


def test_guard_is_active_before_fixtures(request):
    from tests import parent_offline_gate as gate
    assert request.config.pluginmanager.getplugin("tests.parent_offline_gate") is gate
    assert gate.active


@pytest.mark.parametrize("name", [".env", ".env.synthetic", "data/synthetic.py", "secrets/synthetic.py"])
def test_private_open_denied_before_missing_file(name):
    from tests import parent_offline_gate as gate
    with pytest.raises(AssertionError, match="offline gate:"):
        gate.audit("open", (str(gate.ROOT / name), "r", 0))
    with pytest.raises(AssertionError, match="offline"):
        (gate.ROOT / name).read_bytes()


def test_network_denied_before_resolution():
    with pytest.raises(AssertionError, match="offline"):
        socket.getaddrinfo("synthetic.invalid", 443)
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="offline"):
            sock.connect(("192.0.2.1", 443))
        with pytest.raises(AssertionError, match="offline"):
            sock.connect_ex(("192.0.2.1", 443))


def test_real_constructors_denied():
    from backend.core import creds
    from backend.banks.dbs import DbsCrawler
    from scrapling.fetchers import StealthySession
    for constructor in (creds.BankCreds, DbsCrawler, StealthySession):
        with pytest.raises(AssertionError, match="offline"):
            constructor()
    with pytest.raises(AssertionError, match="offline"):
        creds._load_dotenv()


@pytest.mark.parametrize("sdk", ["playwright", "patchright"])
def test_browser_entrypoints_denied(sdk):
    from importlib import import_module
    cls = import_module(f"{sdk}._impl._browser_type").BrowserType
    for name in ("launch", "launch_persistent_context", "connect", "connect_over_cdp"):
        with pytest.raises(AssertionError, match="offline"):
            getattr(cls, name)(None)


def test_unapproved_child_denied_before_spawn():
    with pytest.raises(AssertionError, match="offline"):
        subprocess.run(["synthetic-forbidden-child"], check=True)


def test_policy_denies_without_private_audit_help():
    from tests import parent_offline_gate as gate
    events = [
        ("socket.connect", (None, ("192.0.2.1", 443))),
        ("socket.getaddrinfo", ("synthetic.invalid", 443)),
        ("socket.sendto", (None, ("192.0.2.1", 443))),
        ("urllib.Request", ("https://synthetic.invalid",)),
        ("http.client.connect", (None,)),
        ("open", (str(gate.ROOT / "synthetic.txt"), "r", 0)),
        ("open", (str(gate.ROOT / "tests/synthetic.py"), "w", 0)),
        ("os.system", ("synthetic-forbidden-child",)),
        ("os.symlink", ("synthetic", "synthetic")),
        ("subprocess.Popen", (gate._GIT, ["git", "status"], str(gate.ROOT), None)),
    ]
    for event, args in events:
        with pytest.raises(AssertionError, match="offline gate:"):
            gate.audit(event, args)


def test_source_read_and_synthetic_write(tmp_path):
    assert Path(__file__).read_text().startswith('"""Synthetic')
    target = tmp_path / "synthetic.txt"
    target.write_text("synthetic")
    assert target.read_text() == "synthetic"


def test_repo_write_denied_before_creation():
    from tests import parent_offline_gate as gate
    with pytest.raises(AssertionError, match="offline"):
        (gate.ROOT / "synthetic-must-not-exist.txt").write_text("synthetic")
