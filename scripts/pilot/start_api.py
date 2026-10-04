"""Start the pinned editable Hindsight fork and its own embedded database."""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / '.pilot'
RUN = pathlib.Path(os.environ['PILOT_RUN_DIR'])
config = json.loads((RUN / 'config.json').read_text())
os.environ.update(config['hindsight_environment'])
os.environ['HF_HOME'] = str(STATE / 'hf-cache')
os.environ['HINDSIGHT_API_RERANKER_FLASHRANK_CACHE_DIR'] = str(STATE / 'flashrank-cache')
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['OMP_NUM_THREADS'] = '4'
from pg0 import Pg0

db = config['database']
pg = Pg0(name=config['run_id'], port=db['port'], username='hindsight', password='local-pilot-only',
         database='hindsight', data_dir=db['path'])
pathlib.Path(db['path']).mkdir(parents=True, exist_ok=True)
try:
    if not pg.running:
        pg.start()
    os.environ['HINDSIGHT_API_DATABASE_URL'] = pg.uri
    import hindsight_api
    (RUN / 'implementation.json').write_text(json.dumps(dict(module_path=hindsight_api.__file__, executable=sys.executable,
         source_revision='f7dd3f4fd7420f7beec60c32c965e5e5cf7be066', install='editable HenryHamster fork source'), indent=2))
    from hindsight_api.main import main
    sys.argv = ['hindsight-api', '--host', '127.0.0.1', '--port', str(config['api_port']), '--no-access-log']
    main()
finally:
    if pg.running:
        pg.stop()
