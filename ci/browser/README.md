# Dashboard browser check

This independent Node package tests the real `apps/api/src/server.js` on random
loopback ports. It never uses operator deployment credentials. Chromium loads the
actual HTML, JavaScript and CSS. The unconfigured-server check allows only the
four asset/health paths, verifies source selection, review and navigation, and
confirms that submission is blocked.

The configured-server check uses an in-memory deployment service and GitHub
source loader behind the real HTTP upload adapter. It submits repository, ZIP and
folder sources, checks target selection and remote health responses that hide the
target ID, and verifies Bearer header handling without storing credentials. It
covers duplicate submissions, authentication failure, publication vs deployment,
unsafe result links, failed status lookup, manual retry, and polling termination
on completion, manual stop and page exit. The browser clock advances polling
without real-time waits. All network access stays on the test server; unexpected
requests and WebSockets fail the check. The removed legacy dashboard is no longer
a supported UI alternative.

From the repository root (Node 22 or newer):

```sh
npm ci --prefix apps/api --ignore-scripts
npm ci --prefix ci/browser --ignore-scripts
npm run --prefix ci/browser install:browser
npm test --prefix ci/browser
```

The package is separate from the dashboard/root workspace. Its exact Playwright
version and transitive dependencies are locked in `package-lock.json`. CI installs
Playwright's Chromium. A local installed Chrome can be used without downloading a
browser by omitting `install:browser` and running:

```sh
CHROME_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  npm test --prefix ci/browser
```

The unconfigured-server check writes `failure.json`, a screenshot and a Playwright
trace on failure under a unique
`railshot-browser-*` directory in `CI_OUTPUT_DIR`, then `RUNNER_TEMP`, then the OS
temporary directory. CI can upload `${{ runner.temp }}/railshot-browser-*` on
failure. Passing checks discard the trace. These checks establish local browser
and asset compatibility; authenticated CI publication, cloud provisioning, Argo
sync and externally reachable applications need separate E2E evidence.
