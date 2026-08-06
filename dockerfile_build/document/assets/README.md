# Document build assets

The Docker build uses local files first. Missing public inputs are downloaded
by `scripts/prepare_document_assets.sh`; no SHA256 checks are performed.

- PDF cache: `cases/pdf/input/of306_aug2023.pdf`
- XLSX finished cache: `cases/xlsx/input/monthly_operations_template.xlsx`
- XLSX source caches: `downloads/xlsx/yellow_tripdata_2024-01.parquet` and
  `downloads/xlsx/taxi_zone_lookup.csv`
- Per-architecture wheel caches: `wheels/arm64/` and `wheels/amd64/`
- Per-architecture websocat caches: `runtime/arm64/websocat` and
  `runtime/amd64/websocat` (used by `push_to_harbor.sh`, not the base image)
- Host-only recipes: `operations/` (excluded from Docker build context)

Sources are pinned by URL/version/commit and validated by file format,
required fields, package versions, and benchmark business constants. TLS
certificate verification is deliberately disabled for the target network.

The `skills/pdf/` and `skills/xlsx/` trees are accepted locally only when all
runtime documents and helper scripts required by the two recipes are present.
If both trees are absent they are downloaded from the pinned skills commit; a
partially uploaded tree fails the build and prints every missing required file.
