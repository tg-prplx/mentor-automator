import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    root: Path = Path.cwd()
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: int = 0
    telegram_user_id: int = 0
    moodle_url: str = "https://mentor.khai.edu"
    poll_seconds: int = Field(300, ge=30)
    course_ids: str = ""
    selectors_file: Path = Path("config/selectors.json")
    data_dir: Path = Path("data")
    browser_channel: str = "chrome"
    show_panel: bool = False
    action_delay_min: float = Field(0.25, ge=0)
    action_delay_max: float = Field(0.65, ge=0)
    min_confidence: float = Field(0.55, ge=0, le=1)
    uncertain_answer_policy: str = "pause"
    confirmation_ttl: int = Field(900, ge=30)
    llm_backend: str = "codex"
    codex_command: str = "codex"
    codex_model: str = ""
    codex_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = "high"
    llm_base_url: str = "http://127.0.0.1:1234/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = Field(1200, ge=128)
    llm_timeout: float = Field(180, ge=1)
    llm_retries: int = Field(2, ge=0, le=5)
    llm_json_mode: bool = True
    llm_send_temperature: bool = True
    llm_vision: bool = True

    @model_validator(mode="after")
    def validate_options(self):
        u = urlsplit(self.moodle_url)
        if u.scheme not in {"https", "http"} or not u.hostname or u.username or u.password:
            raise ValueError("Invalid Moodle URL")
        if u.scheme == "http" and u.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("Moodle requires HTTPS outside localhost")
        if u.query or u.fragment:
            raise ValueError("Moodle URL cannot contain query parameters")
        if self.action_delay_max < self.action_delay_min:
            raise ValueError("Invalid action delays")
        if self.llm_backend not in {"codex", "openai"}:
            raise ValueError("Unknown LLM backend")
        if self.browser_channel not in {"chrome", "msedge", "chromium"}:
            raise ValueError("Unknown browser channel")
        if self.uncertain_answer_policy not in {"pause", "skip"}:
            raise ValueError("Unknown uncertain answer policy")
        return self

    def path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.root / path

    @property
    def data(self) -> Path:
        return self.path(self.data_dir)

    @classmethod
    def load(cls, root: Path):
        # Explicitly called by main(), never on import. Does not touch cred.txt.
        env = {**dotenv_values(root / ".env"), **os.environ}
        return cls(root=root, **{k.lower(): v for k, v in env.items() if v is not None})


class Selectors:
    def __init__(self, path: Path):
        self.values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(self.values, dict) or not all(isinstance(v, str) for v in self.values.values()):
            raise ValueError("Invalid selectors file")

    def __getitem__(self, key: str) -> str:
        return self.values[key]
