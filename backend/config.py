from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MeshToPartApp"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "app.log"


@dataclass
class AppConfig:
    api_key: str = ""
    port: int = 8790
    auto_start_server: bool = True


def load_config() -> AppConfig:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AppConfig()
    except json.JSONDecodeError:
        return AppConfig()

    config = AppConfig()
    for key, value in data.items():
        if hasattr(config, key):
            setattr(config, key, value)
    return config


def save_config(config: AppConfig) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


def append_log(message: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")
