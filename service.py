"""医护独立执业轮转的运行入口。

保留稳定的健康检查契约；领域能力见 rotation 包。
"""

import argparse
import os

from rotation.api import (
    ApiHandler as Handler,
    SERVICE_ID,
    SERVICE_NAME,
    build_server,
    health_payload,
)
from rotation.domain import RotationService
from rotation.store import EventStore


def build_default_server(port):
    store_path = os.environ.get("ROTATION_STORE_PATH")
    service = RotationService(EventStore(store_path) if store_path else EventStore())
    return build_server(port, service)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    build_default_server(args.port).serve_forever()


if __name__ == "__main__":
    main()
