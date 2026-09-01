from pathlib import Path


BICEP = Path("deploy/main.bicep")


def test_deployment_uses_external_jobs_and_scale_to_zero() -> None:
    text = BICEP.read_text()

    assert "var scheduledJobName = '${namePrefix}-sync-scheduled'" in text
    assert "var queuedJobName = '${namePrefix}-sync-queued'" in text
    assert "var reminderJobName = '${namePrefix}-payment-reminders'" in text
    assert "resource scheduledSyncJob 'Microsoft.App/jobs@" in text
    assert "triggerType: 'Schedule'" in text
    assert "cronExpression: '0 2,4,10 * * *'" in text
    assert "args: [\n            'scheduled'\n          ]" in text
    assert "resource queuedSyncJob 'Microsoft.App/jobs@" in text
    assert "triggerType: 'Event'" in text
    assert "type: 'postgresql'" in text
    assert "status=\\'queued\\'" in text
    assert "started_at::timestamptz" in text
    assert "INTERVAL \\'7 hours\\'" in text
    assert "triggerParameter: 'connection'" in text
    assert "args: [\n            'queued'\n          ]" in text
    assert "name: reminderJobName" in text
    assert "cronExpression: '0 1 * * *'" in text
    assert "args: [\n            'reminders'\n          ]" in text
    assert text.count("replicaRetryLimit: 0") == 3
    assert text.count("replicaTimeout: 21600") == 1
    assert text.count("replicaTimeout: 900") == 2
    assert text.count("cpu: json('1.0')") == 4
    assert text.count("memory: '2.0Gi'") == 4
    assert "{ name: 'SYNC_EXECUTION_MODE', value: 'external' }" in text
    assert "minReplicas: 0" in text
    assert "maxReplicas: 1" in text
    assert "THOTH_DISABLE_SCHEDULER" not in text
