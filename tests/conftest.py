"""Keep target configuration isolated from the developer's installed agents."""

import json

import pytest


@pytest.fixture(autouse=True)
def pi_target_defaults(tmp_path, monkeypatch):
    home = tmp_path / "configured-pi"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"defaultProvider": "target-provider", "defaultModel": "target-model"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home))
