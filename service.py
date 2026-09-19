"""医护独立执业轮转的运行入口。

保留稳定的健康检查与服务身份；领域接口由 ``rotation_api`` 分发。
"""

import argparse
from http.server import ThreadingHTTPServer

from rotation_api import build_handler

SERVICE_ID = "clinical-credential-rotation"
SERVICE_NAME = "医护独立执业轮转"

# 领域服务实例与 HTTP Handler；测试可通过 build_handler(service) 获得隔离实例。
Handler = build_handler()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
