"""Settings from environment variables. No secrets have defaults; `local` mode uses fakes."""
import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    mode: str = "local"                     # local (fakes, for demo/tests) | production
    database_url: str | None = None         # postgresql://... ; None -> in-memory store
    kit_dir: str | None = None              # defaults to ../factory-kit
    inbox_base_url: str = "http://localhost:8080"
    inbox_signing_key: str | None = None    # HMAC key for signed inbox/Slack callbacks

    github_api_url: str = "https://api.github.com"
    github_webhook_secret: str | None = None
    push_app_id: str | None = None          # push-bot GitHub App
    push_app_private_key_path: str | None = None
    push_app_installation_id: str | None = None
    merge_app_id: str | None = None         # merge-bot GitHub App (separate identity)
    merge_app_private_key_path: str | None = None
    merge_app_installation_id: str | None = None

    llm_gateway_url: str | None = None      # LiteLLM proxy base URL
    llm_gateway_master_key: str | None = None
    slack_webhook_url: str | None = None
    slack_signing_secret: str | None = None

    sandbox_image: str = "factory-sandbox:latest"
    sandbox_root: str = "/tmp/factory-sandboxes"
    egress_proxy_url: str | None = None     # only LLM gateway + package mirror allowed
    repos_root: str = "/tmp/factory-repos"  # local clones used to build sandboxes

    deploy_command: str | None = None       # optional shell hook for DeployTarget in production
    evidence_dir: str = "/tmp/factory-evidence"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        values = {}
        for f in fields(cls):
            key = "FACTORY_" + f.name.upper()
            if key in env and env[key] != "":
                values[f.name] = env[key]
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.mode not in ("local", "production"):
            raise ValueError(f"FACTORY_MODE must be local or production, got {self.mode!r}")
        if self.mode == "production":
            required = ["database_url", "inbox_signing_key", "github_webhook_secret", "push_app_id",
                        "push_app_private_key_path", "merge_app_id", "merge_app_private_key_path",
                        "llm_gateway_url", "llm_gateway_master_key"]
            missing = [f"FACTORY_{name.upper()}" for name in required if not getattr(self, name)]
            if missing:
                raise ValueError(f"production mode requires: {', '.join(missing)}")
            if self.push_app_id == self.merge_app_id:
                raise ValueError("push bot and merge bot must be separate GitHub Apps (§11)")

    @property
    def kit_path(self) -> Path:
        from .policy.loader import default_kit_dir
        return Path(self.kit_dir) if self.kit_dir else default_kit_dir()
