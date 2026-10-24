# Local changes to Cloudflare speedtest 1.13.0

The vendored MIT-licensed module limits each measurement to two retries, with 250 ms / 500 ms backoff. Cancelling clears the retry timer. Upstream retries immediately up to twenty times, including errors processing Resource Timing entries.

The dashboard limits individual transfers to 25 MB, and labels runs with failed measurements as partial/incomplete instead of clearing the error when other samples exist. Smaller transfer sizes can underestimate very fast connections; results remain browser-based internet throughput estimates.
