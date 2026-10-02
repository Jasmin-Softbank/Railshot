# Dashboard browser check

This existing Node test package loads the real dashboard and HTTP API on random loopback ports. It exercises the loginless shared workspace with mocked GitHub, CI and CD services; it does not use deployment credentials or call a cloud.

The unconfigured check verifies asset loading, source validation, navigation and disabled execution. The configured check submits GitHub, ZIP and folder sources without browser Authorization, distinguishes published images from deployed applications, verifies the final HTTPS link, and reloads the durable record without resubmitting the source.

The environment form check uses local HTTP fixtures to verify that profile counts become the approved DB placement, one reviewed plan binds the app name and target to the combined deployment, required DB profiles cannot select no DB, and optional/no-DB profiles retain their choices. It also checks environment-only preparation, existing-target builds, and plan mismatch/expiry rejection.

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
