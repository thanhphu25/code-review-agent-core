"""Validate and store the dashboard administrator's provider settings."""

import re
from dataclasses import asdict, replace
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .dashboard_providers import LiveSettings
from .dashboard_store import Store

ENDPOINT_KEYS = {
    'model_url': 'model_key',
    'codelens_url': 'codelens_token',
    'graphify_url': 'graphify_token',
    'graphify_pilot_url': 'graphify_pilot_password',
}
SECRET_FIELDS = set(ENDPOINT_KEYS.values())
TOKEN_FIELDS = SECRET_FIELDS - {'graphify_pilot_password'}


def validate_endpoint(value: str) -> str:
    value = value.strip()
    if value:
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme in {'http', 'https'} and parsed.hostname and parsed.username is None
                     and parsed.password is None and parsed.port != 0 and not re.search(r'[\s\\?#]', value))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('Dùng URL HTTP(S) không chứa thông tin đăng nhập, query hay fragment.')
    return value


class ProviderPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')

    model_url: str | None = Field(default=None, max_length=2048)
    model_key: str | None = Field(default=None, max_length=4096)
    model: str | None = Field(default=None, max_length=200)
    codelens_url: str | None = Field(default=None, max_length=2048)
    codelens_token: str | None = Field(default=None, max_length=4096)
    graphify_url: str | None = Field(default=None, max_length=2048)
    graphify_token: str | None = Field(default=None, max_length=4096)
    graphify_sha: str | None = Field(default=None, max_length=64)
    graphify_pilot_url: str | None = Field(default=None, max_length=2048)
    graphify_pilot_user: str | None = Field(default=None, max_length=100)
    graphify_pilot_password: str | None = Field(default=None, max_length=1024)

    @field_validator(*ENDPOINT_KEYS)
    @classmethod
    def endpoint(cls, value: str | None):
        return validate_endpoint(value) if value is not None else None

    @field_validator('graphify_pilot_url')
    @classmethod
    def pilot_origin(cls, value: str | None):
        if value and urlsplit(value).path not in ('', '/'):
            raise ValueError('Nhập URL gốc của Graphify Pilot, không kèm đường dẫn MCP.')
        return value.rstrip('/') if value is not None else None

    @field_validator(*TOKEN_FIELDS)
    @classmethod
    def key(cls, value: str | None):
        if value is None:
            return None
        value = value.strip()
        if not re.fullmatch(r'[!-~]*', value):
            raise ValueError('Key hoặc token chứa ký tự không hợp lệ.')
        return value

    @field_validator('graphify_sha')
    @classmethod
    def sha(cls, value: str | None):
        if value is None:
            return None
        value = value.strip().lower()
        if value and not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value):
            raise ValueError('Nhập SHA đầy đủ của commit đã index.')
        return value

    @field_validator('model', 'graphify_pilot_user')
    @classmethod
    def model_name(cls, value: str | None):
        return value.strip() if value is not None else None


class ProviderSettings:
    def __init__(self, store: Store, defaults: LiveSettings):
        self.store = store
        self.defaults = defaults

    def _read(self, db) -> LiveSettings:
        values = dict(db.execute('SELECT name, value FROM provider_settings'))
        return replace(self.defaults, **values)

    def get(self) -> LiveSettings:
        with self.store.connect() as db:
            return self._read(db)

    def save(self, patch: ProviderPatch) -> LiveSettings:
        changes = patch.model_dump(exclude_none=True)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._read(db)
            for endpoint, key in ENDPOINT_KEYS.items():
                if (endpoint in changes and changes[endpoint] != getattr(current, endpoint)
                        and getattr(current, key) and key not in changes):
                    raise ValueError('Khi đổi URL, hãy nhập lại key hoặc chọn xóa key của kết nối đó.')
            if ('graphify_pilot_user' in changes and changes['graphify_pilot_user'] != current.graphify_pilot_user
                    and current.graphify_pilot_password and 'graphify_pilot_password' not in changes):
                raise ValueError('Khi đổi tài khoản Pilot, hãy nhập lại mật khẩu.')
            if 'graphify_url' in changes and changes['graphify_url'] != current.graphify_url:
                changes.setdefault('graphify_sha', '')
            db.executemany('INSERT OR REPLACE INTO provider_settings VALUES (?, ?)', changes.items())
            return replace(current, **changes)


def public_settings(settings: LiveSettings) -> dict:
    fields = {name: value for name, value in asdict(settings).items()
              if name not in SECRET_FIELDS and name != 'graphify_revision_evidence'}
    for endpoint in ENDPOINT_KEYS:
        try:
            fields[endpoint] = validate_endpoint(fields[endpoint])
        except ValueError:
            fields[endpoint] = ''
    return {
        'fields': fields,
        'has_key': {name: bool(getattr(settings, name)) for name in SECRET_FIELDS},
        'available': settings.availability(),
    }
