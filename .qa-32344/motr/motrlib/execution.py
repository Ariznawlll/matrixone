"""Supervise isolated case processes and schedule resource-disjoint work."""
import multiprocessing as mp
import os
import signal
import threading
import time
import traceback
from collections import deque
from multiprocessing.connection import wait

from .diagnostics import bound_event, bound_result


class DiagnosticSender:
    """Best-effort diagnostic delivery that never blocks case execution."""
    def __init__(self, pipe, max_events=256, sample_limit=256,
                 sample_bytes=512 * 1024):
        self.pipe = pipe
        self.max_events = max_events
        self.items = deque()
        self.condition = threading.Condition()
        self.stopping = False
        self.dropped = 0
        self.sample_limit = sample_limit
        self.sample_bytes = sample_bytes
        self.sample_count = 0
        self.sample_size = 0
        self.sample_truncated = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def emit(self, message):
        message = bound_event(message)
        with self.condition:
            if self.stopping:
                self.dropped += 1
                return
            if message.get('type') == 'script_output':
                size = len(str(message.get('text', '')).encode('utf-8'))
                if (self.sample_count >= self.sample_limit
                        or self.sample_size + size > self.sample_bytes):
                    self.dropped += 1
                    if not self.sample_truncated:
                        self.sample_truncated = True
                        if len(self.items) >= self.max_events:
                            self.items.popleft()
                            self.dropped += 1
                        self.items.append(bound_event(dict(
                            type='diagnostic_truncated', stream='script_output')))
                        self.condition.notify()
                    return
                self.sample_count += 1
                self.sample_size += size
            if len(self.items) >= self.max_events:
                self.items.popleft()
                self.dropped += 1
            self.items.append(message)
            self.condition.notify()

    def _run(self):
        try:
            while True:
                with self.condition:
                    while not self.items and not self.stopping:
                        self.condition.wait()
                    if not self.items:
                        return
                    message = self.items.popleft()
                self.pipe.send(message)
        except (BrokenPipeError, EOFError, OSError):
            return
        finally:
            self.pipe.close()

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify()
        self.thread.join(timeout=1)


def _child(payload, control_pipe, diagnostic_pipe, worker):
    # A case's scenario subprocesses belong to this group as well. The parent
    # can stop the complete local workload when the case exceeds its budget.
    if os.name == 'posix':
        os.setsid()
    control_lock = threading.Lock()
    diagnostics = DiagnosticSender(diagnostic_pipe)
    latest_operation = {}

    def emit(message):
        nonlocal latest_operation
        if message.get('type') in (
                'sql_start', 'connect_start', 'script_start', 'reap_start',
                'directive_start', 'send_start'):
            latest_operation = dict(message)
        if message.get('type') == 'first_error':
            with control_lock:
                control_pipe.send(message)
        else:
            diagnostics.emit(message)

    cfg = dict(payload[5], _event_sink=emit, _supervised_worker=True)
    try:
        with control_lock:
            control_pipe.send(dict(type='ready'))
        result = worker((*payload[:5], cfg))
        result.setdefault('last_operation', latest_operation)
        result.setdefault('diagnostics_dropped', diagnostics.dropped)
        config = payload[5]
        password = payload[1].get('password', '')
        with control_lock:
            control_pipe.send(dict(type='result', result=bound_result(
                result,
                max_bytes=int(config.get('result_payload_limit_bytes', 256 * 1024)),
                artifact_dir=config.get('_artifact_dir'), secrets=[password])))
    except Exception:
        try:
            with control_lock:
                control_pipe.send(dict(type='worker_error', traceback=traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        diagnostics.close()
        control_pipe.close()


def _stop(process, group_ready):
    if os.name == 'posix' and group_ready:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if process.is_alive():
            process.kill()
    elif os.name == 'posix':
        # The ready message may be in flight. Kill the worker first so it
        # cannot launch a new child, then reap any group it already created.
        if process.is_alive():
            process.kill()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    elif process.is_alive():
        process.kill()
    process.join(timeout=2)


def _group_exists(process, group_ready):
    if os.name != 'posix' or not group_ready:
        return process.is_alive()
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for_clean_exit(process, group_ready, grace=2):
    """Wait for both the worker and every ordinary group descendant."""
    process.join(timeout=grace)
    return not process.is_alive() and not _group_exists(process, group_ready)


def blocked_result(payload):
    case = payload[0]
    return dict(suite=case['suite'], name=case['name'], status='abort',
                test_file=case.get('test_file', ''), result_file=case.get('result_file', ''),
                failure_kind='blocked', cost_ms=0, stmts=0, ok=0, fail=1,
                reason='not executed: an earlier case left cleanup unverified')


def execute_phase(payloads, *, workers, resources, consume, event, timeout_for,
                  worker=None, heartbeat=30, mark_uncertain=None,
                  child_target=None):
    """Return False when a timeout/crash requires environment inspection.

    No new case is dispatched after an unclean termination. Already running
    cases retain their own deadlines, and all unstarted cases are reported as
    blocked rather than silently disappearing from the run.
    """
    if worker is None:
        from .runner import _worker_run
        worker = _worker_run
    child_target = child_target or _child
    ctx = mp.get_context('spawn')
    pending = [(p, resources(p)) for p in payloads]
    active = {}
    completed = 0
    safe = True
    next_heartbeat = time.monotonic() + heartbeat
    mark_uncertain = mark_uncertain or (lambda detail: None)

    def finish(pipe, result, *, unclean=False):
        nonlocal completed, safe
        job = active[pipe]
        uncertain = unclean or result.get('cleanup_required')
        if not uncertain and not _wait_for_clean_exit(job['process'], job['ready']):
            uncertain = True
            result.update(status='abort', failure_kind='surviving_process_group',
                          cleanup_required=True,
                          reason='worker returned but its local process group did not exit cleanly',
                          fail=max(1, result.get('fail', 0)))
        if uncertain:
            detail = dict(case=job['case'], reason=result.get('reason', 'unclean case exit'),
                          transition=result.get('failure_kind', 'uncertain'))
            # The durable caller callback runs before local termination.
            try:
                mark_uncertain(detail)
            finally:
                _stop(job['process'], job['ready'])
            safe = False
        job = active.pop(pipe)
        pipe.close()
        if job.get('diagnostic') is not None:
            job['diagnostic'].close()
        result.setdefault('last_operation', job['operation'])
        result.setdefault('first_error', job.get('first_error'))
        completed += 1
        consume(result)
        event(dict(type='case_end', case=job['case'], completed=completed,
                   total=len(payloads), status=result['status'],
                   cost_ms=result['cost_ms']))

    try:
        while pending or active:
            used = set().union(*(job['resources'] for job in active.values()))
            while safe and len(active) < workers:
                index = next((i for i, (_, rs) in enumerate(pending)
                              if rs.isdisjoint(used)), None)
                if index is None:
                    break
                payload, rs = pending.pop(index)
                receiver, sender = ctx.Pipe(duplex=False)
                diag_receiver, diag_sender = ctx.Pipe(duplex=False)
                process = ctx.Process(target=child_target,
                                      args=(payload, sender, diag_sender, worker))
                start = time.monotonic()
                limit = timeout_for(payload)
                name = f"{payload[0]['suite']}.{payload[0]['name']}"
                try:
                    process.start()
                except Exception as exc:
                    sender.close()
                    receiver.close()
                    diag_sender.close()
                    diag_receiver.close()
                    result = blocked_result(payload)
                    result.update(failure_kind='worker_start',
                                  reason=f'worker process failed to start: {exc}',
                                  cost_ms=int((time.monotonic()-start)*1000))
                    completed += 1
                    consume(result)
                    event(dict(type='case_end', case=name, completed=completed,
                               total=len(payloads), status=result['status'],
                               cost_ms=result['cost_ms']))
                    continue
                sender.close()
                diag_sender.close()
                active[receiver] = dict(process=process, start=start, limit=limit,
                                        resources=rs, case=name, payload=payload,
                                        operation={}, ready=False,
                                        diagnostic=diag_receiver)
                used.update(rs)
                event(dict(type='case_start', case=name, limit_seconds=limit,
                           completed=completed, total=len(payloads),
                           running=len(active), waiting=len(pending)))
            if not active:
                break
            watched = {}
            for control, job in active.items():
                watched[control] = (control, 'control')
                if job.get('diagnostic') is not None:
                    watched[job['diagnostic']] = (control, 'diagnostic')
            readable = wait(list(watched), timeout=.2)
            readable.sort(key=lambda ready: watched[ready][1] != 'control')
            for ready_pipe in readable:
                control, channel = watched[ready_pipe]
                for _ in range(64):
                    if control not in active:
                        break
                    job = active[control]
                    try:
                        msg = ready_pipe.recv()
                    except (EOFError, OSError):
                        if channel == 'diagnostic':
                            ready_pipe.close()
                            job['diagnostic'] = None
                            break
                        result = blocked_result(job['payload'])
                        failure_kind = 'worker_crash' if job['ready'] else 'partial_start'
                        result.update(failure_kind=failure_kind, cleanup_required=True,
                                      reason=job.get('worker_error', 'case worker exited without a result'),
                                      cost_ms=int((time.monotonic()-job['start'])*1000))
                        finish(control, result, unclean=True)
                        break
                    if channel == 'control':
                        if msg['type'] == 'ready':
                            job['ready'] = True
                        elif msg['type'] == 'result':
                            finish(control, msg['result'])
                            break
                        elif msg['type'] == 'worker_error':
                            job['worker_error'] = msg['traceback']
                        elif msg['type'] == 'first_error':
                            job.setdefault('first_error', msg)
                            event(dict(msg, case=job['case']))
                    else:
                        if msg['type'] == 'script_output':
                            job['operation']['last_output'] = msg.get('text', '')
                        if msg['type'] in ('sql_start', 'connect_start', 'script_start',
                                           'reap_start', 'directive_start', 'send_start'):
                            job['operation'] = dict(msg, started_monotonic=time.monotonic())
                        elif msg['type'] in ('sql_end', 'connect_end', 'reap_end',
                                             'directive_end'):
                            job['operation'] = dict(msg, completed=True)
                        event(dict(msg, case=job['case']))
                    if not ready_pipe.poll():
                        break
            now = time.monotonic()
            for pipe, job in list(active.items()):
                if now-job['start'] >= job['limit']:
                    result = blocked_result(job['payload'])
                    result.update(failure_kind='timeout', cleanup_required=True,
                                  reason=f"case exceeded {job['limit']:g}s; server cleanup must be verified",
                                  cost_ms=int((now-job['start'])*1000))
                    finish(pipe, result, unclean=True)
            if now >= next_heartbeat:
                event(dict(type='heartbeat', completed=completed, total=len(payloads),
                           waiting=len(pending), running=[
                               dict(case=j['case'], elapsed_seconds=round(now-j['start'], 1),
                                    operation=j['operation']) for j in active.values()]))
                next_heartbeat = now + heartbeat
        for payload, _ in pending:
            consume(blocked_result(payload))
    except BaseException as exc:
        if active:
            mark_uncertain(dict(
                case=','.join(job['case'] for job in active.values()),
                reason=f'runner interrupted: {type(exc).__name__}: {exc}',
                transition='parent_interruption'))
        raise
    finally:
        for pipe, job in active.items():
            _stop(job['process'], job['ready'])
            pipe.close()
            if job.get('diagnostic') is not None:
                job['diagnostic'].close()
    return safe
