"""Response times of the running server's search (parity matrix B10), from a client in the VPN.

    python tests/bench/server_latency.py --base https://wissen.melchy.cloud --cookie-file ~/wr-session.txt

Reads only: ``GET /api/search`` with the session of a signed-in account. The session value comes from a
file (browser developer tools, cookie ``__Host-wr_session``), never from the command line, so it does not
end up in the shell history; delete the file afterwards and sign out, which ends the session. The rate
limit allows 120 searches per minute and account, so the default of 100 requests with 2 at a time stays
below it; answers 429 are counted separately and not timed. Prints median, P90, P95 and maximum in
milliseconds as JSON, plus the search mode the server reports. Search words are generic; no content of
the answers is printed.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORDS = ('Wartung', 'Rechnung', 'Vertrag', 'Projekt', 'Termin', 'Angebot', 'Protokoll', 'Bericht')


def percentile(values, share):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))]


def measure(base, session, query):
    if not base.startswith('https://'):
        raise ValueError('Nur über HTTPS.')
    url = f'{base}/api/search?' + urllib.parse.urlencode({'q': query})
    request = urllib.request.Request(url, headers={'Cookie': f'__Host-wr_session={session}',
                                                   'Accept': 'application/json'})
    start = time.perf_counter()
    try:
        # The scheme is checked above (bandit B310 warns about file: URLs).
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
            body = json.loads(response.read())
        return time.perf_counter() - start, 200, body.get('mode')
    except urllib.error.HTTPError as error:
        return None, error.code, None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--base', required=True, help='z. B. https://wissen.melchy.cloud')
    parser.add_argument('--cookie-file', required=True, help='Datei mit dem Wert von __Host-wr_session')
    parser.add_argument('--requests', type=int, default=100)
    parser.add_argument('--parallel', type=int, default=2)
    args = parser.parse_args(argv)
    session = Path(args.cookie_file).expanduser().read_text(encoding='ascii').strip()
    base = args.base.rstrip('/')
    measure(base, session, WORDS[0])  # warm-up, not counted
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        results = list(pool.map(lambda n: measure(base, session, WORDS[n % len(WORDS)]), range(args.requests)))
    times = [seconds * 1000 for seconds, status, _ in results if status == 200]
    statuses = {}
    for _, status, _ in results:
        statuses[str(status)] = statuses.get(str(status), 0) + 1
    report = {'anfragen': args.requests, 'parallel': args.parallel, 'status': statuses,
              'modus': sorted({mode for _, status, mode in results if status == 200 and mode}),
              'gemessen': len(times)}
    if times:
        report.update(median_ms=round(statistics.median(times), 1), p90_ms=round(percentile(times, 0.9), 1),
                      p95_ms=round(percentile(times, 0.95), 1), max_ms=round(max(times), 1))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if times else 1


if __name__ == '__main__':
    raise SystemExit(main())
