from datetime import datetime
import zoneinfo

from pydantic import BaseModel, Field

from ..base import BaseTool


class GetCurrentTimeParam(BaseModel):
    timezone: str = Field(
        default="",
        description="IANA 时区名，如 Asia/Tokyo、America/New_York。不填则使用系统本地时区。",
    )


class GetCurrentTimeTool(BaseTool):
    name: str = "get_current_time"
    description: str = (
        "获取系统当前时间、日期和星期几。"
        "无需任何参数即可直接调用，返回本地时间。"
        "仅当用户明确要求其他时区时才传入 timezone 参数。"
    )
    param_class = GetCurrentTimeParam

    WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

    def execute(self, parameters: GetCurrentTimeParam) -> str:
        if parameters.timezone:
            try:
                tz = zoneinfo.ZoneInfo(parameters.timezone)
            except zoneinfo.ZoneInfoNotFoundError:
                return f"未知时区: {parameters.timezone}"
            now = datetime.now(tz)
            tz_label = parameters.timezone
        else:
            now = datetime.now().astimezone()
            offset = now.strftime("%z")
            tz_label = f"UTC{offset[:3]}:{offset[3:]}"

        return (
            f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"({self.WEEKDAYS[now.weekday()]})\n"
            f"时区: {tz_label}"
        )
