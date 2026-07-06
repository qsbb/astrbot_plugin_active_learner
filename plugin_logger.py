"""插件独立 logger。

不传播到 AstrBot 根 logger，确保插件日志只显示在插件页面，
不会污染 AstrBot 自带的日志界面。

所有插件模块统一使用：
    from .plugin_logger import logger
"""

import logging

logger = logging.getLogger("astrbot_plugin_active_learner")
logger.setLevel(logging.INFO)
# 关键：不传播到根 logger，避免日志输出到 AstrBot 主日志界面
logger.propagate = False

# 兜底 handler：防止 "No handlers could be found" 警告
# 真正的输出由 main.py 中挂载的 _BufferHandler 负责
if not any(isinstance(h, logging.NullHandler) for h in logger.handlers):
    logger.addHandler(logging.NullHandler())
