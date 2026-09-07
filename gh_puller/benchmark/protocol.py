"""Define the shared endpoint routes and response schema for benchmarks."""

ASK_PATH = "/ask"
OPENAPI_PATH = "/openapi.json"
RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "minLength": 1}},
    "additionalProperties": True,
}
