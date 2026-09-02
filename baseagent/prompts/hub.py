from .template import PromptTemplate

SUMMARIZE_PROMPT_TEMPLATE = PromptTemplate(
    "你是一个对话摘要助手，你的任务是根据用户和助手之间的对话内容生成一个简明扼要的摘要。请确保摘要准确地反映了对话的主要内容和关键信息。"
    "请将以下历史摘要和对话生成新的摘要：\n"
    "历史摘要: {history_summary}\n"
    "对话: {conversation}"
    )

DEFAULT_SUPERVISOR_PROMPT = "你是一个任务调度器,根据用户请求和可用 Agent 列表,使用 route_tool 将请求路由到最合适的子 Agent。"