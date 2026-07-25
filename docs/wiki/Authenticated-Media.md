# Authenticated media

Synapse can ingest media that `yt-dlp` is allowed to access only after a
browser sign-in. This includes many recordings and courses on Zoom, Udemy, X,
and other supported platforms.

Authentication does not bypass DRM, payment, enrollment, approval, regional
restrictions, or the content owner's download policy. Use it only for material
you are authorized to access and retain.

## Sign in while creating a project

1. Open **Projects** and select a URL media source.
2. Paste the source URL.
3. Select **Open an isolated sign-in browser after creating**.
4. Create the project.
5. Complete the platform's normal sign-in, registration, MFA, or approval flow
   in the browser window that opens.
6. Return to Synapse and select **Use this sign-in**, or **Save & start
   ingest**.

If the browser blocks the new window, allow pop-ups for the local Synapse
address and retry from the project's **Source access** card.

## Sign in from an existing project

Open a URL media project and find **Source access**:

- **Sign in to source** starts a fresh disposable Chromium session.
- **Open sign-in browser** returns to a session already in progress.
- **Use this sign-in** saves source access without starting a job.
- **Save & start ingest** saves access and queues audio ingestion.
- **Sign in again** replaces access that has expired.
- **Clear sign-in** removes the project's cookie jar and captured authorized
  URL.
- **Cancel** destroys an unfinished browser session without replacing saved
  access.

Only one interactive sign-in can run at a time. Media jobs and source-access
changes are mutually fenced so a download cannot read a half-written cookie
jar.

## What Synapse retains

The sign-in browser uses a temporary profile. Credentials, password-manager
entries, browser history, extensions, local storage, and the profile itself are
destroyed when the session is saved, canceled, or expires.

Synapse retains only:

- cookies applicable to the submitted source and its final authorized page;
- the final authorized URL when a provider places a short-lived access token
  in it;
- the controlled browser's User-Agent; and
- a referer pointing at the submitted source.

These values live under `data/media/<project>/` with the project's working
media. They are mode-restricted where the host filesystem supports it, are
removed with the project, and are excluded from Synapse backups and cloud
sync. They are still credentials: anyone who can read the host's data
directory may be able to reuse them.

The URL you originally submit remains in the project's SQLite row so Synapse
can retry it. Database backups therefore contain that exact URL; if it is a
signed link, enable backup encryption and protect the backup key. Synapse never
copies source cookies into generated Markdown, artifact metadata, or logs.
Signed source URLs appear there only as their origin (for example,
`https://sans.zoom.us`) plus a SHA-256 digest used for staleness checks. Synapse
also removes arbitrary paths because providers may place bearer credentials in
path segments.

## Browser sidecar

Docker Compose starts a pinned, concurrency-one Selenium Chromium container:

- WebDriver and noVNC have no published host ports.
- Synapse relays the viewer under its own origin with a high-entropy token
  bound to the project and browser session. The WebSocket also validates its
  exact browser origin against `SYNAPSE_PUBLIC_ORIGIN`; it never derives trust
  from a client-controlled `Host` or `X-Forwarded-Host` header.
- The viewer route is excluded from frontend and API access logs because its
  short-lived capability is part of the URL.
- Saving, canceling, expiry, or project deletion revokes the viewer and closes
  attached relays. The Selenium container restarts after every session, so an
  old viewer cannot observe the next sign-in.
- The browser lives on a separate network containing no Synapse application
  services. Chromium is forced through a filtering proxy that permits only
  public HTTP/HTTPS destinations and pins connections to vetted DNS answers.
- URL-driven yt-dlp metadata, captions, and downloads use that same boundary.
  Saved cookies, browser headers, and authorized URLs fail closed if the proxy
  is unavailable, so a DNS change cannot redirect credentials to a private
  address.
- A fixed-purpose bridge exposes only WebDriver and noVNC back to the API.
- That bridge and the API share a dedicated internal control network. No
  worker, broker, model server, or frontend container can reach its WebDriver
  or read browser cookies.
- No database, library, media, Redis, Docker socket, or host browser profile is
  mounted into the browser container.
- The profile is ephemeral and Selenium reaps abandoned sessions.

The first `docker compose up` downloads a large Chromium image. Docker caches
it for later starts.

Relevant `.env` settings are:

```dotenv
SYNAPSE_PUBLIC_ORIGIN=http://localhost:8080
AUTH_BROWSER_SESSION_MINUTES=30
AUTH_BROWSER_SESSION_SECONDS=1800
MEDIA_EGRESS_CONNECTION_TIMEOUT_SECONDS=21600
```

`SYNAPSE_PUBLIC_ORIGIN` is the one exact browser origin used to open Synapse:
scheme, host, and non-default port, with no path. Use `https://` for a
TLS-terminated deployment. Requests to the authenticated-media controls pass
through the bundled frontend, which supplies this configured value to the API;
the API rejects direct, missing-origin, and cross-origin mutations.

Keep `AUTH_BROWSER_SESSION_SECONDS` at least as large as
`AUTH_BROWSER_SESSION_MINUTES × 60`; the former is Selenium's hard reap timer
and the latter is Synapse's project-session expiry.
`MEDIA_EGRESS_CONNECTION_TIMEOUT_SECONDS` is separate so a long authorized
course or recording download can continue after the interactive login browser
has been destroyed.

Compose supplies `MEDIA_EGRESS_PROXY_URL` internally. If you run the API and
worker without Compose and ingest URL media, run the guarded proxy separately
and set that variable to its HTTP proxy URL.
All URL retrieval is intentionally refused when `ALLOW_PRIVATE_URLS=false` and
no guarded proxy is configured. Setting `ALLOW_PRIVATE_URLS=true` is an
explicit opt-out for trusted local-development sources.

The viewer uses the same address as Synapse; there is no second browser port to
forward or expose. Synapse still has no multi-user authentication. A
deliberately remote deployment must put the entire application behind access
control and TLS.

## Manual cookies.txt fallback

The **Advanced fallback** section accepts a Mozilla/Netscape-format
`cookies.txt` file up to 2 MiB. Uploading a manual jar discards any older final
URL captured by the interactive browser so the jar applies to the submitted
project URL.

Treat the file like a password. Browser-wide cookie export can include sessions
for unrelated sites; the interactive workflow is preferable because it asks
Chromium only for cookies applicable to this source.

See the
[yt-dlp cookie FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)
for the expected file format.

## Platform notes

### Zoom

`yt-dlp` supports normal Zoom `/rec/play/` and `/rec/share/` cloud-recording
URLs. A recording passcode is a separate yt-dlp input and is not the same as a
Zoom account sign-in.

Zoom on-demand recordings may first require name/email registration, approval,
or an emailed access link. Finish that flow in the isolated browser. If Zoom
emails a new link, paste that link into the same isolated browser, open it, and
confirm that the recording actually plays before selecting **Use this sign-in**.
Synapse can retain that verified recording page while it refreshes the original
submitted URL. Some Zoom registration flows can still fail if the current
yt-dlp Zoom extractor does not understand the resulting page.

### Udemy and other course platforms

Sign in to the account that owns the course and navigate back to the submitted
lecture before saving. Enrollment does not guarantee that the platform exposes
a non-DRM stream that yt-dlp can retain.

### X

Sign in and open the submitted post before saving. X may rotate sessions or
require related-domain cookies, so a later `401` or `403` usually means the
project needs a new sign-in.

### YouTube

Only authenticate when it is necessary. YouTube can rotate account cookies and
apply automated-download restrictions. Follow yt-dlp's current
[extractor guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors) and use
an account session conservatively.

## Limitations

Interactive sign-in cannot guarantee that yt-dlp can download a playable
stream. Common blockers include:

- DRM or owner-disabled downloads;
- passkey or hardware-security-key flows that do not work through remote
  Chromium;
- OAuth providers that reject automated browsers;
- a login flow that requires a nonstandard destination port (the isolated
  browser permits public web ports `80` and `443` only);
- access stored only in local storage rather than cookies or the final URL;
- partitioned cookies that cannot be represented in Netscape cookie format;
- TLS-fingerprint, device, IP, or rapidly expiring anti-bot tokens; and
- a platform change that requires an updated yt-dlp extractor.

The browser sidecar and media worker leave through the same Docker host, which
helps with IP-bound sessions. Capturing the browser User-Agent also helps, but
neither can reproduce every browser fingerprint.

## Troubleshooting

- **Interactive browser unavailable** — run `docker compose ps auth-browser`
  and inspect `docker compose logs auth-browser`.
- **Popup blocked** — allow pop-ups for `http://localhost:8080`, then retry.
- **Blank or disconnected browser view** — wait a few seconds and reopen it;
  the one-session browser container may still be restarting. Check
  `docker compose ps auth-browser auth-bridge auth-egress`.
- **Login saved but yt-dlp reports no formats** — confirm the recording plays
  in the isolated browser, update yt-dlp/Synapse, and check whether the source
  is DRM-protected or needs a separate passcode.
- **`401` or `403` after previously working** — select **Sign in again**; the
  provider probably expired or rotated the session.
- **Remote Synapse session cannot open the browser** — set
  `SYNAPSE_PUBLIC_ORIGIN` to the exact external origin, then make sure the
  reverse proxy preserves the browser `Origin` and WebSocket upgrade headers.
  Route `/api` through Synapse's frontend proxy rather than publishing the API
  container directly. The whole Synapse site must remain behind authentication.
