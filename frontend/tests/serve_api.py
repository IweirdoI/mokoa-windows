"""Isolated HTTP fixture for frontend tests. No real LLM/embedding performance claims.

Run from repository root: .venv/Scripts/python frontend/tests/serve_api.py
All generated fixtures/state stay in ignored tmp/frontend-ingestion-* directories.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['OS_AGENT_MEMORY_BACKEND'] = 'memory'
os.environ['OS_AGENT_EMBEDDING_BACKEND'] = 'hash'

from src.common.config import load_default_config

(ROOT / 'tmp').mkdir(exist_ok=True)
out = Path(tempfile.mkdtemp(prefix='frontend-ingestion-', dir=ROOT / 'tmp'))
cfg = load_default_config()
cfg['storage']['staging_sqlite'] = str(out / 'staging.db')
cfg['storage']['staging_auto_purge_interval_seconds'] = 0
cfg['preference']['storage_dir'] = str(out / 'preferences')
cfg['preference']['use_llm'] = False
cfg['preference']['allow_rules'] = True
cfg['ingestion']['vision'] = 'off'
cfg['semantic_detection'] = {'enabled': False}
cfg['index_path'] = str(out / 'security.json')

from tests.test_ingestion_files import _docx_bytes, _pdf_bytes, _png_bytes

(out / 'guide.docx').write_bytes(_docx_bytes('麒麟系统安装依赖失败时，先检查网络，再切换清华镜像重试。'))
(out / 'guide.pdf').write_bytes(_pdf_bytes('Kylin Python installation guide: use pip with a mirror after a timeout.'))
(out / 'blank.png').write_bytes(_png_bytes())
print(f'FIXTURE_DIR={out}', flush=True)

from api.server import app
import uvicorn

uvicorn.run(app, host='127.0.0.1', port=18086, log_level='warning')
