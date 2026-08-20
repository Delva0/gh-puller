"""协议契约(代码化):ask 路由与响应校验的单一权威定义,调用方与服务方共用。"""

ASK_PATH = "/ask"  # 唯一路由
OPENAPI_PATH = "/openapi.json"  # 路由声明的读取入口
RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "minLength": 1}},
    "additionalProperties": True,  # 容忍未知字段,保证协议前向兼容
}
