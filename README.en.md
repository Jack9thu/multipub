# multipub

[中文](README.md) | **English**

Publish **videos / image posts / articles** to **Bilibili, Douyin, Xiaohongshu (RedNote) and Zhihu** with one command. The CLI is `mpub`, Windows first.
Login and publishing are done with browser automation ([patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)) and the web APIs the platforms' own sites use.
It ships with a [Claude Code skill](#using-it-with-claude-code) so Claude can prepare manifests, check logins and publish after you confirm.

> **First time? Read the [Windows tutorial](docs/tutorial-windows.md)** (Chinese): installation, login, first post, scheduled posts and FAQ, step by step.

> [!WARNING]
> **Most features are covered by offline tests only and have not been verified one by one on real accounts. Test with a throwaway account first.**
> Platform pages and APIs change at any time; each platform document ends with a list of what has not been verified on a real account, with confidence levels.

> [!NOTE]
> **Real-account test (2026-10-25)**: basic publishing succeeded on all four platforms (one Bilibili video, one Douyin video, one Xiaohongshu image note and one Zhihu column article, each with a content label).
> Douyin `collection`, Xiaohongshu `collection` and Zhihu `column` (adding to a collection / column while publishing) were also used in real publishes the same day: the Douyin and Xiaohongshu logs show the collection was selected, and the Zhihu column API confirmed the article was added.
> Bilibili `collection` (tested 2026-10-26): about 3 seconds after the submission succeeded the video was added to the collection while still under review; it already appeared in the collection's video list (marked as pending review), with no need to wait for approval.

## Platforms and content types

| | Video `video` | Image post `note` | Article `article` |
|---|---|---|---|
| **Bilibili** | ✔ video upload (via [biliup](https://github.com/biliup/biliup)) | ✔ dynamic post (opus, published in the browser) | ✘ |
| **Douyin** | ✔ | ✔ image post | ✘ |
| **Xiaohongshu** | ✔ | ✔ image note | ✘ |
| **Zhihu** | ✘ | ✔ "idea" (can be published as an article instead) | ✔ column article (Markdown, images, topics, drafts) |

Combinations marked ✘ are skipped automatically and do not count as failures. Fields, limits, login methods, results and known risks per platform (Chinese):
[Bilibili](docs/platforms/bilibili.md) · [Douyin](docs/platforms/douyin.md) · [Xiaohongshu](docs/platforms/xiaohongshu.md) · [Zhihu](docs/platforms/zhihu.md).

One piece of content = one directory + one `post.yaml` ([field reference](docs/manifest.md), Chinese):

```yaml
kind: video
title: Episode 12 - XXX in three minutes
body: Video description
tags: [tech, tutorial]
video: video.mp4
cover: cover.jpg
account: main
platforms:
  bilibili: {tid: 231}
  douyin: {title: XXX in three minutes}
  xiaohongshu: {title: Learn XXX in 3 minutes}
```

## How it works

There are two approaches:

- **Browser automation**: patchright opens a real Chrome (or Chromium / Edge) with the login state saved at login time, and uploads, fills in, clicks and presses "Publish" on the creator site like a person, checking after each step that the page looks as expected. Platform signatures and encryption are computed by the page itself; the costs are speed and the need to update element locators whenever a page is redesigned.
- **Calling the API directly**: no browser; the cookies / tokens from the login file are used to send the requests straight to the platform's servers (the same requests the web page sends behind the scenes). Fast and independent of page layout, but it breaks when the API changes or starts requiring request signatures.

| Platform | Basic publishing | Adding to a collection |
|---|---|---|
| **Bilibili** | **Video: API** - calls the [biliup](https://github.com/biliup/biliup) CLI, which uploads and submits through Bilibili's upload API; **dynamic posts: browser automation** (the t.bilibili.com composer) | **API, right after submitting** - calls the creator center's collection API once the submission succeeds (the same as the upload page), so videos still under review can be added |
| **Douyin** | **Browser automation** - the creator center publish page: upload, title and description, topics, cover, declaration, press publish, and listen for the create-API response | **Browser automation, during publishing** - picks the collection in the form's "add to collection" dropdown by exact name and verifies it |
| **Xiaohongshu** | **Browser automation** - the creator platform publish page, same flow as Douyin | **Browser automation, during publishing** - "content settings → add to collection", picked with a page script and verified against the name shown on the button |
| **Zhihu** | **API** (a browser is opened only for QR-code login) - the web API Zhihu's own site uses: upload images → create draft → body, topics, declaration → publish | **API, during publishing** - the column id (`column`) is sent in the publish request |

None of the platforms can **create** a collection (collections / columns must be created by hand on the website or in the app first). If a collection cannot be selected, the result only carries a note and the post is still published.

## Collections / columns

| Platform | In the manifest | Notes |
|---|---|---|
| Bilibili | `platforms.bilibili.collection: <collection name>` | exact name match; videos only; added right after submitting, no need to wait for approval |
| Douyin | `platforms.douyin.collection: <collection name>` | exact name match; videos only |
| Xiaohongshu | `platforms.xiaohongshu.collection: <collection name>` | exact name match; videos and image notes |
| Zhihu | `platforms.zhihu.column: c_1234567890` (or the column URL) | articles only; ignored when `draft: true` |

For content that is already published: Bilibili - add it under "Collection management" in the creator center, or run `biliup season add <section_id> --vid <BV>` after approval; Douyin - creator center "Content management → Collections → Edit collection → Add works"; Zhihu - "Settings → Remove from column" under the article on the column page, or "Add content" on the column page;
Xiaohongshu - only in the app (collections are not visible on the web).

## Safety design

- **No duplicate posts**: `submit_started` is recorded before pressing "Publish" / calling the publish API (a "result pending" entry is written to the publish history first, so it survives even if mpub is killed); no failure after that point is ever retried automatically. If the outcome is uncertain the result is `unknown_outcome`, telling you to check the platform's dashboard first.
  Publishing the same content again is skipped by the duplicate check, and two mpub processes publishing the same item only publish it once; to really publish again use `--to <platform> --force`.
- **Validate before publishing**: `mpub validate` / `mpub publish --dry-run` only run local checks (title length, image count, schedule window, ...) and never touch the network.
- **No declarations on your behalf**: AI-content declarations, repost declarations and platform rule confirmations are only ticked when you explicitly set them (see the changes to social-auto-upload below).
- **One subprocess per platform**: if one platform hangs it is killed together with its browser without affecting the others; timeouts and errors leave logs and screenshots.
- **Every step is logged**: each progress step shown in the terminal (plus debug messages the terminal hides) is written with a timestamp to the run's `<platform>-<action>.log`, so you can still see where it got stuck after closing the terminal, from a scheduled task or a background run; on `unknown_outcome` it helps tell whether the publish request went out.
  Before writing to disk, the values of login credentials such as `SESSDATA`, `bili_jct`, `z_c0`, `access_token`, `sessionid` and `Cookie:` headers are masked, so sharing a log for troubleshooting does not hand over your account.
- **Login credentials stay in the local data directory** (default `%LOCALAPPDATA%\multipub`), never in the repository.

## Why upstream social-auto-upload rather than omnipost

The Douyin and Xiaohongshu browser flows come from [social-auto-upload](https://github.com/dreammis/social-auto-upload) (SAU, MIT)
upstream commit `0012d2c` (2026-09-03), vendored in `src/multipub/_sau/`. Its forks omnipost / PostSail were not used because:

- **omnipost is an old snapshot**: the whole repository is a single squashed commit corresponding to SAU `99a392a` (2026-05-30),
  missing about 36 later upstream fixes for Douyin / Xiaohongshu / Bilibili (QR login and publish-page selectors after the Douyin creator center redesign, SMS second verification, Xiaohongshu topics and declarations, ...).
- **omnipost's Zhihu support is minimal**: plain-text articles only, no images, Markdown or topics, no check that publishing succeeded, and not reachable from the command line (only wired into its web UI).
- **omnipost's `requirements.txt` is broken**: wrong encoding, and package names that do not exist on PyPI and are easy to typosquat.
- **PostSail** adds article publishing on top of omnipost, but its own documentation marks Zhihu, Bilibili and Xiaohongshu as not accepted on real accounts.

multipub's Zhihu (API publishing) and Bilibili (biliup upload + dynamic posts) are its own implementations; only the Xiaohongshu AI-content declaration function was ported from omnipost.

### Changes multipub makes to SAU

Every change is marked with `# multipub patch:` in the source; the full list is in [`src/multipub/_sau/PATCHES.md`](src/multipub/_sau/PATCHES.md). The main ones:

- **No default AI declaration**: upstream ticks "content generated by AI" on every Douyin video; now it is declared only with `ai_generated: true` or an explicit `declaration`.
- **No forced Toutiao / Xigua sync**: upstream switches on Douyin's third-party sync; now it is left alone by default, switched on only with `sync_toutiao: true`, and switched off with `false`.
- **No default "reposted from"**: upstream declares every Xiaohongshu note as a repost (with an empty media name); now it is declared only when `repost_source` is set.
- **Every wait loop has a deadline**: a redesigned page no longer hangs forever; timeouts take a screenshot and distinguish "before pressing publish" (retryable) from "after pressing publish" (outcome unknown).
- **Guarded publish button**: upstream clicks "Publish" every 3.5 seconds until the page navigates; now it clicks again only if a platform dialog intercepted the click or nothing happened for 20 seconds, at most 3 times,
  while listening for the create-API response; an explicit platform rejection is reported immediately.
- Also: the browser follows the configuration (the local Chrome can be used), logs no longer go to stdout, Douyin SMS codes are submitted with `mpub verify-code`, and QR-code images can be scanned in headless mode.
- **Any Xiaohongshu content-type declaration**: `platforms.xiaohongshu.declaration: 虚构演绎，仅供娱乐` (or `内容包含营销广告`, `笔记含AI合成内容`; `false` for none; the text must match the page exactly).
  Upstream can only select "AI-synthesized" and "reposted"; content labels became mandatory in 2026-05. The selection is verified, and **if it cannot be verified the post is refused** (nothing is published).
- **Xiaohongshu collection while publishing**: `platforms.xiaohongshu.collection: <collection name>` selects the collection under "content settings → add to collection" on the publish page (page structure checked on the real publish page); if it cannot be selected, only a warning is given and the post is still published.

## Windows quick start

Requires Windows 10/11 and [Git](https://git-scm.com/download/win); Bilibili video upload also needs the [VC++ 2015-2022 runtime (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe) (most PCs already have it; `mpub doctor` checks). PowerShell in [Windows Terminal](https://aka.ms/terminal) is recommended.
More details (Chinese paths, mirrors in mainland China, using the local Chrome, antivirus, scheduled tasks) in [docs/windows.md](docs/windows.md) (Chinese).

1. Install [uv](https://docs.astral.sh/uv/) (it also manages Python, so no separate Python install is needed):

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   Reopen the terminal afterwards; `uv --version` should print a version.

2. Get the code and install the dependencies:

   ```powershell
   git clone https://github.com/Jack9thu/multipub
   cd multipub
   uv sync
   ```

   `uv sync` prepares Python 3.12 according to `.python-version`, creates `.venv` and installs the dependencies (including the dev group for tests).
   Alternatively `uv venv` then `uv pip install -e .`, or run `scripts\install.ps1` to do steps 2 and 3 (add `-China` inside mainland China: dependencies from domestic mirrors, and the built-in Edge instead of a downloaded browser; see [docs/windows.md](docs/windows.md)).

3. Install the browser engine, prepare biliup and run the self-check:

   ```powershell
   uv run mpub setup
   uv run mpub doctor
   ```

   **You choose where biliup comes from** (used for Bilibili video upload): `setup` lists the biliup binaries already on the machine and asks whether to give a path, use one it found,
   or download the verified v1.3.1. You can change it any time with `mpub biliup find` / `mpub biliup use <path or number>` /
   `mpub biliup use --auto` / `mpub biliup use --download`, and check with `mpub biliup status` (see [docs/platforms/bilibili.md](docs/platforms/bilibili.md)).

   Slow downloads in mainland China: see the mirror settings in [docs/windows.md](docs/windows.md), or use the local Chrome / Edge (no engine download needed).

4. Log in to each platform by QR code (`main` is the name you give this account; `account` in `post.yaml` must match it):

   ```powershell
   uv run mpub login bilibili --account main
   uv run mpub login douyin --account main
   uv run mpub login xiaohongshu --account main
   uv run mpub login zhihu --account main
   uv run mpub check --all
   ```

   Douyin, Xiaohongshu and Zhihu open a browser window with the QR code; for Bilibili, scan the QR-code image whose path is printed in the terminal with the mobile app (TV login, no browser).
   All QR-code images are in `%LOCALAPPDATA%\multipub\qrcode\`.

5. Prepare a post and publish it:

   ```powershell
   uv run mpub new posts/demo --kind video      # creates posts\demo\post.yaml; edit it as commented and add video.mp4 etc.
   uv run mpub publish posts/demo --dry-run     # local validation only
   uv run mpub publish posts/demo               # publish for real; asks for confirmation once more
   ```

## Common commands

Run every command from the repository root as `uv run mpub ...` (or activate `.venv` first). `--json` / `--home` go after the subcommand.

| Command | What it does |
|---|---|
| `mpub doctor` | Self-check: Python, patchright, browser, biliup, logged-in accounts per platform |
| `mpub setup [--skip-browser] [--biliup auto\|download\|<path>]` | Install the patchright Chromium and prepare biliup (the only command that downloads) |
| `mpub biliup find` / `status` / `use <path or number>` / `use --auto` / `use --download` | Choose which biliup is used for Bilibili video upload |
| `mpub login <platform> --account <name> [--headless]` | QR-code login; shows a browser window by default |
| `mpub login bilibili --account <name> --interactive` | Run biliup's own interactive login in a real terminal (Bilibili fallback) |
| `mpub check [platform…] [--account <name>]` | Check whether logins are valid; no platform = all (same as `--all`) |
| `mpub accounts` | List logged-in accounts |
| `mpub new <dir> --kind video\|note\|article [--force]` | Create a `post.yaml` template |
| `mpub validate <post>` | Validate a manifest locally |
| `mpub publish <post> --dry-run` | Local validation, printed in the publish result format (also shows which platforms were already published and will be skipped) |
| `mpub publish <post> [--to platform,platform] [--account name] [--yes] [--headed] [--gap seconds] [--timeout seconds] [--force]` | Publish |
| `mpub history [--limit N]` | Show the publish history |
| `mpub verify-code douyin <code>` | Submit the SMS code when Douyin asks for SMS verification while publishing |

Platform names can be `bilibili` / `douyin` / `xiaohongshu` / `zhihu`, or aliases such as `b站`, `抖音`, `xhs`, `知乎`.
`<post>` is the path to `post.yaml` or its directory. With `--json`, stdout carries a single JSON object and progress goes to stderr
(format in [cli-contract.md](.claude/skills/multipub/references/cli-contract.md), Chinese).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Everything succeeded (published, submitted for review, scheduled, draft, validation passed, skipped) |
| `1` | At least one platform failed |
| `2` | Usage or manifest error, validation failed, no `--yes` outside an interactive terminal, confirmation declined |
| `3` | Needs a human: login expired, captcha, SMS code, risk-control verification, platform rule confirmation |
| `130` | Interrupted with Ctrl+C (platforms where publish was already pressed are recorded as outcome unknown) |

If there are both failures and items needing a human, the exit code is `1`.

## Data directory

Default `%LOCALAPPDATA%\multipub` (Linux/macOS: `~/.local/share/multipub`); change it with the `MULTIPUB_HOME` environment variable or `--home`:

```
%LOCALAPPDATA%\multipub\
├─ config.toml              optional configuration, see docs/config.example.toml
├─ accounts\<platform>\<account>.json   login credentials (cookies / tokens - as sensitive as passwords, never share them)
├─ tools\biliup\v1.3.1\     downloaded biliup.exe and its install record (install.json, sha256)
├─ tools\biliup\selected.json  the biliup source you chose (written by mpub biliup use)
├─ qrcode\                  login QR-code images (<platform>-<account>.png, deleted after login)
├─ runs\<time>\<platform>\  logs (<platform>-<action>.log, every step timestamped, credentials masked) and screenshots of each run (the evidence in results points here)
├─ sau\                     logs of the Douyin/Xiaohongshu flows, Douyin SMS code file verify_code.txt
├─ locks\                   account locks (one task per account at a time)
├─ pending\                 records of posts where publish was pressed but no final result arrived (left when mpub is killed mid-publish; used by the duplicate check)
└─ history.jsonl            publish history (duplicate check, reconciliation)
```

patchright's Chromium lives in its own directory (Windows: `%LOCALAPPDATA%\ms-playwright`).

## Risks

- **Risk control and bans**: automated publishing can trigger platform risk control (captchas, rate limits, reduced reach, bans in severe cases). multipub tries to behave like normal use but cannot guarantee it will not be detected.
- **Test with a throwaway account**: apart from the basic publishing listed in the NOTE above, features have not been verified one by one on real accounts. Walk through login, publishing, drafts and scheduling with an unimportant account first.
- **Keep the frequency low**: a few posts per account per day at most; 30 seconds between platforms by default (`--gap` / `gap_seconds`); do not publish frequently from many accounts on one IP.
- **Follow the platform rules**: content must follow each platform's community guidelines; set `ai_generated: true` for AI-generated content; declare reposts truthfully.
- **Legal risk of Bilibili APIs**: Bilibili has shut down several third-party API libraries with lawyer's letters. multipub publishes Bilibili image posts in the browser by default; the HTTP mode is experimental only, see [bilibili.md](docs/platforms/bilibili.md).
- **Check the dashboard when the outcome is unknown**: when you see `unknown_outcome` or a failure with `submit_started: true`, check the platform's dashboard first instead of publishing again.
- **Zhihu may start requiring signatures**: in a 2026-10-25 test, publishing itself succeeded, but the post-publish verification request was rejected (HTTP 403, "请求参数异常，请升级客户端后重试").
  If publishing starts failing the same way, Zhihu has started requiring the `x-zse-96` signature and API publishing will stop working.
- **Zhihu image processing timeouts**: if a run times out while uploading images (before publish is pressed, so nothing is published), raise `image_timeout` (seconds) under `[platforms.zhihu]` in `config.toml`,
  and give the whole task more time with `--timeout`. Older versions sometimes waited 6-10+ minutes per image because they never told Zhihu the upload had finished (fixed; images now finish within seconds).
- **Xiaohongshu's main site blocks headless browsers**: opening www.xiaohongshu.com in a headless browser is flagged as "IP at risk" (the creator platform used for publishing is not affected); use headed mode if you need to visit the main site.

## Using it with Claude Code

The repository includes a project-level skill: [`.claude/skills/multipub/`](.claude/skills/multipub/SKILL.md). Open Claude Code in the repository directory
and just say things like "publish posts/ep12 to Bilibili and Douyin", "check the logins on every platform" or "write a post.yaml for a Xiaohongshu image note". Claude will:

1. prepare and check the manifest with `mpub new` / `mpub validate`;
2. **run `--dry-run` first** and summarize platforms, accounts, titles, schedules, declarations etc., and **publish only after you explicitly confirm**;
3. run `mpub login` and show you the QR-code image when a login has expired, and ask you for the SMS code when Douyin needs one;
4. remind you to check the platform dashboard when an outcome is uncertain, and **never republish automatically**.

You can also ask for **pre-publish preparation** only, e.g. "do the pre-publish preparation for `<work directory>`": Claude checks or fills in titles, descriptions, tags, covers, content labels and collections for each platform,
checks how Zhihu articles render as Markdown (e.g. bold that breaks when Chinese punctuation touches `**`), leftover Markdown symbols in plain-text bodies (Xiaohongshu etc.) and whether Zhihu topics have exact matches,
and stops at the confirmation summary without publishing (see [prepare.md](.claude/skills/multipub/references/prepare.md) and [content-check.md](.claude/skills/multipub/references/content-check.md), in Chinese).

To use it from any directory, copy `.claude\skills\multipub` to `%USERPROFILE%\.claude\skills\multipub`
and change the commands in SKILL.md to include the repository path (see "全局安装" at the end of SKILL.md).
Your own style guides, work-directory conventions, cover tools, collection tables and so on can be added to that global copy; see "本机适配" in SKILL.md for what fits where.

## Development

```powershell
uv sync                          # includes the dev group (pytest)
uv run pytest -q                 # all offline tests; no browser or account needed
```

The browser simulation tests under `tests/sim/` are skipped by default; set `MULTIPUB_SIM_CHROMIUM` to a Chromium-based browser to run them (the local Chrome works too):

```powershell
$env:MULTIPUB_SIM_CHROMIUM = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
uv run pytest -q tests/sim/test_sim_xhs.py -k "not sigterm"   # the sigterm test reads /proc and only runs on Linux
```

pip users: `pip install -e ".[dev]"`. The optional extra `terminal-qr` (opencv) prints ASCII-art QR codes in the logs.

## License

[MIT](LICENSE). Licenses of the bundled third-party code (social-auto-upload etc.) and of programs downloaded at runtime (biliup, Chromium) are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This project is not affiliated with any of the platforms.
