from __future__ import annotations

import uuid
from datetime import datetime

from app.schemas.common import ResponseModel


class UserRead(ResponseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    created_at: datetime
