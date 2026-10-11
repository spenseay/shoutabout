# Hosting: the website and the downloads

Status (2026-10-10): **prepared, nothing deployed.** No Cloudflare, DNS, GitHub or App Store
Connect setting has been changed. Every step below is for the owner (or the coordinator,
after approval) to run. The aim is zero running costs: everything here is on a free plan,
apart from the Apple Developer membership the owner already pays for.

| What | Where | Cost |
|---|---|---|
| Website (`site/`: plain files, no build step, under 1 MB) | **Cloudflare Pages**, connected to the GitHub repo, on `shoutabout.fyi` and `www.shoutabout.fyi` | $0 (Free plan) |
| Windows installer `ShoutAbout-Setup.exe` and Mac disk image `ShoutAbout.dmg` | **GitHub Releases** on the public source repo `spenseay/shoutabout` | $0 |
| iPhone app | **TestFlight** (public link), the App Store later | in the Apple membership |
| Contact address (optional) | Cloudflare Email Routing, forwarding to your own inbox | $0 |
| The tunnel at `dictate.shoutabout.fyi` | Unchanged. **Never edit or delete that DNS record.** | |

Contents:

- [The go-live switches](#the-go-live-switches)
- [Before you start: look at the DNS](#before-you-start-look-at-the-dns)
- [A. The website on Cloudflare Pages](#a-the-website-on-cloudflare-pages)
- [B. Downloads on GitHub Releases](#b-downloads-on-github-releases)
- [C. The iPhone link](#c-the-iphone-link)
- [D. A contact address (optional)](#d-a-contact-address-optional)
- [E. Updating later](#e-updating-later)
- [Tokens and permissions](#tokens-and-permissions)
- [What's verified](#whats-verified)

## The go-live switches

`site/downloads.json` is the only file to edit at go-live. Every page reads it when it loads,
so there's nothing to rebuild: commit, push to `main`, and Cloudflare Pages publishes it in
about a minute.

| Key | Now | What it does |
|---|---|---|
| `windows.live` | `false` | `true` turns the Windows buttons into real downloads of `windows.url`. While `false` they read *Coming soon* and link nowhere. |
| `windows.url` | `https://github.com/spenseay/shoutabout/releases/latest/download/ShoutAbout-Setup.exe` | Never needs changing: the file name is stable and `latest` follows new releases. |
| `mac.live` | `false` | The same for the Mac. |
| `mac.url` | `https://github.com/spenseay/shoutabout/releases/latest/download/ShoutAbout.dmg` | |
| `iphone.live` | `false` | `true` (with a link below) turns the iPhone button into *Join the TestFlight beta*, or *Download on the App Store* once `app_store_url` is set (it wins over TestFlight). |
| `iphone.testflight_url` | empty | The TestFlight public link ([C](#c-the-iphone-link)). |
| `iphone.app_store_url` | empty | Later. |
| `contact_email` | empty | Empty: no email anywhere on the site; the help page and the legal pages point to `/support`. Set it, and it appears in the footer, on `/support` and in the privacy policy and terms ([D](#d-a-contact-address-optional)). |
| `windows.size_bytes`, `mac.size_bytes` | `null` | Optional. Shown as "about N MB" (Windows shows "about 500 MB" until then). The release script prints them. |
| `releases_url` | the downloads repo's latest release | The "Release notes and SHA-256 checksums" link under each live download. |

Only `https://` links are used; anything else is ignored and the button stays on *Coming
soon*. No version number appears anywhere on the site or in the file names.

## Before you start: look at the DNS

Cloudflare dashboard → **shoutabout.fyi** → **DNS** → **Records**.

1. Find `dictate`. Leave it exactly as it is. Nothing in this guide touches it.
2. Look for a record **at the apex** (`shoutabout.fyi` or `@`) and for `www`.
   - None: good, Pages adds its own.
   - An A, AAAA or CNAME at the apex or `www` (a parking page, for example): Pages can't add
     its record while it's there (users report error 81053). Delete **only that apex/`www`
     record**, right before attaching the custom domain (step 6 below).
3. **Don't** create a CNAME to `shoutabout.pages.dev` yourself; Pages creates it when you
   attach the domain (adding it by hand first gives a 522 error, per Cloudflare's docs).
4. MX records: if the domain has none, [Email Routing](#d-a-contact-address-optional) can add
   them later. If it has some, mail already works somewhere: keep them.

## A. The website on Cloudflare Pages

**Why Cloudflare Pages.** The domain's DNS is already on Cloudflare, so attaching
`shoutabout.fyi` is two clicks and touches nothing else; static requests and bandwidth are
unlimited on the Free plan (500 builds a month, files up to 25 MiB); `site/_headers` (the
strict Content-Security-Policy) works as is; and pushing to `main` deploys by itself.
Free alternatives exist: **Vercel** (the free Hobby plan is for personal, non-commercial use
and has a monthly bandwidth allowance) and **Render** (free static sites, also with a monthly
bandwidth allowance; both were around 100 GB a month when this was written). Either would
mean pointing `shoutabout.fyi` at another company and watching a quota, for no gain.

**Set it up once (about ten minutes).** The dashboard's wording changes from time to time;
the steps follow Cloudflare's
[Git integration guide](https://developers.cloudflare.com/pages/get-started/git-integration/).

1. [dash.cloudflare.com](https://dash.cloudflare.com) → **Workers & Pages** → **Create** →
   the **Pages** tab → **Import an existing Git repository** (older wording: *Connect to
   Git*).
2. **GitHub** → install the Cloudflare Pages app on your account with **Only select
   repositories** → `spenseay/shoutabout`. Private repos are fine; Cloudflare gets read
   access to that one repo only.
3. Pick `shoutabout` → **Begin setup**, and fill in:

   | Setting | Value |
   |---|---|
   | Project name | `shoutabout` (gives `shoutabout.pages.dev`) |
   | Production branch | `main` |
   | Framework preset | **None** |
   | Build command | leave **empty** (if the form insists, `exit 0`) |
   | Build output directory | `site` |
   | Root directory (advanced) | leave empty (the repo root) |
   | Environment variables | none |

4. **Save and Deploy.** The first deploy takes about a minute. Open
   `https://shoutabout.pages.dev` and click around: every download says *Coming soon*.
5. Project → **Settings** → **Builds** (*Builds & deployments*) → **Branch control**:
   production branch `main`, automatic deployments on; **Preview branch: None**. Otherwise
   every push to any branch builds a public preview at `<branch>.shoutabout.pages.dev` and
   uses up builds.
6. Project → **Custom domains** → **Set up a custom domain** → `shoutabout.fyi` → **Continue**
   → **Activate domain**. Cloudflare adds a proxied CNAME at the apex and issues a certificate
   (a few minutes, until the status reads **Active**). Repeat with `www.shoutabout.fyi`.
7. Leave **Web Analytics** off: the privacy policy says the site has none.

Notes:

- Cloudflare's builder clones the whole private repo, but only `site/` is published.
- A Pages project made by *direct upload* can't be switched to Git later, so create this one
  from Git as above. (If Git ever isn't wanted, `npx wrangler@4 pages deploy site
  --project-name <new project> --branch main` uploads `site/` by hand to a separate,
  direct-upload project.)
- **Deploying from now on:** merge to `main` and push. **Rollback:** project →
  **Deployments** → an older deployment → ⋯ → **Rollback to this deployment**.

Preview the site locally first, served the way Pages serves it (`/download` without
`.html`, the 404 page, the `_headers` file):

```powershell
python scripts\site\serve.py          # http://127.0.0.1:18400
```

### Checks after deploying

```powershell
curl.exe -sI https://shoutabout.fyi/            # 200, with content-security-policy, referrer-policy: no-referrer
curl.exe -sI https://shoutabout.fyi/download    # 200 (Pages serves download.html at /download)
curl.exe -sI https://shoutabout.fyi/nope        # 404 (our 404.html)
curl.exe -s  https://shoutabout.fyi/downloads.json
curl.exe -sI https://dictate.shoutabout.fyi/health   # the tunnel still answers like before
```

In a private browser window: every page, on a phone too; DevTools → **Application** →
**Cookies**: none; **Network**: only `shoutabout.fyi` (plus GitHub when you click a live
download). A `__cf_bm` cookie would come from Cloudflare bot protection on the zone (Security
→ Bots): turn Bot Fight Mode off, or add a sentence about that cookie to `privacy.html`.
Paste `https://shoutabout.fyi` into a chat app: the yellow wordmark card appears
(`assets/img/og.png`).

## B. Downloads on GitHub Releases

Free, files up to 2 GiB each, no bandwidth bill ("There is no limit on the total size of a
release, nor bandwidth usage",
[GitHub](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)).
The source repo stays private; downloads come from a second, **public** repo that holds only
a README and releases. The release script refuses a private repo, so a build can't land in
the source repo by mistake.

**Stable names, always "latest".** The website links to

```text
https://github.com/spenseay/shoutabout/releases/latest/download/ShoutAbout-Setup.exe
https://github.com/spenseay/shoutabout/releases/latest/download/ShoutAbout.dmg
```

GitHub sends those to the file of that name in the **latest** release. So the names never
change (no version in them), and **the latest release must carry both files** once both
exist: a Windows-only release would otherwise break the Mac link. The release script handles
that by copying the other platform's file forward from the current latest release.

**1. Where the downloads live.** On the releases of the public source repo
`spenseay/shoutabout` (no separate downloads repo).

**2. The Windows installer.** Build and test it as in
[installer.md](../installer.md#building-it-locally) (it comes out as
`packaging\dist\ShoutAbout-Setup-<version>.exe`; the script uploads it as
`ShoutAbout-Setup.exe`). Install it on a real PC or a VM, not the daily PC, and walk
through setup before publishing.

**3. The Mac disk image.** Codemagic builds, signs (Developer ID) and notarizes
`ShoutAbout.dmg` (that pipeline is another workstream). Download the DMG from the Codemagic
build's artifacts and check it on a Mac: `spctl -a -t open --context
context:primary-signature -v ShoutAbout.dmg` says *accepted, source=Notarized Developer
ID*. If Codemagic is ever set to publish to GitHub by itself, it must also attach the current
`ShoutAbout-Setup.exe` to that release, for the reason above; publishing through the script
avoids that.

**4. Dry run** (checks the files, shows what would be carried forward, creates nothing):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\release\publish-github-release.ps1 `
  -InstallerPath packaging\dist\ShoutAbout-Setup-1.2.0.exe -Version 1.2.0 `
  -Repo spenseay/shoutabout -DryRun
```

**5. Publish** (one platform or both; separate several files with a comma):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\release\publish-github-release.ps1 `
  -InstallerPath "packaging\dist\ShoutAbout-Setup-1.2.0.exe,C:\Users\you\Downloads\ShoutAbout.dmg" `
  -Version 1.2.0 -Repo spenseay/shoutabout -UpdateSiteConfig
```

What it does: checks each file (.exe or .dmg, size, SHA-256, Authenticode status for the
.exe); checks `gh` is logged in, the repo exists and is public, and the tag `v1.2.0` is new;
downloads the other platform's file from the current latest release, if there is one, and
attaches it again; writes `SHA256SUMS.txt` and short release notes (or `-NotesFile
notes.md`); runs `gh release create v1.2.0 … --latest`; checks the uploaded sizes; prints
(and with `-UpdateSiteConfig` writes) `size_bytes` for `site/downloads.json`. It **doesn't**
flip the live flags. The version appears only in the tag and the release title on GitHub.

**6. Switch it on:** open the link from the script's last lines in a browser. When the
download starts, set `windows.live` (and/or `mac.live`) to `true` in `site/downloads.json`,
commit and push. The buttons change by themselves.

Download counts, without tracking anyone:

```powershell
gh api repos/spenseay/shoutabout/releases --jq '.[] | .tag_name as $t | .assets[] | "\($t) \(.name) \(.download_count)"'
```

Things to know: the download URL shows the GitHub account name; GitHub (not us) handles the
download under its own privacy statement, which `privacy.html` mentions. GitHub Actions
billing doesn't apply to releases on a public repo, but publishing hasn't been tried yet.
(Cloudflare R2 on `dl.shoutabout.fyi` would also work for free up to 10 GB stored, but it
needs an API token, multipart uploads and its own setup; GitHub Releases is simpler.)

## C. The iPhone link

1. Codemagic uploads a build to App Store Connect (another workstream).
2. App Store Connect → the app → **TestFlight** → **External Testing** → create a group (for
   example *Public beta*) → add the build. The first build of a version goes through
   **TestFlight App Review** (often a day or two); answer the *Test Information* fields from
   [store-listing.md](store-listing.md#testflight).
3. In the group, turn on **Public Link** (you can cap the number of testers). Copy the
   `https://testflight.apple.com/join/…` link.
4. In `site/downloads.json`: `iphone.testflight_url` = that link, `iphone.live` = `true`.
   Commit, push. The buttons say *Join the TestFlight beta*.
5. Later, when the App Store version is out: `iphone.app_store_url` = its
   `https://apps.apple.com/…` link. The buttons switch to *Download on the App Store* and the
   "A beta first" note disappears. Then update the two FAQ answers that say "It starts as a
   TestFlight beta" (`index.html`, `support.html`).

Each TestFlight build works for 90 days; up to 10,000 external testers.

## D. A contact address (optional)

The site works without any email: `/support` has the FAQ and quick fixes, and iPhone beta
testers can send feedback from TestFlight. With `contact_email` empty, no address appears
anywhere.

**For the App Store, set one up anyway.** Apple's App Review Guideline **1.5** (Developer
Information) asks that the app and its **Support URL** include an easy way to contact you.
The Support URL will be `https://shoutabout.fyi/support`. TestFlight's external testing also
asks for a *feedback email* that testers see. A free forwarding address keeps your personal
inbox private:

1. Cloudflare dashboard → **shoutabout.fyi** → **Email** → **Email Routing** (in newer
   dashboards also under *Compute → Email Service*) → **Onboard Domain** → accept the
   MX/SPF/DKIM records it proposes. (If the domain already receives mail elsewhere, don't;
   pick another address instead.)
2. **Destination addresses**: add your own inbox and click the link in the verification mail.
3. **Routing rules** → **Create routing rule**: `support` (or `hello`) → *Send to an email* →
   your inbox. Send a test from another account.
4. `site/downloads.json` → `"contact_email": "support@shoutabout.fyi"` → commit, push.

Email Routing only receives. Replies go out from your own inbox, so people see that address
when you answer; answering only through TestFlight or keeping replies short is up to you.
Cost: $0.

Related: App Store Connect asks once whether you're a **trader** under the EU Digital
Services Act. A trader's address, phone number and email are shown on the app's page in EU
storefronts; a non-trader's aren't. A free app with no business behind it is often
non-trader, but that's the owner's call (not legal advice).

## E. Updating later

| Change | Steps |
|---|---|
| Website text or design | Edit `site/`, preview with `scripts\site\serve.py`, merge to `main`, push. |
| New Windows and/or Mac version | Build, test → `publish-github-release.ps1 -UpdateSiteConfig` → commit `site/downloads.json` (sizes) → push. The links don't change. |
| TestFlight / App Store link | `downloads.json` → `iphone.*` → push. |
| Contact email on or off | `downloads.json` → `contact_email` → push. |
| Screenshots, icons, fonts, social image | `scripts\site\make_assets.py` (needs Pillow, fontTools and Brotli in a separate tool venv; see its docstring). Only screenshots whose mouth is the primary mark: tongue out, no ring or outline around it (on black it sits on a yellow tile). The script's lists already follow that rule; the iPhone is drawn in CSS (`.iphone`, `.island` in `site.css`), because the app's record button and Live Activity show a ringed mouth. |
| Undo a bad deploy | Pages → Deployments → older deployment → Rollback. |

## Tokens and permissions

| What | Where | Permissions | Used for |
|---|---|---|---|
| Cloudflare Pages GitHub app | GitHub → Settings → Applications | Read access to `spenseay/shoutabout` only (*Only select repositories*) | Building the site on every push to `main` |
| GitHub CLI (`gh auth login`) | already on this PC | `repo` scope (gh's default) | `gh repo create`, `gh release create` |

No Cloudflare API token is needed with the Git integration. Never put a token in the repo,
in `site/`, or in a saved script.

## What's verified

- `site/` was served by `scripts/site/serve.py` on 127.0.0.1:18410 and checked in headless
  Chrome (throwaway profile) through the DevTools protocol: all 8 pages (home, download,
  setup, support, privacy, terms, licenses, 404) at 1440 and 390 px, light and dark, with no
  console errors or Content-Security-Policy reports, no failed requests (apart from the 404
  page's own status), no broken images and no horizontal overflow. Every same-site link,
  image and `#fragment` resolves; the five external links answered 200.
- With a test `downloads.json` served in place of the real one (all three `live` flags on,
  a sample TestFlight link, sizes and a contact address), the buttons link to the planned
  URLs, the sizes and checksum links appear, and the address shows in the footer, on
  `/support` and in the legal pages. With the real file, every button reads *Coming soon*
  and no email address is anywhere.
- The homepage wordmark's talking loop was sampled every 100 ms over a full cycle: the red
  tongue is visible in every frame.
- `html-validate` 9.7.1 (recommended rules) reports no problems.
- `publish-github-release.ps1`: parsed without errors, ASCII only, and run with `-DryRun` on
  dummy files (Windows only; Windows + Mac; a bad version). It has **never published
  anything**; no repo, release or tag exists.
- Not done: any Cloudflare, DNS, GitHub or App Store Connect change, so Pages' Git builds,
  `_headers`, pretty URLs and 404 handling on real Pages, the `latest/download` redirects and
  Email Routing are untested (the local server mimics Pages as Cloudflare documents it).
- Vendor limits and prices are from their docs as of 2026-10-10.
