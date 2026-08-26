# Data Sources

Every adapter is a small, self-contained module under
`src/kc_news_radar/collectors/`. Each one implements `fetch()` and
`parse()`. Failure is isolated per-adapter (see `docs/ARCHITECTURE.md`).

## Tier-1 sources currently live

| Adapter                    | Source URL                                            | Type                | Notes |
| -------------------------- | ----------------------------------------------------- | ------------------- | ----- |
| `kcmo_open_data`           | `https://data.kcmo.org/resource/7at3-sxhp.json`       | Socrata JSON        | KCMO 311 service requests. Aggregate signals only; see privacy note below. |
| `kcmo_council_legistar`    | `https://webapi.legistar.com/v1/kansascity/Matters`   | Legistar InSite JSON | KCMO City Council legislative record (Clerk / Legistar). Bounded to 50 most-recently-modified matters. |
| `jackson_county`           | `https://webapi.legistar.com/v1/jacksonco/Matters`    | Legistar InSite JSON | Jackson County MO County Legislature. Bounded to 50 most-recently-modified matters. |
| `johnson_county`           | `https://www.jocogov.org/rss.xml`                     | RSS                 | Johnson County, KS news + notices. Requires Brotli-capable HTTP client. |
| `nws_kc`                   | `https://api.weather.gov/alerts/active`               | GeoJSON             | Filtered by KC-metro zones. `empty_ok = True` so quiet periods stay HEALTHY. |
| `nws_afd_eax`              | `https://api.weather.gov/products/types/AFD/locations/EAX` | JSON           | NWS Area Forecast Discussion for KC (KEAX office). |
| `usgs_quakes`              | `https://earthquake.usgs.gov/fdsnws/event/1/query`    | GeoJSON             | Central-US bounding box; `empty_ok = True`. |
| `mo_house`                 | Missouri House bill list                              | HTML                | Bounded to 60 most recent items per run. |
| `ridekc`                   | `https://www.ridekc.org/getting-around/service-alerts/` | HTML              | RideKC / KCATA service alerts (title, cause, effect, expiration, routes, bulletin permalink). |
| `flykc`                    | `https://flykc.cdn.prismic.io/api/v2` (Prismic InSite) | Prismic JSON       | KCI Airport aviation-department publications from the Prismic `general_content_page` type. See the FlyKC note below. |

## Notes on individual adapters

### `kcmo_council_legistar` (KCMO City Council)

Kansas City's City Clerk publishes the council's legislative record
through Legistar (`kansascity.legistar.com`). The Legistar InSite web
API (`webapi.legistar.com/v1/kansascity/`) exposes matter rows with
`MatterFile`, `MatterType`, `MatterStatus`, `MatterBody`, intro/agenda/
passed/enactment dates, and `MatterTitle`. That is the correct
legislative-monitoring surface for a newsroom radar. It replaces the
earlier generic press-release RSS, which (a) was fronted by a CDN that
blocked automated access and (b) surfaced marketing copy rather than
legislative actions — a conceptually wrong source for this system's
mission.

### `jackson_county` (Jackson County Legislature)

Same shape as KCMO: Jackson County publishes County Legislature matters
through Legistar (`jacksongov.legistar.com`) with the InSite web API at
`webapi.legistar.com/v1/jacksonco/`. Structured Matter rows are cleaner
than the county's news RSS and, crucially, retrievable — the news RSS is
fronted by Cloudflare and returns 403 to automated clients. We do not
bypass that control; instead we monitor the legislative record itself
through the API the county's own Legistar tenant exposes.

### `ridekc` (RideKC service alerts)

RideKC publishes rider alerts server-side on
`https://www.ridekc.org/getting-around/service-alerts/`. Each alert
exposes a title, expiration date, cause, effect, description, list of
affected routes, and a permalink to the underlying service bulletin.
There is no public RSS feed, and we do not invent one — the page is the
canonical current interface.

### `flykc` (KCI Airport)

FlyKC.com is a Gatsby SPA whose content is served by Prismic
(`flykc.cdn.prismic.io`). The Prismic `newsroom` document type contains
only a landing/hub document — MCI's aviation department does not
publish per-release documents through any dedicated `news_release`
type. Dated aviation-department content (for example the 2026 FIFA
World Cup airport-operations page or the new-terminal project awards)
is published through the `general_content_page` type. This adapter
surfaces the 20 most-recently-published `general_content_page`
documents as the closest publicly-available "aviation department
publications" stream. The label makes this framing explicit — these
are content-page publications from the aviation department, not
formal press releases.

## Privacy notes

### 311 (`kcmo_open_data`)

311 service requests are a **resident-reported signal**, not a verified
fact about any household, address, or person. The adapter and the
`COMMUNITY_311_TREND` detector observe two safeguards:

1. **Only aggregate geography surfaces in dashboard-visible fields**
   (neighborhood, or council district when the neighborhood is missing).
   Street addresses are kept in `metadata.street_address_private` for
   downstream investigation by human reporters but are never rendered in
   signal titles, signal summaries, forecast claims, or brief cards.
2. **Every 311 signal summary is explicitly labeled** as a
   resident-reported pattern, not a verified fact.

### General

- No collector persists or transmits data outside the local machine.
- The FastAPI server binds to `127.0.0.1` by default.

## Development-deal early warning

The `DEVELOPMENT_DEAL_ACTIVITY` detector matches a curated set of
Kansas-City-specific development-deal patterns: `royals`, `chiefs`,
`stadium`, `arrowhead`, `kauffman stadium`, `port kc`, `tif`, `tax
increment financing`, `bond issue|authorization|referendum`, `development
agreement`, `land acquisition`, `public financing`, `revenue bond`, and
`eminent domain`. The point is to give a newsroom manager a dedicated view
of the largest recurring civic-story category in the metro without having
to scan every unrelated agenda item.

## Immigration court trend monitoring (future work)

Kansas City hosts the EOIR immigration court that serves a multi-state
area. Aggregate trends — total case volumes, hearing schedule density,
docket composition by charge category — are legitimately newsworthy and
publicly discussed. The EOIR public data portal
(<https://www.justice.gov/eoir/foia-library-0>) publishes anonymized case
tables suitable for aggregate analysis.

If and when we add EOIR ingestion, these constraints apply:

1. **Aggregate signals only.** Total case counts, categorical breakdowns,
   hearing-day density. Never per-person profiling. Never anything that
   would reveal a specific respondent.
2. **No name, no A-number, no address, no attorney identity** enters
   `source_items.title`, `source_items.excerpt`, signal titles or
   summaries, forecast claims, or the Morning Strategy Brief.
3. **Explicit labeling** in every surfaced signal that this is a docket
   trend, not a claim about any individual case or person.
4. **Community-organization consultation** before enabling — see
   `docs/EDITORIAL_SAFETY.md`.

The rationale mirrors the 311 constraint: administrative case data can
easily be misused to profile individuals or communities. The radar's job
is to flag *newsroom-worthy patterns*, not to expose the people the
system is processing.
