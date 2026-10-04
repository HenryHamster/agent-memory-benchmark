"""Loopback-only Gemini gateway: persistent aggregate preflight reservations.

Only this process receives the real key, via stdin. Clients use a dummy key.
All retries pass through here. Unknown outcomes retain the worst-case reservation.
No tools, explicit caching, streaming, batch calls, or non-text input are accepted.
"""
import argparse
import json
import math
import pathlib
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = 'gemini-3.1-flash-lite'
INPUT_RATE = 0.25 / 1_000_000
OUTPUT_RATE = 1.50 / 1_000_000
MAX_USD = 4.50
MAX_REQUESTS = 600
MAX_INPUT = 2_000_000
MAX_OUTPUT = 2_000_000
MAX_BODY_BYTES = 1_000_000


class BudgetExceeded(Exception):
    pass


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, phase TEXT, role TEXT, started REAL, finished REAL, status TEXT, input INTEGER, output INTEGER, usd REAL, usage_json TEXT)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def totals(self):
        with self.connect() as c:
            n, inp, out, usd = c.execute('SELECT count(*),coalesce(sum(input),0),coalesce(sum(output),0),coalesce(sum(usd),0) FROM calls').fetchone()
        return dict(requests=n, input_tokens_charged_or_reserved=inp, output_tokens_charged_or_reserved=out, usd_charged_or_reserved=usd,
                    limits=dict(usd=MAX_USD, requests=MAX_REQUESTS, input_tokens=MAX_INPUT, output_tokens=MAX_OUTPUT))

    def reserve(self, phase, role, inp, out):
        usd = inp * INPUT_RATE + out * OUTPUT_RATE
        with self.lock, self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            n, si, so, cost = c.execute('SELECT count(*),coalesce(sum(input),0),coalesce(sum(output),0),coalesce(sum(usd),0) FROM calls').fetchone()
            if n + 1 > MAX_REQUESTS or si + inp > MAX_INPUT or so + out > MAX_OUTPUT or cost + usd > MAX_USD:
                raise BudgetExceeded('Pilot aggregate request/token/cost ceiling reached')
            return c.execute('INSERT INTO calls(phase,role,started,status,input,output,usd) VALUES(?,?,?,?,?,?,?)',
                             (phase, role, time.time(), 'reserved', inp, out, usd)).lastrowid

    def finish(self, call_id, status, usage=None):
        with self.lock, self.connect() as c:
            ri, ro = c.execute('SELECT input,output FROM calls WHERE id=?', (call_id,)).fetchone()
            if usage and 'promptTokenCount' in usage and 'totalTokenCount' in usage:
                inp = int(usage['promptTokenCount'])
                # totalTokenCount includes thinking; take the larger reported total.
                out = max(int(usage.get('candidatesTokenCount', 0)) + int(usage.get('thoughtsTokenCount', 0)),
                          int(usage.get('totalTokenCount', 0)) - inp)
                if inp > ri or out > ro:
                    raise RuntimeError('Provider usage exceeded reservation; manual audit required')
                c.execute('UPDATE calls SET input=?,output=?,usd=?,usage_json=? WHERE id=?',
                          (inp, out, inp*INPUT_RATE + out*OUTPUT_RATE, json.dumps(usage), call_id))
            # Errors and missing usage keep their full reservation, even HTTP 4xx.
            c.execute('UPDATE calls SET finished=?,status=? WHERE id=?', (time.time(), status, call_id))


def normalize(body, role):
    if set(body) - {'contents', 'systemInstruction', 'generationConfig', 'safetySettings'}:
        raise ValueError('Unsupported request keys; tools/cache/batch are disabled')
    for content in [*body.get('contents', []), body.get('systemInstruction', {})]:
        for part in content.get('parts', []):
            if set(part) != {'text'} or not isinstance(part['text'], str):
                raise ValueError('Only text parts are allowed')
    config = body.setdefault('generationConfig', {})
    if set(config) - {'temperature', 'topP', 'topK', 'maxOutputTokens', 'candidateCount', 'stopSequences', 'responseMimeType', 'responseSchema', 'responseJsonSchema', 'thinkingConfig', 'seed'}:
        raise ValueError('Unsupported generation configuration')
    cap = 8192 if role == 'memory' else 4096 if role == 'amb' else 64
    config['maxOutputTokens'] = min(int(config.get('maxOutputTokens', cap)), cap)
    if config['maxOutputTokens'] <= 0:
        raise ValueError('Output cap must be positive')
    config['candidateCount'] = 1
    config['thinkingConfig'] = {'thinkingLevel': 'minimal'}
    return body


def serve(root, key, port, transport='vertex-express'):
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'model-calls').mkdir(exist_ok=True)
    ledger = Ledger(root / 'budget.sqlite')
    phase = {'name': 'readiness'}
    halted = threading.Event()
    api = 'https://aiplatform.googleapis.com/v1' if transport == 'vertex-express' else 'https://generativelanguage.googleapis.com/v1beta'

    def upstream(path, body=None):
        if transport == 'vertex-express':
            path = path.replace('/models/', '/publishers/google/models/', 1)
            if path.endswith(':countTokens') and body and 'generateContentRequest' in body:
                # Vertex's count API takes contents directly rather than the
                # Developer API wrapper. The full generation body still determines
                # our conservative reservation, including schema and system text.
                generation = body['generateContentRequest']
                body = {k: v for k, v in generation.items() if k in ('contents', 'systemInstruction')}
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(api + path, data=data,
                   headers={'x-goog-api-key': key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == '/health':
                return self.send(200, {'ok': not halted.is_set(), 'model': MODEL, 'phase': phase['name'], **ledger.totals()})
            if self.path == '/models-check':
                if transport == 'vertex-express':
                    status, result = upstream('/models/' + MODEL + ':countTokens', {'contents': [{'role': 'user', 'parts': [{'text': 'availability probe'}]}]})
                    result = {'probe': 'countTokens', 'model': MODEL, 'transport': transport, **result}
                else:
                    status, result = upstream('/models/' + MODEL)
                (root / 'model-availability.json').write_text(json.dumps(result, indent=2))
                return self.send(status, result)
            self.send(404, {'error': 'Unknown path'})

        def do_POST(self):
            call_id = None
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY_BYTES:
                    raise ValueError('Request size outside pilot limits')
                body = json.loads(self.rfile.read(length))
                if self.path == '/phase':
                    phase['name'] = str(body['name'])[:150]
                    return self.send(200, {'phase': phase['name']})
                match = re.fullmatch(r'/(memory|amb|official)/v1beta/models/' + re.escape(MODEL) + r':generateContent', self.path)
                if not match:
                    raise ValueError('Model or API method is not allowed')
                if halted.is_set():
                    raise BudgetExceeded('Pilot is halted')
                role = match.group(1)
                body = normalize(body, role)
                # Count API is non-generative; it cannot spend output tokens.
                status, count = upstream('/models/' + MODEL + ':countTokens',
                                         {'generateContentRequest': {'model': 'models/' + MODEL, **body}})
                if status != 200:
                    return self.send(status, count)
                prompt_tokens = int(count['totalTokens'])
                # Reserve full serialized UTF-8 byte count + 2048, a deliberately
                # loose text-token ceiling; actual count is retained in each receipt.
                upper_input = max(prompt_tokens + 2048, len(json.dumps(body, ensure_ascii=False).encode()) + 2048)
                call_id = ledger.reserve(phase['name'], role, upper_input, body['generationConfig']['maxOutputTokens'])
                receipt = dict(call_id=call_id, phase=phase['name'], role=role, model=MODEL, count_tokens=count,
                               reserved_input=upper_input, request=body, started=time.time())
                path = root / 'model-calls' / f'{call_id:06d}.json'
                path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
                status, result = upstream('/models/' + MODEL + ':generateContent', body)
                receipt.update(http_status=status, response=result, elapsed_s=time.time()-receipt['started'])
                path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
                ledger.finish(call_id, str(status), result.get('usageMetadata') if status == 200 else None)
                (root / 'usage-summary.json').write_text(json.dumps(ledger.totals(), indent=2))
                return self.send(status, result)
            except (ValueError, BudgetExceeded) as exc:
                return self.send(400, {'error': {'code': 400, 'status': 'INVALID_ARGUMENT', 'message': str(exc)}})
            except Exception as exc:
                halted.set()
                if call_id is not None:
                    ledger.finish(call_id, 'uncertain-error')
                # Never log exception text: network libraries can echo headers.
                return self.send(400, {'error': {'code': 400, 'status': 'INVALID_ARGUMENT', 'message': 'Gateway halted: ' + type(exc).__name__}})

    print(json.dumps({'gateway': f'http://127.0.0.1:{port}', 'model': MODEL, 'key_present': bool(key), **ledger.totals()}), flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--port', type=int, default=8891)
    parser.add_argument('--api', choices=['vertex-express', 'gemini-developer'], default='vertex-express')
    args = parser.parse_args()
    secret = sys.stdin.readline().strip()
    if not secret:
        raise SystemExit('Missing Gemini key on stdin')
    serve(args.output, secret, args.port, args.api)
