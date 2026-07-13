#!/bin/bash
# ============================================================
# Wikipedia Selected Page Downloader
# - Download one or more configurable HTML pages
# - Download images slowly
# - Fix image links for selected pages only
# - Check missing images and retry 3 rounds
# ============================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="${SCRIPT_DIR}/web_content"
HTML_DIR="$BASE_DIR/en.wikipedia.org/wiki"
IMG_DIR="$BASE_DIR/upload.wikimedia.org"

DOWNLOAD_LIST="$BASE_DIR/download_images.txt"
MISSING_LIST="$BASE_DIR/missing_images.txt"
FAILED_LIST="$BASE_DIR/failed_images.txt"
LOG_FILE="$BASE_DIR/download_page_.log"

MAX_FIX_ROUNDS=3

SLEEP_MIN=2
SLEEP_MAX=8
ROUND_SLEEP_MIN=20
ROUND_SLEEP_MAX=60

UA="WikipediaPageDownloader/1.0"

PAGE_SPEC=""
PAGES=()
HTML_FILES=()
ALL_PAGES=(
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

usage() {
    cat <<EOF
Usage:
  bash $(basename "$0")                         # Defaults to all 10 pages
  bash $(basename "$0") -p all
  bash $(basename "$0") -p PAGE_NAME
  bash $(basename "$0") -p PAGE_NAME_1,PAGE_NAME_2
  bash $(basename "$0") --help

Examples:
  bash $(basename "$0")                         # Download all 10 pages
  bash $(basename "$0") -p all
  bash $(basename "$0") -p List_of_paintings_by_Vincent_van_Gogh
  bash $(basename "$0") -p List_of_paintings_by_Vincent_van_Gogh, Weibo

Output directory:
  web_content

Available sample pages from the original list:
  https://en.wikipedia.org/wiki/China
  https://en.wikipedia.org/wiki/World_War_II
  https://en.wikipedia.org/wiki/United_States
  https://en.wikipedia.org/wiki/Hubble_Space_Telescope
  https://en.wikipedia.org/wiki/Solar_System
  https://en.wikipedia.org/wiki/Earth
  https://en.wikipedia.org/wiki/Human
  https://en.wikipedia.org/wiki/List_of_paintings_by_Vincent_van_Gogh
  https://en.wikipedia.org/wiki/Galaxy
  https://en.wikipedia.org/wiki/Weibo
EOF
}

if [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

while getopts ":p:h" opt; do
    case "$opt" in
        p)
            if [ -z "$PAGE_SPEC" ]; then
                PAGE_SPEC="$OPTARG"
            else
                PAGE_SPEC="$PAGE_SPEC,$OPTARG"
            fi
            ;;
        h)
            usage
            exit 0
            ;;
        :)
            echo "Option -$OPTARG requires an argument." >&2
            usage >&2
            exit 2
            ;;
        \?)
            echo "Unknown option: -$OPTARG" >&2
            usage >&2
            exit 2
            ;;
    esac
done

shift $((OPTIND - 1))
if [ "$#" -gt 0 ]; then
    if [ -z "$PAGE_SPEC" ]; then
        PAGE_SPEC="$*"
    else
        PAGE_SPEC="$PAGE_SPEC $*"
    fi
fi

normalize_page_name() {
    local page="$1"

    page=$(printf '%s' "$page" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    page="${page#https://en.wikipedia.org/wiki/}"
    page="${page#http://en.wikipedia.org/wiki/}"
    page="${page// /_}"

    printf '%s' "$page"
}

add_page() {
    local page="$1"
    local existing

    [ -n "$page" ] || return 0

    for existing in "${PAGES[@]}"; do
        if [ "$existing" = "$page" ]; then
            return 0
        fi
    done

    PAGES+=("$page")
    HTML_FILES+=("$HTML_DIR/$page.html")
}

add_all_pages() {
    local page

    for page in "${ALL_PAGES[@]}"; do
        add_page "$page"
    done
}

if [ -z "$PAGE_SPEC" ]; then
    PAGE_SPEC="all"
fi

IFS=',' read -r -a RAW_PAGES <<< "$PAGE_SPEC"
for raw_page in "${RAW_PAGES[@]}"; do
    page="$(normalize_page_name "$raw_page")"
    if [ "$page" = "all" ]; then
        add_all_pages
    else
        add_page "$page"
    fi
done

if [ "${#PAGES[@]}" -eq 0 ]; then
    echo "No valid page names found." >&2
    usage >&2
    exit 2
fi

mkdir -p "$HTML_DIR" "$IMG_DIR"
: > "$LOG_FILE"
: > "$FAILED_LIST"

log() {
    echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"
}

rand_sleep() {
    local min="$1"
    local max="$2"
    local span=$((max - min + 1))
    local sec=$((min + RANDOM % span))
    sleep "$sec"
}

download_url_to_path() {
    local url="$1"
    local local_path="$2"

    mkdir -p "$(dirname "$local_path")"

    if [ -s "$local_path" ]; then
        return 0
    fi

    wget --no-check-certificate \
        --continue \
        --tries=3 \
        --timeout=60 \
        --read-timeout=60 \
        --waitretry=20 \
        --limit-rate=150k \
        --user-agent="$UA" \
        -O "$local_path" \
        "$url" >> "$LOG_FILE" 2>&1

    local rc=$?

    if [ "$rc" -ne 0 ] || [ ! -s "$local_path" ]; then
        log "FAILED: $url"
        echo "$url" >> "$FAILED_LIST"
        rm -f "$local_path"
        return 1
    fi

    return 0
}

extract_image_urls_from_html_abs() {
    : > "$DOWNLOAD_LIST"

    local html_file

    for html_file in "${HTML_FILES[@]}"; do
        [ -f "$html_file" ] || continue

        grep -hoP '(https:|)//upload\.wikimedia\.org[^"'\'' <>)]+\.(jpg|jpeg|png|svg|gif|webp)(\?[^"'\'' <>)]*)?' "$html_file" 2>/dev/null \
            | sed 's|^//|https://|' \
            | sed 's|&amp;|\&|g' >> "$DOWNLOAD_LIST" || true
    done

    sort -u -o "$DOWNLOAD_LIST" "$DOWNLOAD_LIST" 2>/dev/null || true
}

fix_html_links() {
    local html_file
    local filename

    for html_file in "${HTML_FILES[@]}"; do
        [ -f "$html_file" ] || continue

        filename=$(basename "$html_file")
        log "Fixing links: $filename"

        sed -i 's|https://upload.wikimedia.org/|../../upload.wikimedia.org/|g' "$html_file"
        sed -i 's|//upload.wikimedia.org/|../../upload.wikimedia.org/|g' "$html_file"
    done
}

collect_missing_images_from_fixed_html() {
    : > "$MISSING_LIST"

    local html_file

    for html_file in "${HTML_FILES[@]}"; do
        [ -f "$html_file" ] || continue

        grep -o '\.\./\.\./upload\.wikimedia\.org/[^"'\'' <>)?]*' "$html_file" 2>/dev/null \
            | sed 's|^\.\./\.\./||' \
            | sed 's|&amp;|\&|g' \
            | sed 's|\?.*||' \
            | grep -Ei '\.(jpg|jpeg|png|svg|gif|webp)$' \
            | sort -u \
            | while read -r f; do
                [ -z "$f" ] && continue
                if [ ! -s "$BASE_DIR/$f" ]; then
                    echo "$f"
                fi
              done >> "$MISSING_LIST"
    done

    sort -u -o "$MISSING_LIST" "$MISSING_LIST" 2>/dev/null || true
}

print_integrity_summary() {
    log "=========================================="
    log "Image Integrity Summary"
    log "=========================================="

    local html_file
    local page
    local total
    local ok
    local miss

    for html_file in "${HTML_FILES[@]}"; do
        [ -f "$html_file" ] || continue

        page=$(basename "$html_file")
        total=0
        ok=0
        miss=0

        while read -r f; do
            [ -z "$f" ] && continue
            total=$((total + 1))
            if [ -s "$BASE_DIR/$f" ]; then
                ok=$((ok + 1))
            else
                miss=$((miss + 1))
            fi
        done < <(
            grep -o '\.\./\.\./upload\.wikimedia\.org/[^"'\'' <>)?]*' "$html_file" 2>/dev/null \
                | sed 's|^\.\./\.\./||' \
                | sed 's|&amp;|\&|g' \
                | sed 's|\?.*||' \
                | grep -Ei '\.(jpg|jpeg|png|svg|gif|webp)$' \
                | sort -u
        )

        log "$page TOTAL=$total OK=$ok MISS=$miss"
    done
}

log "=========================================="
log "Step 1: Download HTML Pages"
log "=========================================="

html_success=0

for i in "${!PAGES[@]}"; do
    page="${PAGES[$i]}"
    html_file="${HTML_FILES[$i]}"

    mkdir -p "$(dirname "$html_file")"

    log "Downloading HTML: $page"

    if wget --no-check-certificate \
        --tries=3 \
        --timeout=60 \
        --read-timeout=60 \
        --waitretry=20 \
        --user-agent="$UA" \
        -O "$html_file" \
        "https://en.wikipedia.org/wiki/$page" >> "$LOG_FILE" 2>&1; then
        log "OK HTML: $page"
        html_success=$((html_success + 1))
    else
        log "FAILED HTML: $page"
        rm -f "$html_file"
    fi

    rand_sleep "$SLEEP_MIN" "$SLEEP_MAX"
done

if [ "$html_success" -eq 0 ]; then
    log "No HTML pages downloaded successfully."
    exit 1
fi

log "Requested HTML pages: ${#PAGES[@]}"
log "Downloaded HTML pages: $html_success"

log "=========================================="
log "Step 2: Extract Image URLs"
log "=========================================="

extract_image_urls_from_html_abs

TOTAL_URLS=$(wc -l < "$DOWNLOAD_LIST" 2>/dev/null || echo 0)
log "Extracted image URL count: $TOTAL_URLS"

log "=========================================="
log "Step 3: Initial Image Download"
log "=========================================="

current=0

while read -r url; do
    [ -z "$url" ] && continue

    current=$((current + 1))
    rel="${url#https://}"
    rel="${rel%%\?*}"
    local_path="$BASE_DIR/$rel"

    log "Initial image progress: $current/$TOTAL_URLS $rel"

    if [ ! -s "$local_path" ]; then
        download_url_to_path "$url" "$local_path" || true
        rand_sleep "$SLEEP_MIN" "$SLEEP_MAX"
    fi
done < "$DOWNLOAD_LIST"

log "Actual image files after initial download: $(find "$IMG_DIR" -type f 2>/dev/null | wc -l)"

log "=========================================="
log "Step 4: Fix Image Links in HTML Files"
log "=========================================="

fix_html_links

log "=========================================="
log "Step 5: Retry Missing Images, Max Rounds=$MAX_FIX_ROUNDS"
log "=========================================="

previous_missing=-1
no_progress_rounds=0

for round in $(seq 1 "$MAX_FIX_ROUNDS"); do
    collect_missing_images_from_fixed_html
    missing_before=$(wc -l < "$MISSING_LIST" 2>/dev/null || echo 0)

    log "Round $round/$MAX_FIX_ROUNDS missing_before=$missing_before"

    if [ "$missing_before" -eq 0 ]; then
        log "All images complete before round $round."
        break
    fi

    if [ "$previous_missing" -eq "$missing_before" ]; then
        no_progress_rounds=$((no_progress_rounds + 1))
    else
        no_progress_rounds=0
    fi

    if [ "$no_progress_rounds" -ge 2 ]; then
        log "No progress for 2 consecutive scans. Stop retrying."
        break
    fi

    previous_missing="$missing_before"

    current=0
    success=0
    failed=0

    while read -r f; do
        [ -z "$f" ] && continue
        current=$((current + 1))

        url="https://$f"
        local_path="$BASE_DIR/$f"

        log "Retry round $round progress: $current/$missing_before $f"

        if download_url_to_path "$url" "$local_path"; then
            success=$((success + 1))
        else
            failed=$((failed + 1))
        fi

        rand_sleep "$SLEEP_MIN" "$SLEEP_MAX"
    done < "$MISSING_LIST"

    collect_missing_images_from_fixed_html
    missing_after=$(wc -l < "$MISSING_LIST" 2>/dev/null || echo 0)

    log "Round $round summary: success=$success failed=$failed missing_after=$missing_after"

    if [ "$missing_after" -eq 0 ]; then
        log "All images downloaded successfully."
        break
    fi

    if [ "$round" -lt "$MAX_FIX_ROUNDS" ]; then
        cooldown=$((ROUND_SLEEP_MIN + RANDOM % (ROUND_SLEEP_MAX - ROUND_SLEEP_MIN + 1)))
        log "Cooldown ${cooldown}s before next retry round."
        sleep "$cooldown"
    fi
done

collect_missing_images_from_fixed_html
final_missing=$(wc -l < "$MISSING_LIST" 2>/dev/null || echo 0)

sort -u -o "$FAILED_LIST" "$FAILED_LIST" 2>/dev/null || true

print_integrity_summary

log "=========================================="
log "Download Summary"
log "=========================================="
log "Requested HTML pages: ${#PAGES[@]}"
log "Downloaded HTML pages: $html_success"
log "Image files: $(find "$IMG_DIR" -type f 2>/dev/null | wc -l)"
log "Final missing images: $final_missing"
log "Failed URL list: $FAILED_LIST"
log "Missing image list: $MISSING_LIST"
log "Log file: $LOG_FILE"
log "Total size: $(du -sh "$BASE_DIR" 2>/dev/null | cut -f1)"

echo ""
echo "All done. Local preview command:"
echo "   cd $BASE_DIR && python3 -m http.server 8081"
echo "   Browser access:"
for page in "${PAGES[@]}"; do
    echo "      http://localhost:8081/en.wikipedia.org/wiki/$page.html"
done

if [ "$final_missing" -eq 0 ]; then
    exit 0
else
    exit 1
fi
