from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_retired_target_removed_from_settings_and_summary():
    settings = (ROOT / 'web/templates/settings.html').read_text()
    index = (ROOT / 'web/templates/index.html').read_text()
    assert '<h2>OneDrive' not in settings
    assert "$('#od-start')" not in settings
    script = (ROOT / 'web/static/settings.js').read_text()
    # Positive provider whitelist excludes both retired target and OAuth config.
    assert "Object.hasOwn(names,p.provider)" in script
    assert "google:'Google Drive'" in script
    assert 'onedrive:' not in script and 'google_oauth:' not in script
    assert '<option value="onedrive">' not in index
    assert '帳號 OD' not in index
    assert 's.onedrive_free_gb' not in index
    assert "j.status === 'failed' && !retiredTarget" in index
    assert '!retiredTarget && (j.status' in index


def test_legacy_records_have_no_retired_player_or_location_actions():
    task = (ROOT / 'web/templates/task.html').read_text()
    assert "retiredTarget ? '舊目標（已停用）' : dest" in task
    assert "retiredTarget ? '' : job.error_message" in task
    assert "let acts = retiredTarget ? ''" in task
    assert "!retiredTarget && videoExt.test(name)" in task
    assert "if (!retiredTarget && f.status === 'done')" in task
    assert '重新搬運到 Google Drive' in task


def test_system_summary_no_longer_queries_retired_service():
    source = (ROOT / 'app/api/routes_tasks.py').read_text()
    summary = source.split('async def system_status', 1)[1].split('@router.get("")', 1)[0]
    assert 'onedrive' not in summary
