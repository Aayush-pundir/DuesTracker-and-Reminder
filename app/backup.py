"""SQLite online backups (safe while the app is running) with simple retention."""
import glob, os, sqlite3, datetime
from .db import DB_PATH

def backup_dir():
    return os.environ.get("DUES_BACKUP_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups"))

def run(conn, keep=14, tag=""):
    d = backup_dir(); os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"dues-{datetime.datetime.now():%Y%m%d-%H%M%S}{tag}.db")
    dst = sqlite3.connect(path); conn.backup(dst); dst.close()
    files = sorted(glob.glob(os.path.join(d, "dues-*.db")))
    for f in files[:-keep]:
        os.remove(f)
    return path

def latest():
    f = sorted(glob.glob(os.path.join(backup_dir(), "dues-*.db")))
    return (os.path.basename(f[-1]), datetime.datetime.fromtimestamp(os.path.getmtime(f[-1])).isoformat(timespec="seconds")) if f else None
