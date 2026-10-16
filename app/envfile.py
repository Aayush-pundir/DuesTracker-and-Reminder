"""Optional config file for channel credentials: data/channels.env (git-ignored, never shown in the UI).
Format: KEY=VALUE per line, '#' comments. Real environment variables win over the file."""
import os

PATH = os.environ.get("DUES_ENV_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "channels.env"))

def load(path=None):
    path = path or PATH; n = 0
    if not os.path.isfile(path):
        return 0
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1); k = k.strip(); v = v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v; n += 1
    return n
