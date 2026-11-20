"""冒烟启动器：把库指到独立临时目录后再导入 app，避免污染工作区数据。"""
import pathlib
import sys

sys.path.insert(0, "/workspace/backend")

import app.db as db  # noqa: E402

tmp = pathlib.Path(sys.argv[1])
db.DATA_DIR = tmp
db.DB_PATH = tmp / "meterseal.db"

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=11000, log_level="warning")
