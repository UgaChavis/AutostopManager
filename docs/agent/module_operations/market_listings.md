# Avito and Baza.Drom listing leads

Manager exposes two independent on-demand marketplace adapters through its native
MCP surface. Avito uses ReefAPI's `/avito/v1/search` and `/avito/v1/listing`.
Baza.Drom uses Webbee's task API: create, start, poll, then read JSON results.
The providers are external data sources; neither adapter contacts sellers or
writes CRM, Store, or marketplace records.

| Source | Private runtime settings | MCP tools | Source contract |
| --- | --- | --- | --- |
| Avito | `REEFAPI_API_KEY` | `avito_search_listings`, `avito_read_listing` | [ReefAPI Avito API](https://reefapi.com/docs/avito) |
| Baza.Drom | `WEBBEE_API_TOKEN`, `WEBBEE_DROM_ROBOT_ALIAS` | `drom_start_parts_search`, `drom_get_parts_search` | [Webbee task API](https://app.webbee-ai.ru/api-docs/swagger.yml) |

Set credentials in the private Manager runtime environment. Never place token
values, vendor payloads, or customer search text in Git or general docs.
`catalog_provider_status(stage="market_listing")` lists missing setting names
without exposing values. `live_callable_now` requires settings and any explicit
activation switch; `authorization_status=unverified` is a configuration-only marker and
does not persist successful calls. Judge live access by each tool result.
Verify both sources with a non-customer part query after activation.

Baza.Drom/Webbee is implemented but **disabled**. The current Webbee Free
account does not expose an API token: its security page says API access is
unavailable on Free, and the authenticated token request returned 401. Provider
status reports `activation_status=disabled`, `indicator=red` and
`live_callable_now=false`; both Drom tools return `webbee_disabled` without a
vendor request, even if credentials are added later. After Webbee grants API
access, set `WEBBEE_API_TOKEN` privately, verify a bounded non-customer task
with the switch enabled in an isolated environment, then set
`AUTOSTOP_DROM_LISTINGS_ENABLED=1` during a later authorized server release.
The switch defaults to off. Avito is independent of this switch.

For parts sourcing, use an exact OEM/article and part name when available.
Search Krasnoyarsk first, then broader regions if delivery is realistic. Search
and detail calls reject a full VIN, personal contacts and secrets. Results
carry source URL, observed time and a `lead` status. A listed price, stock or
compatibility claim requires live confirmation with the seller and vehicle
before it becomes a procurement offer. Only the authorized CRM/Store case may
retain selected business findings.

Both sources return the same listing keys: `source`, `listing_id`, `url`,
`title`, `description`, `price_rub`, `price_text`, `price_qualifier`, `city`,
`condition`, `seller`, `delivery`, `availability`, `published_at`,
`observed_at`, `status`, `fitment_confirmed`, and `availability_confirmed`.
`seller` is a public metadata object (`name`, `type`, `rating`,
`reviews_count`, `reviews`); `delivery` has `available` and `label`.
Unknown values are `null`, not inferred.

Webbee queues an asynchronous task. A successful start response means queued,
not completed. Poll `drom_get_parts_search` with the returned task ID and run
UID until completion, observing provider quota and avoiding duplicate starts.
`drom_start_parts_search` bounds the requested item and page counts with
`elementCountLimit` and `pageCountLimit`; whether the selected robot actually
follows every result page still needs a live sample.
Treat provider auth, quota, timeout, malformed output and an empty result as
distinct outcomes; none implies the MCP transport is down. The API may return
field names that differ across robots, so a new robot alias or changed export
requires a fixture update and a fresh live sample before relying on it.

The source release is separate from the installed Manager runtime. After a
later authorized coordinated server release, follow
[MCP activation checks](../../mcp_release_checks.md), reconnect Codex, verify
the four tool names in `tools/list`, and run one bounded provider call for each
source. The current task ends at GitHub publication; it does not activate the
running server.
