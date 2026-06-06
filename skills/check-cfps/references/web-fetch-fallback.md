# Web fetch — JS-rendered CFP pages

Applies wherever this skill fetches a CFP page (Steps 3, 5, 6). Many sites return empty SPA shells to plain `WebFetch`.

- Switch to `mcp__nanoclaw__fetch_markdown(url: "<cfp-url>", wait_until: "networkidle")` — the NanoClaw host's server-side renderer returns the fully-rendered page as clean markdown ready for the model. A built-in disk cache makes repeat lookups against the same CFP page during a single sweep free.
- Fall back to Cloudflare Browser Rendering via Composio (`CLOUDFLARE_BROWSER_RENDERING_TAKE_WEBPAGE_SNAPSHOT` for full DOM, `..._SCRAPE_HTML_ELEMENTS` for CSS-selector queries) when the `fetch_markdown` response is empty or indicates a failure (loading shell or renderer error).
- The Composio path needs an account ID and has parameter-name and `MULTI_EXECUTE` batching caveats — see your Composio Cloudflare Browser Rendering tool configuration.
