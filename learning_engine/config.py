"""Small UTF-8 .env reader; no expansion, execution or process-env mutation."""
from pathlib import Path
import re


def read_env(path=".env", required=False):
    path = Path(path)
    if not path.exists():
        if required:
            raise ValueError(f"Environment file not found: {path}")
        return {}
    result = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env assignment at {path}:{number}")
        if value.startswith(("'", '"')):
            match = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", value)
            if not match:
                raise ValueError(f"Invalid .env quoting at {path}:{number}")
            value = match[2]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        result[key] = value
    return result
