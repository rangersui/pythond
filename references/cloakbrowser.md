# CloakBrowser — Stealth Web Browsing

Source-level stealth Chromium. Dozens of C++ patches covering canvas, WebGL,
audio, fonts, GPU, screen, WebRTC, network timing, and automation signals. No
runtime JS injection — anti-bot systems cannot detect the patching.

Read this reference when the task involves web scraping, browsing behind
anti-bot protection, or interacting with sites that detect automation.

## Quick start

```bash
pip install cloakbrowser
```

```python
from cloakbrowser import launch

browser = launch(headless=True, humanize=True)
page = browser.new_page()
page.goto("https://example.com")
print(page.title())
```

`browser` and `page` persist in the pythond namespace. Do not call
`browser.close()` — keep it alive across turns like any other connection.

With proxy and geo-IP:
```python
browser = launch(headless=True, humanize=True, proxy="http://user:pass@proxy:8080", geoip=True)
```

## Common operations

```python
# Navigate
page.goto("https://example.com")
page.wait_for_load_state("networkidle")

# Extract
title = page.title()
text = page.inner_text("article")
html = page.content()

# Query elements
items = page.query_selector_all(".product")
for item in items:
    name = item.inner_text()

# Interact (with humanize, these simulate human timing)
page.click("button.submit")
page.fill("input[name=q]", "search query")
page.press("input[name=q]", "Enter")

# Wait
page.wait_for_selector(".results", timeout=10000)

# Screenshot
page.screenshot(path="screenshot.png")

# JS in page context
count = page.evaluate("document.querySelectorAll('.item').length")

# Fetch JSON
resp = page.goto("https://api.example.com/data.json")
data = resp.json()
```

## Persistent profiles

Login state survives across restarts:

```python
from cloakbrowser import launch_persistent_context

ctx = launch_persistent_context("./my-profile", headless=False)
page = ctx.new_page()
page.goto("https://protected-site.com")
ctx.close()

# Next run — cookies/localStorage restored
ctx = launch_persistent_context("./my-profile", headless=False)
```

With extensions:
```python
ctx = launch_persistent_context(
    "./my-profile",
    headless=False,
    extension_paths=["./my-extension"],
)
```

## Humanize

```python
browser = launch(humanize=True)
browser = launch(humanize=True, human_preset="careful")
browser = launch(
    humanize=True,
    human_config={
        "mistype_chance": 0.05,
        "typing_delay": 100,
        "idle_between_actions": True,
        "idle_between_duration": [0.3, 0.8],
    }
)
```

## Verify stealth

Run these checks after setup. All three should show no automation detected:

```python
page.goto("https://abrahamjuliot.github.io/creepjs/")  # trust score > 70% = good
page.goto("https://bot.sannysoft.com/")                 # all rows green = good
page.goto("https://pixelscan.net/")                     # "consistent" verdict = good
```

## Advanced: cloakserve (standalone daemon)

Use cloakserve when the browser must outlive Python — e.g. multiple pythond
sessions sharing one browser, or you need to restart Python without losing
browser state. For most tasks launch() in a pythond session is simpler and
sufficient.

### Docker

```bash
docker run -d --name cloak -p 127.0.0.1:9222:9222 \
  cloakhq/cloakbrowser cloakserve
```

With proxy:
```bash
docker run -d --name cloak -p 127.0.0.1:9222:9222 \
  cloakhq/cloakbrowser cloakserve --proxy-server=http://proxy:8080
```

Docker Compose:
```yaml
services:
  cloakbrowser:
    image: cloakhq/cloakbrowser
    command: cloakserve
    restart: unless-stopped
    ports:
      - "127.0.0.1:9222:9222"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9222/json/version"]
      interval: 30s
      timeout: 5s
      retries: 3
```

### Connect

```python
from playwright.sync_api import sync_playwright

pw = sync_playwright().start()
cloak = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
page = cloak.new_page()
page.goto("https://example.com")
```

`pw` and `cloak` persist in the pythond namespace. If the session restarts,
re-run these two lines — cloakserve is still up.

### Per-connection fingerprint

Each connection gets a unique fingerprint seed via query params — different
canvas, WebGL, fonts, timing for each:

```python
b1 = pw.chromium.connect_over_cdp("http://localhost:9222?fingerprint=11111")
b2 = pw.chromium.connect_over_cdp("http://localhost:9222?fingerprint=22222")

# Full customization
b3 = pw.chromium.connect_over_cdp(
    "http://localhost:9222?fingerprint=33333"
    "&timezone=Asia/Tokyo&locale=ja-JP&platform=macos"
    "&hardware-concurrency=4&device-memory=8"
)

# Per-connection proxy
b4 = pw.chromium.connect_over_cdp(
    "http://localhost:9222?fingerprint=44444"
    "&proxy=http://proxy:8080&geoip=true"
)
```

Query params: `fingerprint`, `timezone`, `locale`, `platform`,
`platform-version`, `brand`, `brand-version`, `gpu-vendor`, `gpu-renderer`,
`hardware-concurrency`, `device-memory`, `screen-width`, `screen-height`,
`proxy`, `geoip`.

## Security

CDP has zero authentication. Always bind to `127.0.0.1`. Never expose
port 9222 to the network.
