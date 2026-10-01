#!/usr/bin/env python3
"""Small HTTP probe. Validates status AND body; uses no third-party packages."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--allow-failures", action="store_true",
                        help="Record expected disruption errors without exiting nonzero")
    args = parser.parse_args()
    if not args.url.startswith("http://") or not 1 <= args.concurrency <= 32 or args.requests < 1 or args.duration < 0:
        parser.error("HTTP URL, positive requests, concurrency 1..32 and nonnegative duration required")
    started = time.monotonic()
    deadline = started + args.duration

    def worker(index):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        results = []
        count = len(range(index, args.requests, args.concurrency))
        while (time.monotonic() < deadline) if args.duration else (len(results) < count):
            begin = time.monotonic()
            error = ""
            try:
                with opener.open(args.url, timeout=3) as response:
                    if response.status != 200 or response.read().strip() != b"Jasmin Deployment PoC OK":
                        error = "Unexpected status/body"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = str(exc)
            results.append((time.monotonic() - begin, error))
            if args.duration:
                time.sleep(0.05)
        return results

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = [r for batch in pool.map(worker, range(args.concurrency)) for r in batch]
    errors = [error for _, error in results if error]
    output = dict(url=args.url, requests=len(results), concurrency=args.concurrency,
                  failures=len(errors), error_rate=len(errors) / len(results) if results else 1,
                  elapsed_seconds=round(time.monotonic() - started, 3),
                  max_latency_seconds=round(max((r[0] for r in results), default=0), 3),
                  error_examples=list(dict.fromkeys(errors))[:5])
    print(json.dumps(output, ensure_ascii=False))
    return 0 if results and (not errors or args.allow_failures) else 1


if __name__ == "__main__":
    raise SystemExit(main())
