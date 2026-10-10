# Data layout

- `raw/` — local source downloads and temporary data; it is ignored by Git.
- `processed/` — local intermediate data; it is ignored by Git.
- `samples/` — small, documented samples safe to keep in Git. See
  [`data/samples/README.md`](samples/README.md) for the current inventory and fields.

## Sources and reproduction

The M0-1 pilot sample uses the «Если быть точным» intercity connectivity dataset,
OSM via Overpass API, and the public OSRM Table endpoint. To refresh the files:

```bash
py scripts/data/download_samples.py
py scripts/data/profile_samples.py
```

Optional environment variables: `OVERPASS_URL`, `OSRM_URL`,
`YANDEX_RASP_API_KEY`, and `YANDEX_RASP_DAILY_LIMIT` (capped at 500). The data
script also reads these from the ignored local `.env` file. With a Yandex key,
it resolves the 10 pilot settlements and requests direct services between
Moscow and the other pilot cities in both directions (at most 28 calls total).
Set `YANDEX_RASP_BYPASS_PROXY=1` to bypass configured HTTP(S) proxies for these
requests. The JSON records the request count and source attribution. Full-region
PBF data is intentionally not downloaded for this pilot.

Large PBF extracts, raw downloads, OSRM data, and intermediates must not be
committed. The ignore rules in `.gitignore` cover these locations and file types.
