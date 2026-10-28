"""Local replacement for the GitHub Actions matrix (seven jobs).

The GitHub Actions matrix cannot run without a paid budget. This script runs the same seven jobs
on local machines and records one result file per job and commit:

    windows-3.11  windows-3.12  windows-3.13  linux-3.11  linux-3.12  linux-3.13  postgres

Each machine runs the jobs of its own platform:

    python tests/local_ci.py run                       # 3.11, 3.12, 3.13 of this platform
    python tests/local_ci.py run --postgres --pg-bin /usr/lib/postgresql/18/bin
    python tests/local_ci.py report                    # gate protocol for HEAD

``run`` refuses a working tree with uncommitted changes, because a result must belong to exactly
one commit. Results land in ``.local-ci/results/<commit>/<job>.json``; copy the files of the other
machine into the same folder before ``report``. ``report`` writes ``local-ci-gate-<commit>.md``
and exits with 0 only when all seven jobs passed on that commit.

Interpreters: ``LOCAL_CI_PY311`` (``..._PY312``, ``..._PY313``) override the search, otherwise
``uv python find``, the Windows ``py`` launcher and ``python3.X`` on PATH are tried in this order.

Tests write their temporary files to ``.local-ci/tmp/<job>``. Exclude this folder from on-access
virus scanning: a scanner that opens new files there makes cleanup and file-list assertions flaky.

Windows only: the development machine sometimes fails to delete temporary folders after a test
has already passed (``WinError 145``). A run whose only errors are of this kind repeats exactly
those tests once. The repetition is recorded in the result and in the protocol; any other error,
and any failure, fails the job immediately.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.local-ci'
VERSIONS = ('3.11', '3.12', '3.13')
JOBS = tuple(f'{system}-{version}' for system in ('windows', 'linux') for version in VERSIONS) + ('postgres',)
POSTGRES_PYTHON = '3.13'
CLEANUP_ERROR = 'WinError 145'
SEPARATOR = re.compile(r'^={50,}$', re.MULTILINE)
PROBLEM_HEAD = re.compile(r'^(ERROR|FAIL): \S+ \(([\w.]+)\)', re.MULTILINE)
RAN = re.compile(r'^Ran (\d+) tests? in', re.MULTILINE)
SUMMARY = re.compile(r'^(OK|FAILED)(?: \(([^)]*)\))?\s*$', re.MULTILINE)


# ---------------------------------------------------------------- parsing (tested without subprocesses)

def parse_unittest_output(text):
    """Counts and problem list from the text that ``python -m unittest`` writes to stderr.

    Returns a dict with ``ran``, ``ok``, ``failures``, ``errors``, ``skipped`` and ``problems``
    (a list of ``{'kind', 'test', 'cleanup_only'}``). ``ran`` is None when the run did not reach
    its summary, for example after an import error or a crash.
    """
    ran_match = RAN.findall(text)
    summaries = SUMMARY.findall(text)
    counts = {'failures': 0, 'errors': 0, 'skipped': 0, 'expected failures': 0, 'unexpected successes': 0}
    ok = False
    if summaries:
        verdict, detail = summaries[-1]
        ok = verdict == 'OK'
        for part in filter(None, (item.strip() for item in (detail or '').split(','))):
            key, _, value = part.partition('=')
            if key.strip() in counts and value.strip().isdigit():
                counts[key.strip()] = int(value)
    problems = []
    for block in SEPARATOR.split(text)[1:]:
        head = PROBLEM_HEAD.search(block)
        if head:
            problems.append({'kind': head.group(1), 'test': head.group(2),
                             'cleanup_only': head.group(1) == 'ERROR' and CLEANUP_ERROR in block})
    return {'ran': int(ran_match[-1]) if ran_match else None, 'ok': ok and bool(ran_match),
            'failures': counts['failures'], 'errors': counts['errors'], 'skipped': counts['skipped'],
            'unexpected_successes': counts['unexpected successes'], 'problems': problems}


def retry_candidates(parsed, system):
    """Test ids that may be repeated once, or [] when the run must fail as it is.

    ponytail: one repetition masks the Windows temp-folder race instead of finding which handle
    stays open. Ceiling: a real leak that shows up as WinError 145 would pass after the retry.
    Upgrade path: close the leaking handle in the affected tests, then delete this rule.
    """
    problems = parsed['problems']
    if system != 'windows' or parsed['ran'] is None or parsed['failures'] or not problems:
        return []
    if len(problems) != parsed['errors'] or not all(problem['cleanup_only'] for problem in problems):
        return []
    return [problem['test'] for problem in problems]


def job_passed(result):
    """A job passes only with a complete run, no open problem and no unexpected success."""
    return bool(result.get('ran')) and result.get('status') == 'PASS'


def gate_table(commit, results):
    """Markdown protocol for one commit; second value is True when all seven jobs passed."""
    lines = ['# Lokales Matrix-Gate Wissensraum-AI', '',
             f'- Commit: `{commit}`',
             f'- Erstellt: {time.strftime("%d.%m.%Y %H:%M:%S")}',
             '- Ersetzt die GitHub-Actions-Matrix (7 Jobs), siehe `tests/local_ci.py`', '',
             '| Job | Ergebnis | Tests | Übersprungen | Wiederholt | System | Dauer |',
             '|---|---|---:|---:|---:|---|---:|']
    passed = True
    for job in JOBS:
        result = results.get(job)
        if result is None:
            passed = False
            lines.append(f'| {job} | FEHLT | | | | | |')
            continue
        ok = job_passed(result)
        passed = passed and ok
        lines.append('| {job} | {status} | {ran} | {skipped} | {retried} | {system} | {duration:.0f} s |'.format(
            job=job, status='PASS' if ok else 'FAIL', ran=result.get('ran') or 0,
            skipped=result.get('skipped', 0), retried=len(result.get('retried', [])),
            system=result.get('system', ''), duration=result.get('seconds', 0)))
    lines += ['', f'**Gesamtergebnis: {"PASS, 7 von 7 Jobs grün" if passed else "FAIL"}**', '']
    retried = [(job, test) for job in JOBS for test in (results.get(job) or {}).get('retried', [])]
    if retried:
        lines += ['## Wiederholte Tests (nur WinError 145 beim Aufräumen)', '']
        lines += [f'- {job}: `{test}`' for job, test in retried] + ['']
    skips = {job: (results.get(job) or {}).get('skip_reasons', {}) for job in JOBS}
    if any(skips.values()):
        lines += ['## Übersprungene Tests', '', 'SKIP ist kein bestandener Test.', '']
        for job, reasons in skips.items():
            for reason, count in sorted(reasons.items()):
                lines.append(f'- {job}: {count} × {reason}')
        lines.append('')
    return '\n'.join(lines), passed


def skip_reasons(verbose_text):
    """Count of skipped tests per reason from ``unittest -v`` output."""
    reasons = {}
    for reason in re.findall(r"""\.\.\. skipped (?:'(.*)'|"(.*)")$""", verbose_text, re.MULTILINE):
        reason = reason[0] or reason[1]  # repr() switches to double quotes when the text has an apostrophe
        reasons[reason] = reasons.get(reason, 0) + 1
    return reasons


# ---------------------------------------------------------------- environment

def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def head_commit():
    return git('rev-parse', '--short=7', 'HEAD')


def working_tree_clean():
    return git('status', '--porcelain', '--untracked-files=normal') == ''


def system_name():
    return 'windows' if os.name == 'nt' else 'linux'


def find_python(version):
    override = os.environ.get('LOCAL_CI_PY' + version.replace('.', ''))
    if override:
        return override
    if shutil.which('uv'):
        found = subprocess.run(['uv', 'python', 'find', version], capture_output=True, text=True, check=False)
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    if os.name == 'nt' and shutil.which('py'):
        found = subprocess.run(['py', f'-{version}', '-c', 'import sys;print(sys.executable)'],
                               capture_output=True, text=True, check=False)
        if found.returncode == 0:
            return found.stdout.strip()
    return shutil.which(f'python{version}')


def venv_python(version, base):
    """Fresh-enough virtual environment with requirements-ocr.txt for one Python version."""
    target = STATE / 'venv' / f'{system_name()}-{version}'
    exe = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    stamp = target / 'requirements.stamp'
    wanted = (ROOT / 'requirements-ocr.txt').read_text() + (ROOT / 'requirements.txt').read_text()
    if not exe.exists():
        subprocess.run([base, '-m', 'venv', str(target)], check=True)
    if not stamp.exists() or stamp.read_text() != wanted:
        subprocess.run([str(exe), '-m', 'pip', 'install', '-q', '--disable-pip-version-check',
                        '-r', str(ROOT / 'requirements-ocr.txt')], check=True)
        stamp.write_text(wanted)
    return str(exe)


def run_logged(command, log, env=None, timeout=3600):
    started = time.monotonic()
    with open(log, 'w', encoding='utf-8', errors='replace') as handle:
        completed = subprocess.run(command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                   timeout=timeout, check=False)
    return completed.returncode, Path(log).read_text(encoding='utf-8', errors='replace'), time.monotonic() - started


# ---------------------------------------------------------------- jobs

def compile_all(python, log):
    files = [name for name in git('ls-files', '*.py').splitlines() if name]
    code, text, _ = run_logged([python, '-m', 'py_compile', *files], log)
    return code == 0, len(files), text


def unittest_job(job, python, extra_env, logs, modules=None):
    # Own temp folder per job: one folder to exclude from on-access virus scanning, which holds
    # handles and adds shadow files in %TEMP% on the development machine (WinError 145).
    temp = STATE / 'tmp' / job
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'TMP': str(temp), 'TEMP': str(temp), 'TMPDIR': str(temp), **extra_env}
    command = [python, '-m', 'unittest', *(modules or ['discover', '-s', 'tests']), '-v']
    if system_name() == 'linux' and modules is None:
        if not shutil.which('xvfb-run'):
            raise SystemExit('xvfb-run fehlt: die Oberflächentests brauchen einen virtuellen Bildschirm')
        command = ['xvfb-run', '-a', *command]
    code, text, seconds = run_logged(command, logs / f'{job}.log', env)
    parsed = parse_unittest_output(text)
    retried = []
    candidates = retry_candidates(parsed, system_name())
    if candidates and modules is None:
        retry_env = {**env, 'PYTHONPATH': str(ROOT / 'tests')}
        retry_code, retry_text, retry_seconds = run_logged([python, '-m', 'unittest', *candidates, '-v'],
                                                           logs / f'{job}-retry.log', retry_env)
        seconds += retry_seconds
        retry_parsed = parse_unittest_output(retry_text)
        if retry_code == 0 and retry_parsed['ok'] and retry_parsed['ran'] == len(candidates):
            retried = candidates
            parsed = {**parsed, 'ok': True, 'errors': 0, 'problems': []}
    status = 'PASS' if parsed['ok'] and not parsed['unexpected_successes'] and (code == 0 or retried) else 'FAIL'
    return {'job': job, 'status': status, 'ran': parsed['ran'], 'skipped': parsed['skipped'],
            'failures': parsed['failures'], 'errors': parsed['errors'], 'problems': parsed['problems'],
            'retried': retried, 'skip_reasons': skip_reasons(text), 'seconds': round(seconds, 1)}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def disposable_postgres(pg_bin):
    """Start a throw-away cluster; returns (environment for the tests, stop function)."""
    pg_bin = Path(pg_bin)
    data = Path(tempfile.mkdtemp(prefix='local-ci-pg-'))
    password = secrets.token_urlsafe(18)
    port = free_port()
    pwfile = data.parent / f'{data.name}.pw'
    pwfile.write_text(password)
    try:
        created = subprocess.run([str(pg_bin / 'initdb'), '-D', str(data / 'cluster'), '-U', 'wissensraum_test',
                                  '--auth=scram-sha-256', f'--pwfile={pwfile}', '-E', 'UTF8', '--locale=C'],
                                 capture_output=True, text=True, check=False)
    finally:
        pwfile.unlink(missing_ok=True)
    if created.returncode:  # initdb refuses root: run the postgres job as an unprivileged user
        shutil.rmtree(data, ignore_errors=True)
        raise SystemExit('postgres: initdb fehlgeschlagen: ' + (created.stderr.strip() or created.stdout.strip()))
    subprocess.run([str(pg_bin / 'pg_ctl'), '-D', str(data / 'cluster'), '-l', str(data / 'server.log'), '-w',
                    '-o', f'-p {port} -k {data} -c listen_addresses=127.0.0.1', 'start'],
                   check=True, capture_output=True)
    env = {**os.environ, 'PGPASSWORD': password}
    subprocess.run([str(pg_bin / 'createdb'), '-h', '127.0.0.1', '-p', str(port), '-U', 'wissensraum_test',
                    'wissensraum_test'], check=True, capture_output=True, env=env)

    def stop():
        subprocess.run([str(pg_bin / 'pg_ctl'), '-D', str(data / 'cluster'), '-m', 'fast', '-w', 'stop'],
                       capture_output=True, check=False)
        shutil.rmtree(data, ignore_errors=True)

    test_env = {'PGTEST_HOST': '127.0.0.1', 'PGTEST_PORT': str(port), 'PGTEST_USERNAME': 'wissensraum_test',
                'PGTEST_PASSWORD': password, 'PGTEST_DATABASE': 'wissensraum_test',
                'PGTEST_ALLOW_DESTRUCTIVE': '1', 'PGTEST_PG_BIN': str(pg_bin), 'PGTEST_REQUIRE_PG_TOOLS': '1'}
    version = subprocess.run([str(pg_bin / 'postgres'), '--version'],
                             capture_output=True, text=True, check=False).stdout.strip()
    return test_env, stop, version


def command_run(args):
    if not working_tree_clean() and not args.allow_dirty:
        raise SystemExit('Arbeitsverzeichnis enthält nicht eingecheckte Änderungen. '
                         'Ein Gate-Ergebnis muss genau zu einem Commit gehören (--allow-dirty nur zum Ausprobieren).')
    commit = head_commit() + ('-dirty' if not working_tree_clean() else '')
    results_dir = STATE / 'results' / commit
    logs = STATE / 'logs' / commit
    results_dir.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    system = system_name()
    versions = [] if args.only_postgres else (args.pythons.split(',') if args.pythons else list(VERSIONS))
    exit_code = 0
    planned = [(f'{system}-{version}', version) for version in versions]
    if args.postgres or args.only_postgres:
        planned.append(('postgres', POSTGRES_PYTHON))
    for job, version in planned:
        base = find_python(version)
        if not base:
            print(f'{job}: Python {version} nicht gefunden (LOCAL_CI_PY{version.replace(".", "")} setzen)')
            exit_code = 1
            continue
        python = venv_python(version, base)
        info = subprocess.run([python, '-c', 'import sys,platform;print(sys.version.split()[0], platform.platform())'],
                              capture_output=True, text=True, check=True).stdout.strip()
        if job == 'postgres':
            if not args.pg_bin:
                raise SystemExit('postgres: --pg-bin mit dem bin-Ordner von PostgreSQL 18 angeben')
            env, stop, pg_version = disposable_postgres(args.pg_bin)
            try:
                result = unittest_job(job, python, env, logs, modules=['tests.test_postgres'])
            finally:
                stop()
            info += f', {pg_version}'
            if result['skipped']:  # the contract must really run against the server
                result['status'] = 'FAIL'
            compiled = (True, 0)
        else:
            ok, count, _ = compile_all(python, logs / f'{job}-compile.log')
            compiled = (ok, count)
            extra = {'WISSENSRAUM_REQUIRE_OCR': '1'} if system == 'linux' else {}
            result = unittest_job(job, python, extra, logs)
            if not ok:
                result['status'] = 'FAIL'
        result.update({'commit': commit, 'system': info, 'compiled_files': compiled[1], 'compile_ok': compiled[0],
                       'finished': time.strftime('%Y-%m-%dT%H:%M:%S')})
        (results_dir / f'{job}.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        print(f'{job}: {result["status"]} · {result["ran"]} Tests · {result["skipped"]} übersprungen'
              f' · {len(result["retried"])} wiederholt · {info}')
        if result['status'] != 'PASS':
            exit_code = 1
    return exit_code


def command_report(args):
    commit = args.commit or head_commit()
    folder = STATE / 'results' / commit
    results = {}
    for job in JOBS:
        path = folder / f'{job}.json'
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('commit') == commit:
                results[job] = data
    text, passed = gate_table(commit, results)
    target = Path(args.output) if args.output else ROOT / f'local-ci-gate-{commit}.md'
    target.write_text(text, encoding='utf-8')
    print(text)
    return 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Jobs dieser Plattform ausführen')
    run.add_argument('--pythons', help='z. B. 3.11,3.13 (Standard: 3.11,3.12,3.13)')
    run.add_argument('--postgres', action='store_true', help='zusätzlich den PostgreSQL-Job ausführen')
    run.add_argument('--only-postgres', action='store_true', help='nur den PostgreSQL-Job ausführen')
    run.add_argument('--pg-bin', help='bin-Ordner von PostgreSQL 18 (initdb, pg_ctl, pg_dump, pg_restore, psql)')
    run.add_argument('--allow-dirty', action='store_true', help='nur zum Ausprobieren, nicht gate-gültig')
    report = commands.add_parser('report', help='Gate-Protokoll für einen Commit schreiben')
    report.add_argument('--commit', help='Standard: HEAD')
    report.add_argument('--output')
    args = parser.parse_args(argv)
    return command_run(args) if args.command == 'run' else command_report(args)


if __name__ == '__main__':
    sys.exit(main())
