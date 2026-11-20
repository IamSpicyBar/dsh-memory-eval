"""Shared model-transport adapter: prefer a host service, allow standalone use.

No evaluation policy lives here. The host retains credentials and enforces its
budget when HMBOT_MODEL_SERVICE is configured. There are no automatic retries.
"""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlparse

def messages_url(base):
    url = urlparse(base)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Model endpoint must be HTTPS without embedded credentials")
    value = base.rstrip("/")
    return value if value.endswith("/messages") else value + ("" if value.endswith("/v1") else "/v1") + "/messages"


def reserve_request():
    """Optional host-owned durable cap; failed attempts still consume a slot."""
    name = os.environ.get("HMBOT_MEMORY_MODEL_BUDGET")
    if not name:
        return
    import fcntl
    path = Path(name)
    with path.open("r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        budget = json.load(stream)
        if type(budget.get("limit")) is not int or type(budget.get("used")) is not int or budget["used"] >= budget["limit"]:
            raise ValueError("Model request budget exhausted; no implicit reset")
        budget["used"] += 1
        stream.seek(0); json.dump(budget, stream); stream.truncate(); stream.flush(); os.fsync(stream.fileno())


def call_model(base_url, model, prompt, max_tokens, temperature=0, thinking="off", timeout=180):
    endpoint = messages_url(base_url)
    service = os.environ.get("HMBOT_MODEL_SERVICE")
    if service:
        local = urlparse(service)
        if local.scheme != "http" or local.hostname != "127.0.0.1" or not local.port or local.username or local.password or local.query or local.fragment:
            raise ValueError("Host model service must be an explicit loopback endpoint")
        endpoint = service
        key = os.environ.get("HMBOT_MODEL_SERVICE_TOKEN")
    else:
        key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("Set a model credential explicitly for the selected endpoint")
    payload = {"model": model, "max_tokens": max_tokens, "temperature": temperature, "stream": False,
               "messages": [{"role": "user", "content": prompt}], "thinking": {"type": "disabled" if thinking == "off" else "enabled"}}
    data = json.dumps(payload).encode()
    if len(data) > 12 * 1024 * 1024:
        raise ValueError("Model request exceeds 12 MiB")
    if not service:
        reserve_request()
    request = urllib.request.Request(endpoint, data=data, method="POST", headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        # Redirects must never carry the model credential to another origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError("Model response exceeds 32 MiB")
    except urllib.error.HTTPError as error:
        code = error.code; error.close()
        raise ValueError("MODEL_REQUEST_FAILED_HTTP_" + str(code)) from None
    except (OSError, urllib.error.URLError):
        raise ValueError("MODEL_REQUEST_FAILED_TRANSPORT") from None
    value = json.loads(raw)
    if value.get("type") == "error" or not isinstance(value.get("content"), list):
        raise ValueError("MODEL_RESPONSE_INVALID")
    text = "\n".join(block["text"] for block in value["content"] if block.get("type") == "text" and isinstance(block.get("text"), str))
    return text, {"stop_reason": value.get("stop_reason"), "actualModel": value.get("model"), "requestedModel": model,
                  "usage": value.get("usage", {}), "responseId": value.get("id"), "transport": "studio-model-client/1",
                  "transportMode": "host-service" if service else "standalone"}
