"""One local command: private key -> services -> real checks -> saved evidence -> shutdown."""
import argparse
import datetime
import getpass
import hashlib
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / '.pilot'
SCRIPTS = ROOT / 'scripts/pilot'


def save(path, value):
    path.write_text(json.dumps(value, indent=2), encoding='utf-8')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def child_environment(config, directory):
    env = os.environ.copy()
    # Only the gateway receives the real key. Ignore .env files and Vertex SDK
    # mode overrides so all paid callers must traverse the local budget guard.
    for key in list(env):
        if key.startswith(('HINDSIGHT_API_', 'HINDSIGHT_HTTP_', 'OMB_ANSWER_', 'OMB_JUDGE_')):
            env.pop(key)
    for key in ['GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_VERTEX_BASE_URL', 'OPENAI_API_KEY', 'GROQ_API_KEY', 'ANTHROPIC_API_KEY']:
        env.pop(key, None)
    env.update(PYTHON_DOTENV_DISABLED='1', GOOGLE_GENAI_USE_VERTEXAI='false', PILOT_RUN_DIR=str(directory),
               GEMINI_API_KEY='pilot-gateway-placeholder', GOOGLE_API_KEY='pilot-gateway-placeholder')
    env.update(config['amb_environment'])
    return env


def wait_ready(process, url, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Service exited; inspect gateway.log / hindsight.log')
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                result = json.load(response)
                if result.get('status') == 'healthy' or result.get('ok'):
                    return result
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('Service readiness timeout; inspect the saved logs')


def stop(process):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def admit_complete_case(usage):
    # First measured case: 257 calls, ~1.03M input tokens, $0.384. Require
    # 50% headroom before admitting another complete history, not halfway through.
    limits = usage['limits']
    if (limits['requests'] - usage['requests'] < 390 or
        limits['input_tokens'] - usage['input_tokens_charged_or_reserved'] < 1_550_000 or
        limits['output_tokens'] - usage['output_tokens_charged_or_reserved'] < 130_000 or
        limits['usd'] - usage['usd_charged_or_reserved'] < 0.65):
        raise RuntimeError('Remaining shared budget cannot admit a complete case with retry headroom')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', choices=['vertex-express', 'gemini-developer'], default='vertex-express',
                        help='Vertex API key (default) or Google AI Studio / Gemini Developer API key')
    parser.add_argument('--check', action='store_true', help='Start and verify local API/database only; no key or paid calls')
    parser.add_argument('--smoke-only', action='store_true', help='Real two-document isolation test; omit the benchmark case')
    parser.add_argument('--key-stdin', action='store_true', help='Automation: read one secret line from stdin instead of prompting')
    args = parser.parse_args()
    for name in ['amb-venv', 'hindsight-venv', 'hindsight', 'LongMemEval']:
        if not (STATE / name).exists():
            parser.error('Setup is incomplete; run bash scripts/pilot/setup.sh')
    for name, expected in [('hindsight', 'f7dd3f4fd7420f7beec60c32c965e5e5cf7be066'), ('LongMemEval', '9e0b455f4ef0e2ab8f2e582289761153549043fc')]:
        actual = subprocess.check_output(['git', '-C', str(STATE / name), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != expected or subprocess.check_output(['git', '-C', str(STATE / name), 'status', '--porcelain'], text=True).strip():
            parser.error(f'{name} differs from the tested pin; preserve your edits and use a clean setup')
    secret = ''
    if not args.check:
        secret = sys.stdin.readline().strip() if args.key_stdin else (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY') or getpass.getpass('Gemini API key (hidden; never saved): '))
        if not secret.strip():
            parser.error('No API key entered')
    run_id = 'pilot-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    directory = STATE / 'runs' / run_id
    directory.mkdir(parents=True)
    config = json.loads((SCRIPTS / 'defaults.json').read_text())
    api_port, gateway_port, db_port = free_port(), free_port(), free_port()
    while len({api_port, gateway_port, db_port}) != 3:
        api_port, gateway_port, db_port = free_port(), free_port(), free_port()
    config.update(run_id=run_id, api_port=api_port, hindsight_url=f'http://127.0.0.1:{api_port}',
                  gateway=f'http://127.0.0.1:{gateway_port}', gateway_upstream=args.api,
                  database=dict(port=db_port, path=f'/var/tmp/amb-pilot-{os.getuid()}/{run_id}/postgres'))
    config['hindsight_environment']['GOOGLE_GEMINI_BASE_URL'] = config['gateway'] + '/memory'
    if args.check:
        config['hindsight_environment']['HINDSIGHT_API_SKIP_LLM_VERIFICATION'] = 'true'
    config['amb_environment'].update(HINDSIGHT_HTTP_URL=config['hindsight_url'], GOOGLE_GEMINI_BASE_URL=config['gateway'] + '/amb')
    # The accounting directory is shared across all runs of this checkout; reruns
    # never silently reset the cap. The override is for an existing shared ledger.
    accounting = pathlib.Path(os.environ.get('PILOT_ACCOUNTING_DIR', str(STATE / 'accounting'))).resolve()
    config['accounting_directory'] = str(accounting)
    save(directory / 'config.json', config)
    save(directory / 'source.json', dict(amb_sha=subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
         amb_diff=subprocess.check_output(['git', '-C', str(ROOT), 'diff', 'HEAD', '--', 'src/memory_bench', 'pyproject.toml', 'scripts/pilot'], text=True),
         helper_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in SCRIPTS.iterdir() if p.is_file()}))
    env = child_environment(config, directory)
    api = gateway = evaluation = None
    started = time.monotonic()
    print(f'Run: {run_id}\nEvidence: {directory}', flush=True)
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if not args.check:
            with (directory / 'gateway.log').open('w') as log:
                gateway = subprocess.Popen([str(STATE / 'amb-venv/bin/python'), str(SCRIPTS / 'budget_gateway.py'),
                     '--output', str(accounting), '--port', str(gateway_port), '--api', args.api], env=env,
                     stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            gateway.stdin.write(secret.strip() + '\n')
            gateway.stdin.close()
            secret = ''
            before = wait_ready(gateway, config['gateway'] + '/health', 30)
            save(directory / 'usage-before.json', before)
            if not args.smoke_only:
                admit_complete_case(before)
            with urllib.request.urlopen(config['gateway'] + '/models-check', timeout=30) as response:
                save(directory / 'model-availability.json', json.load(response))
            req = urllib.request.Request(config['gateway'] + '/phase', data=json.dumps({'name': run_id + '/startup'}).encode(),
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req) as response:
                response.read()
        print('Starting pinned Hindsight source and embedded Postgres (cold start can take several minutes)...', flush=True)
        with (directory / 'hindsight.log').open('w') as log:
            api = subprocess.Popen([str(STATE / 'hindsight-venv/bin/python'), str(SCRIPTS / 'start_api.py')],
                                   env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        save(directory / 'readiness.json', wait_ready(api, config['hindsight_url'] + '/health/ready', 600))
        print('API and database ready.', flush=True)
        if not args.check:
            stages = ['smoke'] if args.smoke_only else ['smoke', 'case']
            for stage in stages:
                print(f'Running {stage}; progress is saved in {directory}.', flush=True)
                evaluation = subprocess.Popen([str(STATE / 'amb-venv/bin/python'), str(SCRIPTS / 'evaluate.py'), stage], env=env,
                                              start_new_session=True)
                if evaluation.wait():
                    raise RuntimeError(f'{stage} failed; inspect the saved logs and budget ledger')
            with urllib.request.urlopen(config['gateway'] + '/health') as response:
                after = json.load(response)
                save(directory / 'usage.json', dict(aggregate=after, this_run_requests=after['requests']-before['requests'],
                     this_run_usd=after['usd_charged_or_reserved']-before['usd_charged_or_reserved']))
        save(directory / 'completion.json', dict(passed=True, check_only=args.check, smoke_only=args.smoke_only,
                                                elapsed_s=time.monotonic()-started))
        print(f'PASS. Inspect {directory}. Services are stopping.', flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        save(directory / 'failure.json', dict(error_type=type(exc).__name__, elapsed_s=time.monotonic()-started))
        print(f'Run stopped ({type(exc).__name__}); inspect {directory}.', flush=True)
        raise SystemExit(1) from None
    finally:
        # Kill paid access first on errors/interruptions, then stop our children.
        stop(gateway)
        stop(evaluation)
        stop(api)
        # Also handles an API killed before its finally block ran.
        subprocess.run([str(STATE / 'hindsight-venv/bin/python'), str(SCRIPTS / 'stop_db.py')], env=env, check=True)


if __name__ == '__main__':
    main()
