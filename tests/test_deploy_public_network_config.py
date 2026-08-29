from pathlib import Path


BICEP = Path("deploy/main.bicep")
SCRIPT = Path("deploy/deploy.sh")


def _bicep() -> str:
    return BICEP.read_text(encoding="utf-8")


def test_production_topology_uses_default_azure_network_and_public_postgres() -> None:
    text = _bicep()

    assert "var caeName = '${namePrefix}-cae-public'" in text
    assert "var appName = '${namePrefix}-backend-public'" in text
    assert "var pgServerName = '${namePrefix}-pg-public-${take(uniqueString(resourceGroup().id), 6)}'" in text
    assert "publicNetworkAccess: 'Enabled'" in text
    assert "delegatedSubnetResourceId" not in text
    assert "privateDnsZoneArmResourceId" not in text
    assert "Microsoft.Network/virtualNetworks" not in text
    assert "Microsoft.Network/privateEndpoints" not in text
    assert "Microsoft.Network/privateDnsZones" not in text


def test_container_app_uses_direct_secrets_and_can_disable_scheduler_for_cutover() -> None:
    text = _bicep()

    assert "param schedulerDisabled bool = true" in text
    assert "param bootstrapNetworkOnly bool = true" in text
    assert "name: 'database-url-public'" in text
    assert "{ name: 'DATABASE_URL', secretRef: 'database-url-public' }" in text
    assert "{ name: 'THOTH_DISABLE_SCHEDULER', value: schedulerDisabled ? '1' : '0' }" in text
    assert "{ name: 'THOTH_BOOTSTRAP_NETWORK_ONLY', value: bootstrapNetworkOnly ? '1' : '0' }" in text
    assert "keyVaultUrl:" not in text
    assert "value: jwtSecret" in text
    assert "value: serverFernetKey" in text
    assert "value: serverApiKey" in text
    assert "value: adminApiKey" in text


def test_deploy_script_passes_cutover_scheduler_flag() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'SCHEDULER_DISABLED="${SCHEDULER_DISABLED:-true}"' in text
    assert 'BOOTSTRAP_NETWORK_ONLY="${BOOTSTRAP_NETWORK_ONLY:-true}"' in text
    assert '"schedulerDisabled": {"value": os.environ["SCHEDULER_DISABLED"] == "true"}' in text
    assert '"bootstrapNetworkOnly": {"value": os.environ["BOOTSTRAP_NETWORK_ONLY"] == "true"}' in text
    assert 'if [[ "$BOOTSTRAP_NETWORK_ONLY" == "false" ]]' in text
