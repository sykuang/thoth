"""Packaging-only split: API stays browser-free; all Jobs keep the worker image."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


def test_fx_http_client_is_a_production_dependency() -> None:
    import tomllib

    config = tomllib.loads(Path("pyproject.toml").read_text())
    assert "httpx2>=0.0.1" in config["project"]["dependencies"]
    assert "httpx2>=0.0.1" not in config["dependency-groups"]["dev"]


def test_api_image_offline_smoke_runs_in_fresh_process(tmp_path) -> None:
    script = Path("deploy/smoke_api_image.py").resolve()
    assert script.is_file(), "Image smoke must be runnable without pytest or credentials"
    result = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path.cwd()),
             "DB_BACKEND": "postgres", "DATABASE_URL": "postgresql://invalid.invalid/thoth",
             "SYNC_EXECUTION_MODE": "inprocess"},
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert "API image offline smoke OK" in result.stdout


@pytest.mark.parametrize("image_name", [None, "custom-worker"])
def test_deploy_builds_and_passes_both_images(tmp_path, image_name) -> None:
    # The real script runs end-to-end; az/curl are offline executables, with
    # unexpected Azure calls rejected rather than falling through to the CLI.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    az = bin_dir / "az"
    az.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
record = {"args": args}
if args[:3] == ["deployment", "group", "create"]:
    params = json.loads(Path(args[args.index("--parameters") + 1][1:]).read_text())["parameters"]
    record["images"] = {key: value["value"] for key, value in params.items() if key.endswith("Image")}
with Path(os.environ["AZ_LOG"]).open("a") as log:
    log.write(json.dumps(record) + "\\n")
if args[:2] == ["group", "create"] or args[:2] == ["acr", "build"] or args[:3] == ["deployment", "group", "create"]:
    pass
elif args[:2] == ["group", "show"]:
    print("/subscriptions/test/resourceGroups/test-rg")
elif args[:2] == ["acr", "show"]:
    if "--query" in args:
        print("test.azurecr.io")
elif args[:3] == ["ad", "signed-in-user", "show"]:
    print("00000000-0000-0000-0000-000000000000")
elif args[:3] == ["deployment", "group", "show"]:
    print("test.example.invalid")
else:
    raise SystemExit(f"Unexpected az call: {args}")
''')
    az.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nprintf '192.0.2.1'\n")
    curl.chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    secrets = tmp_path / "synthetic.env"
    secrets.write_text("\n".join(f"{key}=synthetic-test-only" for key in (
        "JWT_SECRET", "SERVER_API_KEY", "ADMIN_API_KEY", "SERVER_FERNET_KEY", "PG_ADMIN_PASSWORD",
    )))
    secrets.chmod(0o600)
    log = tmp_path / "az.jsonl"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
        "SECRETS_FILE": str(secrets), "AZ_LOG": str(log), "IMAGE_TAG": "test-sha",
    }
    if image_name:
        env["IMAGE_NAME"] = image_name
    result = subprocess.run(["/bin/bash", "deploy/deploy.sh"], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    builds = [call["args"] for call in calls if call["args"][:2] == ["acr", "build"]]
    worker = image_name or "thoth-backend"
    assert [(args[args.index("--file") + 1], args[args.index("--image") + 1]) for args in builds] == [
        ("Dockerfile", f"{worker}:test-sha"), ("Dockerfile.api", f"{worker}-api:test-sha"),
    ]
    deployments = [call["images"] for call in calls if "images" in call]
    assert deployments == [{
        "containerImage": f"test.azurecr.io/{worker}:test-sha",
        "apiContainerImage": f"test.azurecr.io/{worker}-api:test-sha",
    }] * 2


def test_only_api_selects_api_image_with_legacy_default() -> None:
    text = Path("deploy/main.bicep").read_text()
    assert "param apiContainerImage string = containerImage" in text
    resources = dict(re.findall(r"resource (\w+) 'Microsoft.App/(?:containerApps|jobs)@[^']+' = (.*?)(?=\nresource |\n// -------- outputs)", text, re.S))
    assert set(resources) == {"app", "scheduledSyncJob", "queuedSyncJob", "paymentReminderJob"}
    for name, body in resources.items():
        assert re.findall(r"image: (\w+)", body) == ["apiContainerImage" if name == "app" else "containerImage"]
    assert "pollingInterval: 30" in resources["queuedSyncJob"]


def test_api_image_keeps_frozen_python_deps_without_browser_payload() -> None:
    path = Path("Dockerfile.api")
    assert path.is_file(), "API needs a separate slim image, not the browser base"
    text = path.read_text()
    instructions = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert "FROM python:3.12-slim-bookworm" in instructions
    assert "uv sync --frozen --no-dev --no-install-project" in instructions
    assert "uv sync --frozen --no-dev" in instructions
    for source in ("pyproject.toml uv.lock README.md ./", "backend/ ./backend/", "cli/ ./cli/", "migrations/ ./migrations/"):
        assert f"COPY {source}" in instructions
    assert "SYNC_EXECUTION_MODE=external" in instructions
    assert re.search(r"RUN .*useradd.*", instructions)
    assert "USER thoth" in instructions
    assert "--chown=thoth:thoth" in instructions
    assert "EXPOSE 8000" in instructions
    assert not re.search(r"playwright|patchright|chromium|chrome|libgl1|libgtk", instructions, re.I)
    cmd = json.loads(instructions.split("CMD ", 1)[1])
    assert cmd == ["/app/.venv/bin/uvicorn", "backend.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
