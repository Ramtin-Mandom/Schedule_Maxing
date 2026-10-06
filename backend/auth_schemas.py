"""Native-client authentication contracts; never expose stored credential hashes."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class TokenPairOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: SecretStr = Field(min_length=43, max_length=43)
