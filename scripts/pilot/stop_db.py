"""Stop only the current run's named database; preserve its data."""
import json
import os
import pathlib
from pg0 import Pg0

directory = pathlib.Path(os.environ['PILOT_RUN_DIR'])
config = json.loads((directory / 'config.json').read_text())
pg = Pg0(name=config['run_id'], port=config['database']['port'], username='hindsight', password='local-pilot-only',
         database='hindsight', data_dir=config['database']['path'])
if pg.running:
    pg.stop()
(directory / 'shutdown.json').write_text(json.dumps(dict(database_running=pg.running, database_preserved=True)))
