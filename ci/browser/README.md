# Dashboard browser check

This independent Node package tests the real `apps/api/src/server.js` on a random
loopback port with `service: null`. It never uses deployment credentials. Chromium
loads the actual HTML, JavaScript and CSS, checks `/healthz`, and fails on page or
console errors, unknown UI structure, or unexpected network requests. Only GETs
to that server's four asset/health paths are allowed; service workers and
WebSockets are blocked.

The check explicitly supports the legacy `#create-view`/`#nav-runs` UI and the
new `#deploy-view`/`#review-panel` dashboard. The former checks source selection and
empty history. The latter checks selection, validation, review, edit, environment
selection, navigation and empty history/monitoring. Its deployment button must
remain disabled. Neither check fabricates a deployment, run, or successful result.

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

Failures write `failure.json`, a screenshot and a Playwright trace under a unique
`railshot-browser-*` directory in `CI_OUTPUT_DIR`, then `RUNNER_TEMP`, then the OS
temporary directory. CI can upload `${{ runner.temp }}/railshot-browser-*` on
failure. Passing checks discard the trace. These checks establish local browser
and asset compatibility; authenticated CI publication, cloud provisioning, Argo
sync and externally reachable applications need separate E2E evidence.
