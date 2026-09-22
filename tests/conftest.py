"""测试全局配置：让 INFO 级判定日志打印到 stdout（pytest -s 可见）。

测试默认捕获日志；运行 ``uv run pytest -s`` 可在控制台看到每次聚合的
价格输入、各来源接受/拒绝原因、熔断与存取判定依据。
"""

import logging
import sys


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


_configure_logging()
