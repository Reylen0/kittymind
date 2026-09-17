"""截图工具。使用 mss 库截取屏幕，保存到本地文件并返回路径。"""

import time
from pathlib import Path

from pydantic import BaseModel, Field

from ..base import BaseTool, ToolResult
from ...config import cfg


def _unique_path(directory: Path) -> Path:
    """生成不撞名的截图路径。

    旧实现用 f"screenshot_{int(time.time())}.png"——同一秒内连拍两张会直接覆盖，
    模型拿到两条「已保存」提示却只剩一个文件（且内容与第一次不符）。
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = directory / f"screenshot_{stamp}.png"
    seq = 1
    while path.exists():
        path = directory / f"screenshot_{stamp}_{seq}.png"
        seq += 1
    return path


def _prune(directory: Path, keep: int) -> int:
    """按修改时间保留最新 keep 张，返回清理数量（keep<=0 表示不清理）。

    只动本工具自己产出的 `screenshot_*.png`（模式严格限定），
    用户放进该目录的其它文件一律不碰。
    """
    if keep <= 0:
        return 0
    try:
        shots = sorted(
            directory.glob("screenshot_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for old in shots[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


class ScreenshotToolParam(BaseModel):
    monitor: int = Field(
        default_factory=lambda: cfg.SCREENSHOT_MONITOR,
        description="截取的显示器编号，1 = 主显示器，0 = 全部合并",
    )
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

    def execute(self, parameters: ScreenshotToolParam) -> ToolResult:
        try:
            import mss
            import mss.tools
        except ImportError:
            return ToolResult(False, "错误: 请先安装 mss 库：uv add mss")

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
                return ToolResult(False, f"错误: 截图失败 — {e}")

            png_bytes = mss.tools.to_png(screenshot.rgb, screenshot.size)

        cfg.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        save_path = _unique_path(cfg.SCREENSHOTS_DIR)
        save_path.write_bytes(png_bytes)
        _prune(cfg.SCREENSHOTS_DIR, cfg.SCREENSHOT_KEEP_MAX)

        w, h = screenshot.size
        size_kb = len(png_bytes) / 1024
        return ToolResult(True, (
            f"截图已保存: {save_path}\n"
            f"尺寸: {w}×{h}  文件大小: {size_kb:.0f}KB"
        ))
