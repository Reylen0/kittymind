"""提示词模板模块"""

from .template import PromptTemplate
from .hub import SUMMARIZE_PROMPT_TEMPLATE, DEFAULT_SUPERVISOR_PROMPT

__all__ = [
    "PromptTemplate",
    "SUMMARIZE_PROMPT_TEMPLATE",
    "DEFAULT_SUPERVISOR_PROMPT",
]
