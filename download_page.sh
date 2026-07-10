#!/bin/bash
# ============================================================
# Wikipedia Warmup Page Downloader
# ============================================================
set -e

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="${SCRIPT_DIR}/web_content"
HTML_DIR="$BASE_DIR/en.wikipedia.org/wiki"
IMG_DIR="$BASE_DIR/upload.wikimedia.org"

PAGES=(
    "China"
    "World_War_II"
    "United_States"
    "Hubble_Space_Telescope"
    "Solar_System"
    "Earth"
    "Human"
    "List_of_paintings_by_Vincent_van_Gogh"
    "Galaxy"
    "Weibo"
)

echo "=========================================="
echo "Step 1: Download HTML Pages"
echo "=========================================="

mkdir -p "$HTML_DIR"
cd "$BASE_DIR"

for page in "${PAGES[@]}"; do
    echo "Downloading HTML: $page"
    wget --quiet --no-check-certificate \
        --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" \
        -O "$HTML_DIR/$page.html" \
        "https://en.wikipedia.org/wiki/$page"
    sleep 2
done

echo "HTML file count: $(ls -1 "$HTML_DIR"/*.html 2>/dev/null | wc -l)"

echo ""
echo "=========================================="
echo "Step 2: Extract Image URLs"
echo "=========================================="

grep -hoP '(https:|)//upload\.wikimedia\.org[^"]+\.(jpg|jpeg|png|svg|gif|webp)' "$HTML_DIR"/*.html 2>/dev/null | \
    sort -u | sed 's|^//|https://|' > "$BASE_DIR/download_images.txt" || true

TOTAL_URLS=$(wc -l < "$BASE_DIR/download_images.txt" 2>/dev/null || echo 0)
echo "Extracted image URL count: $TOTAL_URLS"

echo ""
echo "=========================================="
echo "Step 3: Slow-rate Image Download"
echo "=========================================="

cd "$BASE_DIR"

mapfile -t IMAGE_URLS < "$BASE_DIR/download_images.txt"
TOTAL=${#IMAGE_URLS[@]}
CURRENT=0

if [ "$TOTAL" -eq 0 ]; then
    echo "Warning: No images found for download, skipping this step"
else
    echo "Total $TOTAL images, starting download..."
    echo ""

    show_progress() {
        local percent=$((CURRENT * 100 / TOTAL))
        local filled=$((percent / 2))
        local empty=$((50 - filled))
        printf "\r["
        printf "%${filled}s" | tr ' ' '='
        printf "%${empty}s" | tr ' ' '-'
        printf "] %3d%% (%d/%d)" "$percent" "$CURRENT" "$TOTAL"
    }

    for url in "${IMAGE_URLS[@]}"; do
        CURRENT=$((CURRENT + 1))
        fname=$(basename "$url")

        printf "\rProcessing (%d/%d): %-45s " "$CURRENT" "$TOTAL" "${fname:0:45}"

        # --directory-prefix="$BASE_DIR" + --force-directories
        # -> Automatically creates BASE_DIR/upload.wikimedia.org/wikipedia/... structure
        wget --no-check-certificate \
            --continue --no-clobber \
            --tries=2 \
            --timeout=30 \
            --wait=1 \
            --random-wait \
            --limit-rate=200k \
            --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" \
            --directory-prefix="$BASE_DIR" \
            --force-directories \
            "$url" > /dev/null 2>&1 || true

        # Refresh overall progress after current file is processed
        show_progress
    done

    echo ""
    echo "Image download complete!"
fi

echo "Actual image files: $(find "$IMG_DIR" -type f 2>/dev/null | wc -l)"

echo ""
echo "=========================================="
echo "Step 4: Fix Image Links in HTML Files"
echo "=========================================="

for html_file in "$HTML_DIR"/*.html; do
    [ -f "$html_file" ] || continue
    filename=$(basename "$html_file")
    echo "Fixing: $filename"
    # Convert absolute/protocol-relative paths to relative paths to match local directory structure
    sed -i 's|https://upload.wikimedia.org/|../../upload.wikimedia.org/|g' "$html_file"
    sed -i 's|//upload.wikimedia.org/|../../upload.wikimedia.org/|g' "$html_file"
done

echo ""
echo "=========================================="
echo "Download Summary"
echo "=========================================="

echo "HTML pages: $(ls -1 "$HTML_DIR"/*.html 2>/dev/null | wc -l)"
echo "Image files: $(find "$IMG_DIR" -type f 2>/dev/null | wc -l)"
echo "Total size: $(du -sh "$BASE_DIR" 2>/dev/null | cut -f1)"

echo ""
echo "All done! Local preview command:"
echo "   cd $BASE_DIR/en.wikipedia.org/wiki && python3 -m http.server 8081"
echo "   Browser access: http://localhost:8081/en.wikipedia.org/wiki/China.html"
