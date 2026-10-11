import subprocess
import sys


def test_cold_start_loads_sdk_before_serving_requests(tmp_path):
    script = """
import sys
from fastapi.testclient import TestClient
from backend.config import Settings
from backend.main import create_app
assert 'pymammotion.client' not in sys.modules
settings = Settings(data_dir=sys.argv[1], _env_file=None,
    connection_mode='home_assistant', ha_base_url='', ha_token='',
    mammotion_username='', mammotion_password='', auto_capture=False)
with TestClient(create_app(settings)) as browser:
    for name in ('client', 'transport.base', 'utility.device_type',
                 'messaging.command_queue', 'data.model.device_config',
                 'data.model.coordinates', 'data.model.generate_geojson'):
        assert 'pymammotion.' + name in sys.modules, name
    assert browser.get('/api/health').status_code == 200
    assert browser.get('/api/diagnostics').status_code == 200
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
