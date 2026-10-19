"""Local Cortex bridge. Requests arrive on stdin, never shell arguments.

Run with MEMORY_API_KEYS_FILE pointing at the private systemd ENV file.
Safe errors only; stdout may contain a newly generated secret and must never be
logged. There is no reveal operation for existing keys.
"""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.memory_api.api_keys import KeyRegistryError, list_keys, mutate_keys, verify_key


def main():
    try:
        path = Path(os.environ["MEMORY_API_KEYS_FILE"])
        request = json.loads(sys.stdin.read(16_384))
        if request.get("action") == "list":
            result = list_keys(path)
        elif request.get("action") == "verify":
            result = verify_key(path, request.get("keyId", ""))
        else:
            result = mutate_keys(path, request)
        print(json.dumps({"ok": True, "data": result}))
    except KeyRegistryError as exc:
        print(json.dumps({"ok": False, "code": str(exc)}))
    except Exception:
        print(json.dumps({"ok": False, "code": "REGISTRY_UNAVAILABLE"}))


if __name__ == "__main__":
    main()
