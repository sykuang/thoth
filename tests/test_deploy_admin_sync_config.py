from pathlib import Path


def test_admin_full_history_key_is_wired_into_deployment() -> None:
    bicep = Path("deploy/main.bicep").read_text(encoding="utf-8")
    script = Path("deploy/deploy.sh").read_text(encoding="utf-8")

    assert "param adminApiKey string" in bicep
    assert "name: 'admin-api-key'" in bicep
    assert "{ name: 'ADMIN_API_KEY', secretRef: 'admin-api-key' }" in bicep
    assert '"adminApiKey": {"value": os.environ["ADMIN_API_KEY"]}' in script
