"""带随机延迟的 WeChat 子类。

封装 wxauto ``WeChat`` 中四个**必然触发微信后端 API/RPC**（对外发送、改变
服务器状态）的 public 接口，在每次调用时机前插入随机停顿，模拟人工操作节奏、
降低被风控识别为机器人的风险：

    - ``SendMsg``      发送文本消息（消息上行 RPC）
    - ``SendFiles``    发送文件（文件上传 + 下发 RPC）
    - ``AtAll``        @所有人，本质是发送一条消息
    - ``AddNewFriend`` 服务器搜索 + 好友申请 RPC

其余只读 / 本地 UI 操作（``GetAllMessage``、``ChatWith``、``SwitchToChat`` 等）
直接继承父类，不加延迟。

设计结论（同步阻塞 vs 异步等待）
--------------------------------
延迟在本子类内用**同步阻塞** ``time.sleep`` 实现，异步只属于 adapter 层：

* 本类继承自同步的 ``WeChat``，重写方法必须保持同步签名——``AtAll`` 内部会调用
  ``self.SendMsg(...)``，``SendMsg/SendFiles/AtAll`` 内部会调用
  ``self.ChatWith/self._show``，都是同步调用链；改成 ``async`` 会破坏父类契约与
  内部调用，且同步方法体内也无法 ``await``。
* 底层 uiautomation/COM 调用本身就是阻塞的，在该层用异步延迟没有收益。
* hermes gateway 是全异步（``BasePlatformAdapter.send/connect/disconnect`` 均为
  ``async def``）。**正确的桥接位置在 adapter**：应通过
  ``await asyncio.to_thread(...)``（理想情况下固定到一个已 ``CoInitialize`` 的
  单一 worker 线程）把"随机延迟 + 阻塞 UI 操作"整体丢到线程池，避免冻结事件循环。

即：**wxauto 层同步阻塞，adapter 层异步桥接。**
"""

import os
import sys
import time
import random
from contextlib import contextmanager

# 内层 wxauto 包位于 plugins/wxauto/wxauto/wxauto/（其 __init__.py 导出 WeChat）。
# 基于 __file__ 注入其父目录，保持 vendored 库本体不被改动。
_PKG_PARENT = os.path.join(os.path.dirname(__file__), "wxauto")
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from wxauto_plugin.wxauto import WeChat


class ThrottledWeChat(WeChat):
    """在触发后端 RPC 的接口调用前插入随机延迟的 ``WeChat`` 子类。

    Args:
        min_delay (float, optional): 随机延迟下界（秒），默认 0.5
        max_delay (float, optional): 随机延迟上界（秒），默认 2.0
        *args, **kwargs: 透传给 ``WeChat.__init__``（如 language、debug）

    Example:
        >>> wx = ThrottledWeChat(min_delay=1, max_delay=3)
        >>> wx.SendMsg('hello', who='文件传输助手')  # 发送前出现 1~3 秒随机停顿
    """

    def __init__(self, *args, min_delay: float = 0.1, max_delay: float = 1.0, **kwargs):
        # 实例状态必须在 super().__init__() 之前初始化：父类构造过程会调用
        # GetAllMessage()（非 RPC 方法，不受影响），但先就绪可避免任何意外。
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._delay_depth = 0  # 重入计数，用于嵌套调用的延迟去重
        super().__init__(*args, **kwargs)

    @contextmanager
    def _throttled(self):
        """仅在最外层 RPC 调用前延迟一次，嵌套调用不叠加。"""
        if self._delay_depth == 0:
            time.sleep(random.uniform(self.min_delay, self.max_delay))
        self._delay_depth += 1
        try:
            yield
        finally:
            self._delay_depth -= 1

    # -- 封装的四个 RPC 接口（保持与父类一致的签名与默认值） ------------------
    def SendMsg(self, msg, who=None, clear=True, at=None):
        with self._throttled():
            return super().SendMsg(msg, who=who, clear=clear, at=at)

    def SendFiles(self, filepath, who=None):
        with self._throttled():
            return super().SendFiles(filepath, who=who)

    def AtAll(self, msg=None, who=None):
        with self._throttled():
            return super().AtAll(msg=msg, who=who)

    def AddNewFriend(self, keywords, addmsg=None, remark=None, tags=None):
        with self._throttled():
            return super().AddNewFriend(keywords, addmsg=addmsg, remark=remark, tags=tags)
