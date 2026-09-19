"""轮转履约后端。

事件溯源：所有事实只增不改，任一操作（事件序号）都可还原当时的权责状态。
"""

from .domain import RotationService, DomainError, ConflictError, NotFoundError, AuthorizationError, ValidationError
from .store import EventStore

__all__ = [
    "RotationService",
    "EventStore",
    "DomainError",
    "ConflictError",
    "NotFoundError",
    "AuthorizationError",
    "ValidationError",
]
