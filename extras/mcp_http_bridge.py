"""Generic HTTP bridge MCP server.

Exposes arbitrary HTTP endpoints as MCP tools, driven by a JSON config file.
Lets you give the AI access to your own APIs without writing any code - you
just describe the endpoint in JSON.

Usage:
    python mcp_http_bridge.py <path-to-tools.json>

Config format:
{
  "tools": [
    {
      "name": "get_weather",
      "description": "Get current weather for a city",
      "url": "https://api.example.com/weather",
      "method": "GET",
      "params": {
        "city": {"type": "string", "description": "City name", "required": true}
      },
      "query":  {"city": "{city}", "key": "YOUR_API_KEY"},
      "headers": {"X-Custom": "value"},
      "body":   null,
      "timeout": 20,
      "max_chars": 4000
    }
  ]
}

Templating: anywhere in url / query / headers / body, "{paramName}" is replaced
by the value the model supplied for that parameter.

Only the Python standard library plus the `mcp` package (already a project
dependency) is required.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    print(f"ERROR: the 'mcp' package is required: {exc}", file=sys.stderr)
    raise SystemExit(1)


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def render(template: Any, values: dict[str, Any]) -> Any:
    """Recursively substitute {param} placeholders with supplied values."""
    if isinstance(template, str):
        out = template
        for key, val in values.items():
            out = out.replace("{" + key + "}", "" if val is None else str(val))
        return out
    if isinstance(template, dict):
        return {k: render(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, values) for v in template]
    return template


def build_tool(spec: dict[str, Any]):
    """Create a callable that performs the HTTP request for one spec."""

    url = spec.get("url")
    method = str(spec.get("method", "GET")).upper()
    params = spec.get("params", {}) or {}
    timeout = int(spec.get("timeout", 20))
    max_chars = int(spec.get("max_chars", 4000))

    def call(**kwargs: Any) -> str:
        values = {k: v for k, v in kwargs.items() if v is not None}

        # Fail early with a helpful message if a required param is missing
        missing = [
            name for name, meta in params.items()
            if (meta or {}).get("required") and name not in values
        ]
        if missing:
            return f"Error: missing required parameter(s): {', '.join(missing)}"

        target = render(url, values)

        query = render(spec.get("query"), values) or {}
        if query:
            sep = "&" if urllib.parse.urlparse(target).query else "?"
            target = target + sep + urllib.parse.urlencode(query)

        headers = render(spec.get("headers"), values) or {}
        if "User-Agent" not in headers:
            headers["User-Agent"] = "open-llm-vtuber-http-bridge/1.0"

        data = None
        body = render(spec.get("body"), values)
        if body is not None and method in ("POST", "PUT", "PATCH"):
            if isinstance(body, (dict, list)):
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            else:
                data = str(body).encode("utf-8")

        req = urllib.request.Request(target, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            return f"HTTP {exc.code} {exc.reason}. {detail}"
        except Exception as exc:
            return f"Request failed: {type(exc).__name__}: {exc}"

        if len(raw) > max_chars:
            raw = raw[:max_chars] + f"\n...(truncated, {len(raw)} chars total)"

        # Pretty-print JSON when possible so the model reads it more easily
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)[:max_chars]
        except Exception:
            return raw

    return call


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python mcp_http_bridge.py <tools.json>", file=sys.stderr)
        return 2

    config_path = sys.argv[1]
    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"ERROR: cannot read config {config_path}: {exc}", file=sys.stderr)
        return 1

    specs = config.get("tools") or []
    if not specs:
        print(f"ERROR: no tools defined in {config_path}", file=sys.stderr)
        return 1

    server = FastMCP("http-bridge")

    for spec in specs:
        name = spec.get("name")
        if not name:
            print("WARNING: skipping tool without a name", file=sys.stderr)
            continue

        fn = build_tool(spec)
        fn.__name__ = name
        fn.__doc__ = spec.get("description", name)

        server.tool(name=name, description=spec.get("description", name))(fn)
        print(f"registered tool: {name}", file=sys.stderr)

    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
