# Web fetch — JS-rendered CFP pages

Applies wherever this skill fetches a CFP page (Steps 3, 5, 6). Many sites return empty SPA shells to plain `WebFetch`.

- Switch to `mcp__nanoclaw__fetch_markdown(url: "<cfp-url>", wait_until: "networkidle")` — snitchmd renders the page via CloakBrowser past anti-bot gates and returns clean markdown ready for the model. The disk cache makes repeat lookups against the same CFP page during a single sweep free.
- Fall back to Cloudflare Browser Rendering via Composio (`CLOUDFLARE_BROWSER_RENDERING_TAKE_WEBPAGE_SNAPSHOT` for full DOM, `..._SCRAPE_HTML_ELEMENTS` for CSS-selector queries) when the `fetch_markdown` response is empty or indicates a failure (loading shell, anti-bot wall, snitchmd internal error).
- Account ID, parameter-name caveats, and the MULTI_EXECUTE batching limitation for the Composio path are in the `max-effort` skill's Concrete Tool Call Reference.
