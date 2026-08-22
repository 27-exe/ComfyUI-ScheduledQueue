"""Prompt interceptor - Stage 1 占位,阶段 2 才实际改 _on_prompt 行为"""

import logging

_log = logging.getLogger(__name__)


class PromptInterceptor:
    """包装 PromptServer.add_on_prompt_handler,阶段 1 完全 no-op"""

    def __init__(self, db=None):
        self.db = db
        # 不在 __init__ 调 _install(),因为此时 PromptServer.instance 还没就绪
        # 改成 try/except 包住 + 记日志,失败也不让 custom_node import 崩
        self._installed = False
        try:
            self._install()
            self._installed = True
        except Exception as e:
            _log.warning(
                "[ScheduledQueue] PromptInterceptor install deferred: %s. "
                "Will retry on first /prompt call.",
                e,
            )

    def _install(self):
        from server import PromptServer  # noqa: F401 (imported for side effect)
        server = PromptServer.instance
        server.add_on_prompt_handler(self._on_prompt)

    def _on_prompt(self, json_data):
        """阶段 1: no-op,直接返回原数据(不影响 ComfyUI 行为)
        阶段 2: 检查 json_data 是否带 _scheduled 字段,有则走调度队列
        """
        # 延迟 install fallback: 第一次被调时,如果还没装好,再试一次
        if not self._installed:
            try:
                self._install()
                self._installed = True
            except Exception:
                pass  # 还是装不上就当 no-op,不影响 ComfyUI
        return json_data