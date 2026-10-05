# Dashboard browser check

This existing Node test package loads the real dashboard and HTTP API on random loopback ports. It exercises the loginless shared workspace with mocked GitHub, CI and CD services; it does not use deployment credentials or call a cloud.

The unconfigured check verifies asset loading, source validation, navigation and disabled execution. The configured check submits GitHub, ZIP and folder sources without browser Authorization, distinguishes published images from deployed applications, verifies the final HTTPS link, and reloads the durable record without resubmitting the source.

The app and DB check keeps the original cloud/on-premises cards and uses local HTTP fixtures to verify DB topology, one reviewed plan bound to the app and target, required DB selection, no-DB profiles, cost display, and budget/mismatch/expiry rejection.

From the repository root (Node 22 or newer):

```sh
npm ci --ignore-scripts
npm ci --prefix ci/browser --ignore-scripts
npm exec --prefix ci/browser -- playwright install chromium
npm test --prefix ci/browser
```

To use an installed local Chrome:

```sh
CHROME_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm test --prefix ci/browser
```

Set `RAILSHOT_BROWSER_SCREENSHOT` to an absolute PNG path to keep the successful dashboard render. The Playwright version is locked in this package. These tests establish local browser and HTTP behavior. Container proxy checks, native executor tools, CI image publication, Argo sync and public cloud HTTP are verified separately.

Deployment progress regression coverage keeps the detail pane open through a running-to-success transition, verifies the service URL and shared stage labels, and preserves the selected pane. It also exercises recovery after a timed-out read, stale replies after changing deployments, and delayed metrics responses that must not block or overwrite durable completion. Active deployment records refresh every five seconds; terminal records back off to thirty seconds. These timings begin after a record response, not at the original CI event.
