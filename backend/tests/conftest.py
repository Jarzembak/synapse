import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

for name in ("_lib", "_media", "_db", "_logs"):
    shutil.rmtree(HERE / name, ignore_errors=True)

os.environ["LIBRARY_DIR"] = str(HERE / "_lib")
os.environ["MEDIA_DIR"] = str(HERE / "_media")
os.environ["DB_PATH"] = str(HERE / "_db" / "test.sqlite3")
os.environ["LOG_DIR"] = str(HERE / "_logs")
os.environ["LOG_NAME"] = "test"
