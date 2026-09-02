"""截图工具。使用 mss 库截取屏幕，保存到本地文件并返回路径。"""

import time
from pathlib import Path

from pydantic import BaseModel, Field

from ..base import BaseTool

_SAVE_DIR = Path.home() / ".kittymind" / "screenshots"


class ScreenshotToolParam(BaseModel):
    monitor: int = Field(default=1, description="截取的显示器编号，1 = 主显示器，0 = 全部合并")
    left: int = Field(default=0, description="截取区域左边界（像素），与 width/height 配合使用")
    top: int = Field(default=0, description="截取区域上边界（像素）")
    width: int = Field(default=0, description="截取区域宽度（像素），0 = 截取整个显示器")
    height: int = Field(default=0, description="截取区域高度（像素），0 = 截取整个显示器")


class ScreenshotTool(BaseTool):
    """屏幕截图工具"""

    name: str = "screenshot"
    description: str = (
        "截取屏幕并将图片保存到本地文件，返回文件路径和尺寸信息。"
        "默认截取主显示器全屏（monitor=1）；"
        "可通过 left/top/width/height 截取指定区域。"
    )
    param_class = ScreenshotToolParam

    def execute(self, parameters: ScreenshotToolParam) -> str:
        try:
            import mss
            import mss.tools
        except ImportError:
            return "错误: 请先安装 mss 库：uv add mss"

        with mss.mss() as sct:
            monitors = sct.monitors

            if parameters.width > 0 and parameters.height > 0:
                region = {
                    "left": parameters.left, "top": parameters.top,
                    "width": parameters.width, "height": parameters.height,
                }
            else:
                idx = max(0, min(parameters.monitor, len(monitors) - 1))
                region = monitors[idx]

            try:
                screenshot = sct.grab(region)
            except Exception as e:
                return f"错误: 截图失败 — {e}"

            png_bytes = mss.tools.to_png(screenshot.rgb, screenshot.size)

        _SAVE_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"screenshot_{int(time.time())}.png"
        save_path = _SAVE_DIR / filename
        save_path.write_bytes(png_bytes)

        w, h = screenshot.size
        size_kb = len(png_bytes) / 1024
        return (
            f"截图已保存: {save_path}\n"
            f"尺寸: {w}×{h}  文件大小: {size_kb:.0f}KB"
        )
