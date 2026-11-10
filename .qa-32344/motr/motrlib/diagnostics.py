"""Runtime diagnostics are separate from the SQL golden-output stream."""
import re
import json
import time
import os
from collections import defaultdict
from pathlib import Path


class OutputLimitExceeded(RuntimeError):
    def __init__(self, limit):
        self.limit = limit
        super().__init__(f'case output exceeded {limit} bytes')


class DiagnosticStorageFull(RuntimeError):
    pass


class LimitedLineBuffer(list):
    """List-compatible line buffer whose rendered UTF-8 size is bounded."""
    def __init__(self, max_bytes=64 * 1024 * 1024):
        super().__init__()
        self.max_bytes = max_bytes
        self.used_bytes = 0

    def append(self, value):
        value = str(value)
        size = len(value.encode('utf-8')) + 1
        if self.used_bytes + size > self.max_bytes:
            raise OutputLimitExceeded(self.max_bytes)
        super().append(value)
        self.used_bytes += size

    def extend(self, values):
        for value in values:
            self.append(value)


def safe_text(text: str, limit=2000) -> str:
    # Diagnostics need SQL structure, not literal values or credentials.
    text = re.sub(r"(?s)'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"", "?", str(text))
    text = re.sub(r"(?i)(password|passwd|pwd|token|secret)(\s*[:=]\s*)\S+", r"\1\2<redacted>", text)
    text = re.sub(r"(?i)(identified\s+by\s+)\S+", r"\1<redacted>", text)
    return text if limit is None else text[:limit]


def _bounded_structure(value):
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {str(key): _bounded_structure(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bounded_structure(item) for item in value]
    return value


def _redacted_structure(value, secrets=()):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '<redacted>')
        return safe_text(value, limit=None)
    if isinstance(value, dict):
        return {str(key): _redacted_structure(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted_structure(item, secrets) for item in value]
    return value


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def bound_event(event, max_bytes=8192):
    value = _bounded_structure(dict(event))
    if _encoded_size(value) <= max_bytes:
        return value
    for key in ('text', 'last_output', 'traceback', 'message', 'sql', 'running'):
        if key in value:
            value[key] = '<truncated>'
            value['truncated'] = True
            if _encoded_size(value) <= max_bytes:
                return value
    minimal = {key: value[key] for key in ('type', 'case', 'phase', 'time') if key in value}
    minimal['truncated'] = True
    if _encoded_size(minimal) > max_bytes:
        minimal = {'type': 'diagnostic_truncated', 'truncated': True}
    return minimal


def bound_result(result, max_bytes=262144, artifact_dir=None, secrets=()):
    full_value = _redacted_structure(dict(result), secrets)
    if _encoded_size(full_value) <= max_bytes:
        return full_value
    artifact = None
    if artifact_dir:
        root = Path(artifact_dir)
        root.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r'[^A-Za-z0-9_.-]+', '-',
                      f"{result.get('suite', 'case')}-{result.get('name', 'result')}")
        artifact = root / f'{stem}-{os.getpid()}.json'
        artifact.write_text(json.dumps(full_value, ensure_ascii=False, indent=2),
                            encoding='utf-8')
    value = _bounded_structure(full_value)
    if artifact:
        value['detail_artifact'] = str(artifact)
    if _encoded_size(value) <= max_bytes:
        value['details_truncated'] = True
        return value
    for key in ('actual_output', 'diff', 'traceback', 'reason', 'last_operation'):
        if key in value:
            value[key] = '<truncated>'
            value['details_truncated'] = True
            if _encoded_size(value) <= max_bytes:
                return value
    required = ('suite', 'name', 'status', 'cost_ms', 'stmts', 'ok', 'fail',
                'failure_kind', 'cleanup_required', 'first_error')
    minimal = {key: value[key] for key in required if key in value}
    if artifact:
        minimal['detail_artifact'] = str(artifact)
    minimal['details_truncated'] = True
    if _encoded_size(minimal) > max_bytes:
        minimal.pop('first_error', None)
    if _encoded_size(minimal) > max_bytes:
        minimal = dict(suite=str(result.get('suite', ''))[:64],
                       name=str(result.get('name', ''))[:128],
                       status=result.get('status', 'abort'), details_truncated=True)
    return minimal


class Journal:
    """Append and flush each event/result; never wait for the full run."""
    def __init__(self, root, secrets=(), event_limit=256 * 1024 * 1024,
                 result_limit=1024 * 1024 * 1024):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.secrets = [s for s in secrets if s]
        self.phase = 'initializing'
        self.results = []
        self.last_script_console = {}
        self.event_limit = event_limit
        self.result_limit = result_limit
        self.event_bytes = 0
        self.result_bytes = 0
        self.diagnostic_storage_full = False

    def _clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, '<redacted>')
            return value
        if isinstance(value, dict):
            return {k: self._clean(v) for k,v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        return value

    def _append(self, name, value, limit, used):
        encoded = json.dumps(self._clean(value), ensure_ascii=False) + '\n'
        size = len(encoded.encode('utf-8'))
        if used + size > limit:
            return None
        with (self.root / name).open('a', encoding='utf-8') as file:
            file.write(encoded)
            file.flush()
        return used + size

    def event(self, event):
        event = bound_event(self._clean(dict(event, phase=self.phase, time=time.time())))
        written = self._append('events.jsonl', event, self.event_limit, self.event_bytes)
        if written is None:
            self.diagnostic_storage_full = True
            return
        self.event_bytes = written
        kind = event['type']
        if kind == 'script_output':
            name = event.get('case', '')
            now = time.monotonic()
            if now-self.last_script_console.get(name, float('-inf')) < 5:
                return
            self.last_script_console[name] = now
        if kind in ('case_start', 'case_end', 'heartbeat', 'phase_start', 'phase_end', 'script_output'):
            print('PROGRESS | ' + json.dumps(event, ensure_ascii=False), flush=True)

    def result(self, result):
        result = self._clean(result)
        used = self.result_bytes
        artifact = result.get('detail_artifact')
        if artifact:
            artifact_path = Path(artifact).resolve()
            try:
                artifact_path.relative_to(self.root.resolve())
            except ValueError:
                raise DiagnosticStorageFull('result artifact is outside the report directory')
            if artifact_path.exists():
                used += artifact_path.stat().st_size
        written = self._append('results.jsonl', result, self.result_limit, used)
        if written is None:
            raise DiagnosticStorageFull(
                f'results.jsonl exceeded {self.result_limit} bytes')
        self.result_bytes = written
        self.results.append(result)
        # A small index is rewritten atomically so interruption cannot leave
        # half a JSON document. Full logs remain append-only.
        suites = defaultdict(lambda: dict(cases=0, cumulative_case_ms=0))
        for item in self.results:
            if item.get('failure_kind') != 'blocked':
                suites[item['suite']]['cases'] += 1
                suites[item['suite']]['cumulative_case_ms'] += item['cost_ms']
        blocked = sum(r.get('failure_kind') == 'blocked' for r in self.results)
        summary = dict(completed=len(self.results)-blocked, blocked=blocked, phase=self.phase,
                       suites=dict(suites),
                       failures=sum(r['status'] in ('fail', 'abort') for r in self.results),
                       diagnostic_storage_full=self.diagnostic_storage_full,
                       slowest=sorted([
                           dict(case=f"{r['suite']}.{r['name']}", cost_ms=r['cost_ms'], status=r['status'])
                           for r in self.results], key=lambda r:r['cost_ms'], reverse=True)[:20])
        tmp = self.root / 'progress.json.tmp'
        tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.root / 'progress.json')
