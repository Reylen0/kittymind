"""截图工具。使用 mss 库截取屏幕，返回 base64 编码的 PNG。"""

import base64
import io

from pydantic import BaseModel, Field

from baseagent.tools.base import BaseTool


class ScreenshotToolParam(BaseModel):
    monitor: int = Field(default=0, description="截取的显示器编号，0 = 全部显示器合并，1 = 主显示器")
    left: int = Field(default=0, description="截取区域左边界（像素），与 width/height 配合使用")
    top: int = Field(default=0, description="截取区域上边界（像素）")
    width: int = Field(default=0, description="截取区域宽度（像素），0 = 截取整个显示器")
    height: int = Field(default=0, description="截取区域高度（像素），0 = 截取整个显示器")


class ScreenshotTool(BaseTool):
    """屏幕截图工具"""

    name: str = "screenshot"
    description: str = (
        "截取屏幕并返回 base64 编码的 PNG 图片。"
        "可截取全屏（默认）或通过 left/top/width/height 指定区域。"
        "monitor=0 截取所有显示器合并，monitor=1 截取主显示器。"
    )
    param_class = ScreenshotToolParam

    def execute(self, parameters: ScreenshotToolParam) -> str:
        try:
            import mss
            import mss.tools
        except ImportError:
            return "错误: 请先安装 mss 库：uv add mss"

        with mss.mss() as sct:
            monitors = sct.monitors  # monitors[0] = 全屏, monitors[1] = 主显示器

            if parameters.width > 0 and parameters.height > 0:
                region = {
                    "left": parameters.left,
                    "top": parameters.top,
                    "width": parameters.width,
                    "height": parameters.height,
                }
            else:
                idx = max(0, min(parameters.monitor, len(monitors) - 1))
                region = monitors[idx]

            try:
                screenshot = sct.grab(region)
            except Exception as e:
                return f"错误: 截图失败 — {e}"

            # 转为 PNG bytes
            png_bytes = mss.tools.to_png(screenshot.rgb, screenshot.size)

        b64 = base64.b64encode(png_bytes).decode("ascii")
        w, h = screenshot.size
        return f"data:image/png;base64,{b64}\n[截图尺寸: {w}×{h}]"
