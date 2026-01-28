#!/bin/bash

# Network and container names
DOCKER_IMAGE="uvgcomm-docker"
NETWORK_NAME="uvgcomm-net"
HOST_NAME="uvgcomm-host"
CLIENT_PREFIX="uvgcomm-client"

# Inputs and configs
# 720p (Johnny) source/input
SOURCE_FILE_720_URL="https://media.xiph.org/video/derf/y4m/Johnny_1280x720_60.y4m"
SOURCE_FILE_720="./input/Johnny_1280x720_60.y4m"
INPUT_FILE_720="./input/Johnny_1280x720_30fps.yuv"

# 1080p (Beauty FullHD) source archive and input placeholder
SOURCE_FULLHD_URL="https://ultravideo.fi/video/Beauty_1920x1080_120fps_420_8bit_YUV_RAW.7z"
SOURCE_FULLHD_ARCHIVE="./input/Beauty_1920x1080_120fps_420_8bit_YUV_RAW.7z"
EXTRACTED_FULLHD_FILE="./input/Beauty_1920x1080.yuv"
INPUT_FILE_FULLHD="./input/Beauty_1920x1080_30fps.yuv"

# 4K (Beauty) source archive and input placeholder
SOURCE_4K_URL="https://ultravideo.fi/video/Beauty_3840x2160_120fps_420_8bit_YUV_RAW.7z"
SOURCE_4K_ARCHIVE="./input/Beauty_3840x2160_120fps_420_8bit_YUV_RAW.7z"
EXTRACTED_4K_FILE="./input/Beauty_3840x2160.yuv"
INPUT_FILE_4K="./input/Beauty_3840x2160_30fps.yuv"

CONFIG_FOLDER="./configs"

# Container paths
CONTAINER_HOST_SCRIPT_FILE="/uvgcomm/build/script.txt"
CONTAINER_CONFIG_FILE="/uvgcomm/build/uvgComm.ini"
CONTAINER_STATS_FOLDER="/uvgcomm/build/stats_csv"
CONTAINER_INPUT_FILE="/uvgcomm/input/input.yuv"

# Timestamped root folder for logs and stats
RUN_ID=$(date +"%Y%m%d_%H%M%S")
RUN_FOLDER="./results/$RUN_ID"

# Where to write the generated usernames list. 
USERS_FILE="./configs/usernames.conf"

# Host directory where core dumps will be stored. This can be overridden
# by exporting CORE_DIR_HOST before running the script. Default is
# /tmp/docker_cores which the user already created in the host.
CORE_DIR_HOST=${CORE_DIR_HOST:-/tmp/docker_cores}

# Default experiment parameters (can be overridden with env vars or CLI)
# RUN_COUNT: how many times to repeat each scenario
# CLIENTS_LIST: comma- or space-separated list of client counts to test
# ARCHS: comma-separated architectures to test, or the special value "all"
RUN_COUNT=${RUN_COUNT:-1}
CLIENTS_LIST=${CLIENTS_LIST:-"2,3,4,5,6"}
ARCHS=${ARCHS:-"P2P_Mesh,SFU,Hybrid"}
RESOLUTIONS=${RESOLUTIONS:-"1280x720"}

# Number of visible participants shown in gallery view. Template contains value 9.
# Can be overridden with -v on the CLI or via env var. Default is 9.
VISIBLE_PARTICIPANTS=${VISIBLE_PARTICIPANTS:-9}

# View mode selection: comma-separated or single value. Defaults to 'gallery'
VIEW_MODE=${VIEW_MODE:-gallery}

# Per-benchmark wait values (seconds). Edit these two values to tune timing.
# - WAIT_AFTER_INVITE: seconds to wait after each client is called
# - WAIT_AFTER_SETTINGS: seconds to wait after initial settings before calling clients
WAIT_AFTER_INVITE=15
WAIT_AFTER_SETTINGS=10

# Experiment length in seconds (default 60). Can be overridden with -e on CLI
EXPERIMENT_TIME=${EXPERIMENT_TIME:-60}

# Global warmup/cooldown times (seconds). Can be overridden via env vars.
WARMUP_TIME=${WARMUP_TIME:-10}
COOLDOWN_TIME=${COOLDOWN_TIME:-15}

# Latency simulation settings
# `-l` accepts one or more of: none,local,global (comma-separated). Default: none
LATENCY_MODES=${LATENCY_MODES:-none}

# Send bandwidth mode (affects per-client `upBandwidth` in generated configs)
# Allowed values:
#  - all1000 : every client gets 1000 Mbps (default)
#  - all10   : every client gets 10 Mbps
#  - all1    : every client gets 1 Mbps
#  - inc1    : each client gets i * 1 Mbps
#  - inc5    : each client gets i * 5 Mbps
#  - inc10   : each client gets i * 10 Mbps
SEND_BW_MODE=${SEND_BW_MODE:-all1000}


# ----------------------- functions ------------------------

usage() {
        cat <<EOF
Usage: $0 [-r RUNS] [-c CLIENTS] [-a ARCHS] [-s RESOLUTIONS] [-w VIEW] [-h]

Options:
    -r RUNS        Number of runs per scenario. Example: -r 3
    -c CLIENTS     Comma-separated client counts, e.g. "2,3,4". Defaults to ${CLIENTS_LIST}
    -a ARCHS       Comma separated architectures or "all" to include defaults (P2P_Mesh,SFU,Hybrid). Defaults to ${ARCHS}
    -s RESOLUTIONS  Comma separated resolutions to run, or "all" to include defaults (1280x720,3840x2160,1920x1080). Defaults to ${RESOLUTIONS}
    -w VIEW        View mode to use (gallery|speaker) or comma-separated list. Defaults to ${VIEW_MODE}
    -v VISIBLE     Number of visible participants in gallery view (default: ${VISIBLE_PARTICIPANTS}).
    -e SECONDS     Evaluation period in seconds (default: ${EXPERIMENT_TIME}).
    -l             Enable simulated per-client latency (uses built-in defaults).
    -b MODE        Send bandwidth mode (all1000|all1|inc1|inc5|inc10) or comma-separated list. Defaults to ${SEND_BW_MODE}
    -h             Show this help
EOF
}

format_duration_hms() {
    # Usage: format_duration_hms <total_seconds>
    # Outputs: "X days Y hours Z minutes W seconds"
    local total_seconds="$1"
    # guard against empty/non-numeric input
    if ! [[ "${total_seconds}" =~ ^[0-9]+$ ]]; then
        echo "0 days 0 hours 0 minutes 0 seconds"
        return 0
    fi

    local days=$(( total_seconds / 86400 ))
    local hours=$(( (total_seconds % 86400) / 3600 ))
    local minutes=$(( (total_seconds % 3600) / 60 ))
    local seconds=$(( total_seconds % 60 ))
    printf "%d days %d hours %d minutes %d seconds" "$days" "$hours" "$minutes" "$seconds"
}

# Small extra time added to the countdown to avoid cleaning up a run while the
# host script is still finishing. Scales slightly with client count.
timer_margin_ms() {
    local clients="$1"
    echo $((3000 + clients * 1000))
}

parse_args() {
    while getopts ":r:c:a:s:w:v:e:l:b:h" opt; do
        case ${opt} in
            r ) RUN_COUNT="$OPTARG" ;;
            c ) CLIENTS_LIST="$OPTARG" ;;
            a ) ARCHS="$OPTARG" ;;
            s ) RESOLUTIONS="$OPTARG" ;;
            w ) VIEW_MODE="$OPTARG" ;;
            v ) VISIBLE_PARTICIPANTS="$OPTARG" ;;
            e ) EXPERIMENT_TIME="$OPTARG" ;;
            l ) LATENCY_MODES="$OPTARG" ;;
            b ) SEND_BW_MODE="$OPTARG" ;;
            h ) usage; exit 0 ;;
            \? ) echo "Invalid Option: -$OPTARG" 1>&2; usage; exit 1 ;;
            : ) echo "Invalid Option: -$OPTARG requires an argument" 1>&2; usage; exit 1 ;;
        esac
    done

    # Normalize CLIENTS_LIST to comma-separated. Quote multi-value arguments to avoid shell splitting.
    CLIENTS_LIST=$(echo "$CLIENTS_LIST" | tr ' ' ',')

    # If user asked for 'all' architectures, expand to defaults
    if [ "$ARCHS" = "all" ]; then
        ARCHS="P2P_Mesh,SFU,Hybrid"
    fi

    # If user asked for 'all' resolutions, expand to defaults
    if [ "$RESOLUTIONS" = "all" ]; then
        RESOLUTIONS="1280x720,3840x2160,1920x1080"
    fi

    # Convert CLIENTS_LIST to array for iteration
    IFS=',' read -r -a CLIENTS_ARRAY <<< "$CLIENTS_LIST"

    # Compute MAX_CLIENTS as the maximum of CLIENTS_ARRAY (use provided clients as maximum)
    MAX_CLIENTS=0
    for c in "${CLIENTS_ARRAY[@]}"; do
        # only consider numeric entries
        if [[ "$c" =~ ^[0-9]+$ ]]; then
            if [ "$c" -gt "$MAX_CLIENTS" ]; then
                MAX_CLIENTS=$c
            fi
        fi
    done
    # fallback to 16 if nothing is found
    if [ "$MAX_CLIENTS" -le 0 ]; then
        MAX_CLIENTS=16
    fi
}

# Compact parameter validation function moved to functions section so the main
# script flow remains clean. This validates architectures, scenarios and
# client counts and exits with a one-line error on invalid input.
validate_params() {
    # Strict validation on the raw, normalized strings. Use regex to ensure
    # every token is exactly one of the allowed canonical values.
    local arch_re='^(P2P_Mesh|SFU|Hybrid)(,(P2P_Mesh|SFU|Hybrid))*$'
    local res_re='^(1280x720|3840x2160|1920x1080)(,(1280x720|3840x2160|1920x1080))*$'
    local clients_re='^[0-9]+(,[0-9]+)*$'

    # visible participants must be positive integer
    local visible_re='^[0-9]+$'

    # experiment time must be a positive integer (seconds)
    local exp_re='^[0-9]+$'

    if ! [[ "$ARCHS" =~ $arch_re ]]; then
        echo "ERROR: Unknown architecture '$ARCHS' (allowed: P2P_Mesh,SFU,Hybrid)" >&2; exit 1
    fi

    if ! [[ "$RESOLUTIONS" =~ $res_re ]]; then
        echo "ERROR: Unknown resolution '$RESOLUTIONS' (allowed: 1280x720,3840x2160,1920x1080)" >&2; exit 1
    fi

    # validate latency mode(s)
    local lat_re='^(none|local|global)(,(none|local|global))*$'
    if ! [[ "$LATENCY_MODES" =~ $lat_re ]]; then
        echo "ERROR: Unknown latency mode '$LATENCY_MODES' (allowed: none,local,global or comma-separated list)" >&2; exit 1
    fi

    # validate view mode(s)
    local view_re='^(gallery|speaker)(,(gallery|speaker))*$'
    if ! [[ "$VIEW_MODE" =~ $view_re ]]; then
        echo "ERROR: Unknown view mode '$VIEW_MODE' (allowed: gallery,speaker or comma-separated list)" >&2; exit 1
    fi

    # validate send bandwidth mode(s) (comma-separated values allowed)
    local bw_re='^(all1000|all10|all1|inc1|inc5|inc10)(,(all1000|all10|all1|inc1|inc5|inc10))*$'
    if ! [[ "$SEND_BW_MODE" =~ $bw_re ]]; then
        echo "ERROR: Unknown send bandwidth mode '$SEND_BW_MODE' (allowed: all1000,all1,inc1,inc5,inc10 or comma-separated list)" >&2; exit 1
    fi

    if ! [[ "$CLIENTS_LIST" =~ $clients_re ]]; then
        echo "ERROR: Invalid CLIENTS list '$CLIENTS_LIST' (must be comma-separated positive integers)" >&2; exit 1
    fi

    if ! [[ "$VISIBLE_PARTICIPANTS" =~ $visible_re ]]; then
        echo "ERROR: Invalid visible participants value '$VISIBLE_PARTICIPANTS' (must be a positive integer)" >&2; exit 1
    fi

    if ! [[ "$EXPERIMENT_TIME" =~ $exp_re ]]; then
        echo "ERROR: Invalid experiment time '$EXPERIMENT_TIME' (must be a positive integer seconds)" >&2; exit 1
    fi

    # Populate arrays for later iteration (safe now that inputs are validated)
    IFS=',' read -r -a ARCHS_ARRAY <<< "$ARCHS"
    IFS=',' read -r -a RESOLUTIONS_ARRAY <<< "$RESOLUTIONS"
    IFS=',' read -r -a VIEW_MODES_ARRAY <<< "$VIEW_MODE"
    IFS=',' read -r -a CLIENTS_ARRAY <<< "$CLIENTS_LIST"
    IFS=',' read -r -a SEND_BW_ARRAY <<< "$SEND_BW_MODE"
}

prepare_tests() {
    # do all actions in preparation for tests

    # Create network if it does not exist
    docker network inspect $NETWORK_NAME >/dev/null 2>&1 || \
        docker network create --subnet=172.28.0.0/16 $NETWORK_NAME

    mkdir -p "input"

    # ensure ffmpeg and wget exist
    if ! command -v ffmpeg >/dev/null 2>&1; then
        echo "ERROR: ffmpeg not found. Please install ffmpeg."
        exit 1
    fi
    if ! command -v wget >/dev/null 2>&1; then
        echo "ERROR: wget not found. Please install wget."
        exit 1
    fi

    # For 7z extraction we prefer the `7z` utility (p7zip-full). If not present we will try `p7zip`.
    if ! command -v 7z >/dev/null 2>&1 && ! command -v p7zip >/dev/null 2>&1; then
        echo "ERROR: 7z (p7zip) not found. Please install p7zip-full (provides '7z')."
        exit 1
    fi

    # Download / prepare 720p Johnny video if missing
    if [ ! -f "$SOURCE_FILE_720" ]; then
        echo "Downloading Johnny test video..."
        wget -O "$SOURCE_FILE_720" "$SOURCE_FILE_720_URL"
    fi

    local frame_rate=30

    if [ ! -f "$INPUT_FILE_720" ]; then
        echo "Converting source file $SOURCE_FILE_720 to $INPUT_FILE_720"
        ffmpeg -y -i "$SOURCE_FILE_720" \
               -vf "select='not(mod(n,2))',setpts=N/($frame_rate*TB)" \
               -vsync vfr \
               -c:v rawvideo \
               -pix_fmt yuv420p \
               "$INPUT_FILE_720"
    fi

    # Prepare FullHD (1920x1080) using helper
    prepare_archive "$SOURCE_FULLHD_ARCHIVE" "$SOURCE_FULLHD_URL" "$EXTRACTED_FULLHD_FILE" "$INPUT_FILE_FULLHD" 1920 1080 120

    # Prepare 4K (3840x2160) using helper
    prepare_archive "$SOURCE_4K_ARCHIVE" "$SOURCE_4K_URL" "$EXTRACTED_4K_FILE" "$INPUT_FILE_4K" 3840 2160 120

    mkdir -p "$RUN_FOLDER"
    # Generate usernames.conf once here (not per-scenario). Use MAX_CLIENTS as
    # a reasonable upper bound; the file will be written in configs/.
    generate_usernames "$MAX_CLIENTS"


    # Generate latency files for requested modes (one-time per run folder)
    IFS=',' read -r -a _modes <<< "${LATENCY_MODES}"
    for _mode in "${_modes[@]}"; do
        # only generate files for actual latency modes
        if [ "${_mode}" != "none" ]; then
            generate_latencies "$RUN_FOLDER" "$MAX_CLIENTS" "${_mode}" || true
        fi
    done

    echo "Preparation complete. Input files ready: $INPUT_FILE_720, $INPUT_FILE_FULLHD and $INPUT_FILE_4K"
}

write_metadata() {
    local scenario="$1"
    local architecture="$2"
    local clients="$3"
    local resolution="$4"
    local download_bw="$5"
    local upload_mode="$6"
    local latency="$7"
    local view_mode="$8"
    local output_folder="$9"
    local start="${10}"
    local end="${11}"
    local run="${12}"


    local metadata_file="${output_folder}/metadata.txt"

    # Create output folder if it doesn't exist
    mkdir -p "$output_folder"

    echo "Writing metadata to $metadata_file..."

    {
        echo "Scenario: $scenario"
        echo "Architecture: $architecture"
        echo "Clients: $clients"
        echo "Resolution: $resolution"
        echo "Download_BW: ${download_bw} Mbps"
        echo "Upload_BW_Mode: ${upload_mode}"
        echo "Simulated_latencies: $latency"
        echo "View_Mode: $view_mode"
        echo "Visible_Participants: ${VISIBLE_PARTICIPANTS}"
        echo "Start_Timestamp: $start"
        echo "End_timestamp: $end"
        echo "Run: $run"
    } > "$metadata_file"

    echo "Metadata written successfully."
}

write_run_parameters() {
    # Minimal, quick-glance run parameters file (CLI + a couple of basics)
    local out_file="${RUN_FOLDER}/run_params.txt"
    mkdir -p "${RUN_FOLDER}"

    {
        echo "cmd: ${ORIGINAL_CMDLINE-}"
        echo "opts: RUN_COUNT=${RUN_COUNT} CLIENTS_LIST=${CLIENTS_LIST} ARCHS=${ARCHS} RESOLUTIONS=${RESOLUTIONS} VIEW_MODE=${VIEW_MODE} VISIBLE_PARTICIPANTS=${VISIBLE_PARTICIPANTS} EXPERIMENT_TIME=${EXPERIMENT_TIME} LATENCY_MODES=${LATENCY_MODES} SEND_BW_MODE=${SEND_BW_MODE}"
        echo "inputs: 720=${INPUT_FILE_720} 1080=${INPUT_FILE_FULLHD} 4k=${INPUT_FILE_4K}"
        echo "estimated_total: ${total_hms:-unknown}"
        echo "start_ms: ${GLOBAL_START_MS-}"
    } > "${out_file}"
}

generate_usernames() {
    # Generates the usernames.conf file dynamically so the user doesn't need to
    # maintain it manually. The format is:
    # host=host:172.28.0.2
    # client1=user1:172.28.0.3
    local num_clients="$1"
    local users_file="${USERS_FILE}"

    # Ensure directory exists for the users file
    mkdir -p "$(dirname "${users_file}")" 2>/dev/null || true

    # Write host line
    echo "host=host:172.28.0.2" > "${users_file}"

    # Write client lines
    for i in $(seq 1 "$num_clients"); do
        # client IPs follow the same scheme as docker run calls: 172.28.0.$((2+i))
        echo "client${i}=user${i}:172.28.0.$((2 + i))" >> "${users_file}"
    done

    echo "Generated ${users_file} with ${num_clients} clients"
}

# Helper: download, extract and convert a raw YUV archive to a 30fps input file
# Usage: prepare_archive <archive_path> <archive_url> <extracted_yuv> <out_input_yuv> <width> <height> [src_framerate]
prepare_archive() {
    local archive_path="$1"
    local archive_url="$2"
    local extracted_yuv="$3"
    local out_input="$4"
    local width="$5"
    local height="$6"
    local src_framerate="${7:-120}"

    # Download archive if missing
    if [ ! -f "$archive_path" ]; then
        wget -O "$archive_path" "$archive_url"
    fi

    # Extract if we have the archive but not the extracted YUV
    if [ ! -f "$extracted_yuv" ] && [ -f "$archive_path" ]; then
        if command -v 7z >/dev/null 2>&1; then
            7z x -y -o"input" "$archive_path"
        else
            p7zip -d "$archive_path" || true
        fi
    fi

    # Try to find an extracted file matching the resolution if extraction was lossy
    if [ ! -f "$extracted_yuv" ]; then
        extracted=$(find input -maxdepth 1 -type f -iname "*${width}*${height}*.yuv" -print -quit)
        if [ -n "$extracted" ]; then
            mv "$extracted" "$extracted_yuv" 2>/dev/null || true
        fi
    fi

    # Convert to target 30fps input if needed and extracted file exists
    if [ ! -f "$out_input" ] && [ -f "$extracted_yuv" ]; then
        # compute frame selection modulus (e.g., 120->30 means mod=4)
        local mod=1
        if [ -n "$frame_rate" ] && [ "$frame_rate" -gt 0 ] 2>/dev/null; then
            mod=$(( src_framerate / frame_rate ))
            if [ "$mod" -lt 1 ]; then
                mod=1
            fi
        else
            mod=$(( src_framerate / 30 ))
            if [ "$mod" -lt 1 ]; then
                mod=1
            fi
        fi
        ffmpeg -y -f rawvideo -pixel_format yuv420p -video_size ${width}x${height} -framerate ${src_framerate} -i "$extracted_yuv" \
            -vf "select='not(mod(n,${mod}))',setpts=N/(${frame_rate}*TB)" -vsync vfr -c:v rawvideo -pix_fmt yuv420p "$out_input"
    fi
}

generate_client_configs() {
    # Generate per-client configs from template configs/uvgComm_client.ini.
    # Idempotent: overwrite files so per-scenario settings apply.
    # Usage: generate_client_configs [send_bw_mode] [view_mode] [num_clients]
    # If send_bw_mode is not provided, fall back to global SEND_BW_MODE.
    # If view_mode is not provided, fall back to global VIEW_MODE.
    # If num_clients is not provided, fall back to global MAX_CLIENTS.
    local send_bw_mode="${1:-${SEND_BW_MODE}}"
    local view_mode="${2:-${VIEW_MODE}}"
    local num_clients="${3:-${MAX_CLIENTS}}"
    local template="${CONFIG_FOLDER}/uvgComm_client.ini"
    mkdir -p "${CONFIG_FOLDER}"

    if [ ! -f "${template}" ]; then
        echo "ERROR: Client template '${template}' not found. Please create it." >&2
        exit 1
    fi

    # Determine conferenceMode value and per-client speakerMode:
    # conferenceMode: 'Speaker' or 'Gallery'
    local conference_mode_value="Gallery"
    if [[ "${view_mode}" == *speaker* ]]; then
        conference_mode_value="Speaker"
    fi

    for i in $(seq 1 "${num_clients}"); do
        local out="${CONFIG_FOLDER}/uvgComm${i}.ini"

        # Always overwrite existing client config to reflect current settings
        cp "${template}" "${out}"

        # Replace Username=, ServerAddress=, Name= and visibleParticipants= if present, else error out.
        if grep -qE '^Username=' "${out}"; then
            sed -i "s/^Username=.*/Username=user${i}/" "${out}"
        else
            echo "ERROR: '${template}' (copied to ${out}) does not contain the key 'Username'. Exiting." >&2
            exit 1
        fi

        if grep -qE '^ServerAddress=' "${out}"; then
            sed -i "s/^ServerAddress=.*/ServerAddress=172.28.0.$((2 + i))/" "${out}"
        else
            echo "ERROR: '${template}' (copied to ${out}) does not contain the key 'ServerAddress'. Exiting." >&2
            exit 1
        fi

        if grep -qE '^Name=' "${out}"; then
            sed -i "s/^Name=.*/Name=user${i}/" "${out}"
        else
            echo "ERROR: '${template}' (copied to ${out}) does not contain the key 'Name'. Exiting." >&2
            exit 1
        fi

        if grep -qE '^visibleParticipants=' "${out}"; then
            sed -i "s/^visibleParticipants=.*/visibleParticipants=${VISIBLE_PARTICIPANTS}/" "${out}"
        else
            echo "ERROR: '${template}' (copied to ${out}) does not contain the key 'visibleParticipants'. Exiting." >&2
            exit 1
        fi

        # conferenceMode is a top-level key in the template; overwrite it like visibleParticipants
        if grep -qE '^conferenceMode=' "${out}"; then
            sed -i "s/^conferenceMode=.*/conferenceMode=${conference_mode_value}/" "${out}"
        else
            echo "ERROR: '${template}' (copied to ${out}) does not contain the key 'conferenceMode'. Exiting." >&2
            exit 1
        fi

        # speakerMode indicates the current speaker and must be true only for the first client
        if grep -qE '^speakerMode=' "${out}"; then
            if [ "${i}" -eq 1 ] 2>/dev/null; then
                sed -i "s/^speakerMode=.*/speakerMode=true/" "${out}"
            else
                sed -i "s/^speakerMode=.*/speakerMode=false/" "${out}"
            fi
        else
            echo "ERROR: '${template}' (copied to ${out}) does not contain the key 'speakerMode'. Exiting." >&2
            exit 1
        fi

        # media IP
        client_ip="172.28.0.$((2 + i))"

        if grep -qE '^\[sip\]' "${out}"; then
            sed -i "/^\[sip\]/a localAddress=${client_ip}" "${out}"
        else
            printf "\n[sip]\nlocalAddress=%s\n" "${client_ip}" >> "${out}"
        fi

        # Compute upload bandwidth according to selected send_bw_mode
        local upload_bps=0
        case "${send_bw_mode}" in
            all1000)
                upload_bps=$((1000 * 1000000))
                ;;
            all10)
                upload_bps=$((10 * 1000000))
                ;;
            all1)
                upload_bps=$((1 * 1000000))
                ;;
            inc1)
                upload_bps=$(( i * 1000000 ))
                ;;
            inc5)
                upload_bps=$(( i * 5000000 ))
                ;;
            inc10)
                upload_bps=$(( i * 10000000 ))
                ;;
            *)
                # Fallback to the default all1000 behavior instead of an unexpected incremental value
                upload_bps=$((1000 * 1000000))
                ;;
        esac
        if grep -qE '^upBandwidth=' "${out}"; then
            sed -i "s/^upBandwidth=.*/upBandwidth=${upload_bps}/" "${out}"
        else
            if grep -qE '^\[sip\]' "${out}"; then
                sed -i "/^\[sip\]/a upBandwidth=${upload_bps}" "${out}"
            else
                printf "\n[sip]\nupBandwidth=%s\n" "${upload_bps}" >> "${out}"
            fi
        fi

        echo "Assigned upBandwidth=${upload_bps} to ${out}"
    done

    echo "Generated per-client configs up to ${num_clients} in ${CONFIG_FOLDER}"
}

create_clients() {
    local num_clients="$1"
    local input_file="$2"
    local output_folder="$3"
    local LATENCY_MODE="${4:-}"

    for i in $(seq 1 "$num_clients"); do
        CONTAINER_NAME="${CLIENT_PREFIX}${i}"
        local config_file="${CONFIG_FOLDER}/uvgComm${i}.ini"
        local client_output="${output_folder}/${CLIENT_PREFIX}$i"

        mkdir -p "$client_output"
        echo "Starting client $i"
        docker run -d --name "${CONTAINER_NAME}" --network "${NETWORK_NAME}" --ip "172.28.0.$((2+i))" \
            --cap-add=NET_ADMIN \
            -v "${input_file}:${CONTAINER_INPUT_FILE}:ro" \
            -v "${config_file}:${CONTAINER_CONFIG_FILE}" \
            -v "${client_output}:${CONTAINER_STATS_FOLDER}" \
            -v "${CORE_DIR_HOST}:/cores" \
            --ulimit core=-1 \
            -e CORE_DUMP_DIR=/cores \
            -e KV_HEADLESS_FORCE_OFFSCREEN=1 \
            "${DOCKER_IMAGE}:latest" \
            --stats=${CONTAINER_STATS_FOLDER} \
            --siplog=${CONTAINER_STATS_FOLDER}/siplog.txt

            # apply in-container latency if requested (LATENCY_MODE must be local/global)
            if [ -n "${LATENCY_MODE}" ] && [ "${LATENCY_MODE}" != "none" ]; then
            local lat_ms
            local LATENCY_FILE="${RUN_FOLDER}/latencies_${LATENCY_MODE}.txt"
            if [ -f "$LATENCY_FILE" ]; then
                lat_ms=$(sed -n "$((i+1))p" "$LATENCY_FILE" 2>/dev/null || echo "")
            else
                lat_ms=""
            fi
            if [ -n "$lat_ms" ] && [ "$lat_ms" -gt 0 ] 2>/dev/null; then
                # detect primary interface inside container
                net_if=$(docker exec "$CONTAINER_NAME" sh -c 'ls /sys/class/net | grep -v lo | head -n1' 2>/dev/null || echo "")
                if [ -n "$net_if" ]; then
                    # Increase tx queue length to reduce packet drops when netem adds delay
                    docker exec "$CONTAINER_NAME" ip link set dev "$net_if" txqueuelen 1000 2>/dev/null || true
                    # Use a larger qdisc limit so many simultaneous streams don't overflow the netem queue
                    docker exec "$CONTAINER_NAME" tc qdisc replace dev "$net_if" root netem delay "${lat_ms}ms" limit 10000 2>/dev/null || true
                fi
            fi
        fi
    done
}

create_host_script() {
    local output_file=$1
    local architecture=$2
    local clients=$3
    local resolution=$4
    local download_bw=$5
    local warmup_duration_ms=$6
    local experiment_duration_ms=$7
    local cooldown_duration_ms=$8

    # Use globally-configured wait times (default set at top of file)
    local wait_after_invite=${WAIT_AFTER_INVITE}
    local wait_after_settings=${WAIT_AFTER_SETTINGS}


    echo "# Auto-generated host script" > "$output_file"
    echo "setting sip/Topology $architecture" >> "$output_file"

    # parse resolution WIDTHxHEIGHT
    if [[ "$resolution" == *x* ]]; then
        local width="${resolution%x*}"
        local height="${resolution#*x}"
    else
        local width="0"
        local height="0"
    fi

    # Convert provided download bandwidth value (given as Mbps, possibly floats like 1.0)
    # to integer bits-per-second expected by the host. Use 1 Mbps = 1,000,000 bps.
    local download_bps
    # Use awk for floating point multiplication and integer formatting
    download_bps=$(awk "BEGIN {printf \"%d\", ($download_bw) * 1000000}")

    # Compute per-media bitrates. Template assumes download bandwidth reflects
    # the conference target bitrate and audio is fixed at 24 kbps.
    local conference_bps=$download_bps
    local audio_bitrate=24000
    local overhead_bps=$((conference_bps * 5 / 100))
    local video_bitrate=$((conference_bps - audio_bitrate - overhead_bps))
    if [ "$video_bitrate" -lt 0 ]; then
        video_bitrate=0
    fi

    echo "setting audio/bitrate $audio_bitrate" >> "$output_file"
    echo "setAudio" >> "$output_file"

    echo "setting video/FileResolutionWidth $width" >> "$output_file"
    echo "setting video/FileResolutionHeight $height" >> "$output_file"
    echo "setting video/bitrate $video_bitrate" >> "$output_file"
    echo "setVideo" >> "$output_file"

    echo "wait $wait_after_settings" >> "$output_file"

    # Read clients from USERS_FILE
    local client_count=0
    while IFS= read -r line; do
        [[ "$line" =~ ^#.*$ ]] && continue        # skip comments
        [[ "$line" =~ ^host=.*$ ]] && continue    # skip host line
        client_count=$((client_count + 1))
        [[ $client_count -gt $clients ]] && break # only do set amount of clients

        client_user="${line#*=}"
        client_name="${client_user%%:*}"
        client_ip="${client_user##*:}"

        # Wait before calling next client
        echo "call $client_name $client_ip" >> "$output_file"
        echo "wait $wait_after_invite" >> "$output_file"
    done < "$USERS_FILE"

    # Warmup period (after all clients have been invited)
    # Convert milliseconds to seconds (round down)
    local warmup_s=$((warmup_duration_ms / 1000))
    echo "wait $warmup_s" >> "$output_file"

    # Experiment period
    local experiment_s=$((experiment_duration_ms / 1000))
    echo "# Experiment running for $experiment_s seconds" >> "$output_file"
    echo "wait $experiment_s" >> "$output_file"

    # Hangup and quit
    echo "hangup" >> "$output_file"
    local cooldown_s=$((cooldown_duration_ms / 1000))
    echo "wait $cooldown_s" >> "$output_file"
    echo "quit" >> "$output_file"

    echo "Host script generated at: $output_file"
}

create_host() {
    local script_file="$1/${HOST_NAME}/script.txt"
    local architecture="$2"
    local clients="$3"
    local resolution="$4"
    local download_bw="$5"
    local warmup_time_ms="$6"
    local experiment_time_ms="$7"
    local cooldown_time_ms="$8"
    local latency_mode_param="${9:-}"
    mkdir -p "$(dirname "${script_file}")"

    # Note: create_host_script already accounts for the setup phase via
    # WAIT_AFTER_SETTINGS and WAIT_AFTER_INVITE waits. We only pass the actual
    # warmup/experiment/cooldown durations here.
    create_host_script "${script_file}" "${architecture}" "${clients}" "${resolution}" "${download_bw}" "${warmup_time_ms}" "${experiment_time_ms}" "${cooldown_time_ms}"

    local SFU_CPUSET="0"
    local SFU_CPUS=""
    local SFU_NOFILE=65536

    echo "Starting host"
    docker run -d --name "$HOST_NAME" --network "$NETWORK_NAME" --ip "172.28.0.2" \
        --cap-add=NET_ADMIN \
        --cpuset-cpus="${SFU_CPUSET}" \
        $( [ -n "${SFU_CPUS}" ] && printf '--cpus="%s" \\' "${SFU_CPUS}" || true ) \
        -v "${CONFIG_FOLDER}/uvgComm_host.ini:${CONTAINER_CONFIG_FILE}" \
        -v "${script_file}:${CONTAINER_HOST_SCRIPT_FILE}" \
        -v "${CORE_DIR_HOST}:/cores" \
        --ulimit core=-1 \
        --ulimit nofile=${SFU_NOFILE}:${SFU_NOFILE} \
        -e CORE_DUMP_DIR=/cores \
        "${DOCKER_IMAGE}:latest" --script "$CONTAINER_HOST_SCRIPT_FILE"

    # Non-fatal attempt: try to set net.core sysctls inside the container.
    # Some Docker setups (rootless) prevent passing --sysctl at docker create
    # time and will fail. Try applying them post-start, ignore failures.
    docker exec "$HOST_NAME" sh -c 'sysctl -w net.core.rmem_max=16777216' 2>/dev/null || true
    docker exec "$HOST_NAME" sh -c 'sysctl -w net.core.wmem_max=16777216' 2>/dev/null || true
    docker exec "$HOST_NAME" sh -c 'sysctl -w net.core.netdev_max_backlog=250000' 2>/dev/null || true

    # apply in-container host/SFU latency if requested
    if [ -n "${latency_mode_param}" ] && [ "${latency_mode_param}" != "none" ]; then
        local LATENCY_FILE="${RUN_FOLDER}/latencies_${latency_mode_param}.txt"
        if [ -f "$LATENCY_FILE" ]; then
            host_lat=$(sed -n '1p' "$LATENCY_FILE" 2>/dev/null || echo "")
        else
            host_lat=""
        fi
        if [ -n "$host_lat" ] && [ "$host_lat" -gt 0 ] 2>/dev/null; then
            # detect primary interface inside host container
            host_if=$(docker exec "$HOST_NAME" sh -c 'ls /sys/class/net | grep -v lo | head -n1' 2>/dev/null || echo "")
            if [ -n "$host_if" ]; then
                # Increase tx queue length on the host side of the container
                docker exec "$HOST_NAME" ip link set dev "$host_if" txqueuelen 1000 2>/dev/null || true
                # Use larger qdisc limit to avoid buffer overflows with many participant streams
                docker exec "$HOST_NAME" tc qdisc replace dev "$host_if" root netem delay "${host_lat}ms" limit 10000 2>/dev/null || true
            fi
        fi
    fi
}

countdown_timer() {
    local output_location=$1
    local start_time_ms=$2
    local setup_time_ms=$3
    local warmup_time_ms=$4
    local experiment_time_ms=$5
    local cooldown_time_ms=$6
    local margin_ms=${7:-0}

    # Total duration in ms
    local total_duration_ms=$((setup_time_ms + warmup_time_ms + experiment_time_ms + cooldown_time_ms + margin_ms))

    CPU_LOG="${output_location}/cpu_usage.csv"
    echo "timestamp_ms;cpu_percent" > "$CPU_LOG"

    echo "Starting countdown timer..."

    local last_phase=""

    while true; do
        local now_ms
        now_ms=$(($(date +%s%N)/1000000))
        local elapsed_ms=$((now_ms - start_time_ms))

        # Stop when total duration exceeded
        if [ $elapsed_ms -ge $total_duration_ms ]; then
            break
        fi

        # Determine phase
        local phase=""
        if [ "$elapsed_ms" -lt "$setup_time_ms" ]; then
            phase="Setup ${setup_time_ms} ms"
        elif [ "$elapsed_ms" -lt $((setup_time_ms + warmup_time_ms)) ]; then
            phase="Warmup ${warmup_time_ms} ms"
        elif [ "$elapsed_ms" -lt $((setup_time_ms + warmup_time_ms + experiment_time_ms)) ]; then
            phase="Experiment ${experiment_time_ms} ms"
        else
            phase="Cooldown ${cooldown_time_ms} ms"
        fi

        # Print phase only when it changes
        if [ "$phase" != "$last_phase" ]; then
            echo "[$(date '+%H:%M:%S')] Phase: $phase"
            last_phase="$phase"
        fi

        # CPU measurement
        local CPU
        CPU=$(mpstat 1 1 | awk '/Average/ {print 100-$12}' | tr ',' '.')

        # Log to file
        echo "$now_ms;$CPU" >> "$CPU_LOG"

        # Simple inline print (no formatting headaches)
        echo "${now_ms}: $CPU%"


        # Sleep until the next millisecond boundary (approx 1s intervals)
        local next_ms=$((start_time_ms + ((elapsed_ms/1000)+1)*1000))
        local sleep_ms=$((next_ms - $(($(date +%s%N)/1000000))))
        if [ "$sleep_ms" -gt 0 ]; then
            sleep "$(awk "BEGIN {print $sleep_ms/1000}")"
        fi

    done
    echo "Countdown finished"
}

record_container_logs() {
    local output_location=$1
    echo "Recording logs"
    for i in $(seq 1 "$CLIENTS"); do
        docker logs "${CLIENT_PREFIX}${i}" &> "${output_location}/${CLIENT_PREFIX}${i}/docker.log"
    done
    mkdir -p "${output_location}/${HOST_NAME}"
    docker logs "$HOST_NAME" &> "${output_location}/${HOST_NAME}/${HOST_NAME}.log"
}


# Bandwidth monitor: polls per-container rx/tx byte counters inside each
# container and writes CSV files with timestamp, cumulative bytes and
# bytes/second deltas. Uses docker exec to read
# /sys/class/net/eth0/statistics/{rx,tx}_bytes which gives raw counters.
# The monitor runs in background and is signalled to stop by creating a
# stopfile in the run output folder.
start_bandwidth_monitor() {
    local output_location="$1"
    local interval=${2:-1}  # seconds between polls

    # Stopfile used to terminate the background loop.
    BW_MONITOR_STOPFILE="${output_location}/.bw_monitor_stop"
    # ensure previous stopfile removed
    [ -f "$BW_MONITOR_STOPFILE" ] && rm -f "$BW_MONITOR_STOPFILE"

    # Log file for monitor internal messages
    BW_MONITOR_LOG="${output_location}/bandwidth_monitor.log"
    : > "$BW_MONITOR_LOG"

    (
        # containers list (clients then host)
        containers=()
        for i in $(seq 1 "$CLIENTS"); do
            containers+=("${CLIENT_PREFIX}${i}")
        done
        containers+=("${HOST_NAME}")

        # Initialize CSVs and read initial counters
        prev_rx=()
        prev_tx=()
        net_ifs=()
        for idx in "${!containers[@]}"; do
            c=${containers[$idx]}
            mkdir -p "${output_location}/${c}"
            csv="${output_location}/${c}/bandwidth.csv"
            echo "timestamp_ms;rx_bytes;tx_bytes;rx_bps;tx_bps" > "$csv"

            # Wait briefly for container to appear/finish starting
            attempts=0
                    while [ "$attempts" -lt 10 ]; do
                if docker ps -q -f name="^/${c}$" >/dev/null 2>&1 && [ -n "$(docker ps -q -f name="^/${c}$")" ]; then
                    break
                fi
                attempts=$((attempts+1))
                sleep 0.3
            done

            # detect the primary interface inside the container (non-loopback)
            net_if=$(docker exec "$c" sh -c 'ls /sys/class/net | grep -v lo | head -n1' 2>/dev/null || echo "")
            if [ -z "$net_if" ]; then
                net_if="eth0"
            fi

            # store detected interface per-container for later polling
            net_ifs[$idx]="$net_if"

            rx=$(docker exec "$c" cat /sys/class/net/${net_if}/statistics/rx_bytes 2>/dev/null || echo 0)
            tx=$(docker exec "$c" cat /sys/class/net/${net_if}/statistics/tx_bytes 2>/dev/null || echo 0)
            prev_rx[$idx]=$rx
            prev_tx[$idx]=$tx
        done

        # Poll loop
        while [ ! -f "$BW_MONITOR_STOPFILE" ]; do
            now_ms=$(date +%s%3N)
            for idx in "${!containers[@]}"; do
                c=${containers[$idx]}
                net_if=${net_ifs[$idx]:-eth0}
                rx=$(docker exec "$c" cat /sys/class/net/${net_if}/statistics/rx_bytes 2>/dev/null || true)
                tx=$(docker exec "$c" cat /sys/class/net/${net_if}/statistics/tx_bytes 2>/dev/null || true)

                # If container failed/stopped, exit the monitor
                if [[ ! "$rx" =~ ^[0-9]+$ ]] || [[ ! "$tx" =~ ^[0-9]+$ ]]; then
                    echo "ERROR: Container $c failed at ${now_ms}. Exiting bandwidth monitor." | tee -a "$BW_MONITOR_LOG" >&2
                    break 2
                fi

                prx=${prev_rx[$idx]:-0}
                ptx=${prev_tx[$idx]:-0}
                drx=$((rx - prx))
                dtx=$((tx - ptx))

                # bytes per second (approx). If interval >1, this is averaged.
                interval_s=${interval:-1}
                if [[ ! "$interval_s" =~ ^[0-9]+$ ]] || [ "$interval_s" -le 0 ]; then
                    interval_s=1;
                fi

                rx_bps=$(( drx / interval_s ))
                tx_bps=$(( dtx / interval_s ))

                echo "${now_ms};${rx};${tx};${rx_bps};${tx_bps}" >> "${output_location}/${c}/bandwidth.csv"

                prev_rx[$idx]=$rx
                prev_tx[$idx]=$tx
            done
            sleep "$interval"
        done

        echo "Bandwidth monitor loop exiting" >> "$BW_MONITOR_LOG"
    ) &

    BW_MONITOR_PID=$!
    echo "Started bandwidth monitor (pid=$BW_MONITOR_PID) -> ${output_location}"
}

generate_latencies() {
    # Usage: generate_latencies <out_dir> <num_clients> <scenario>

    local out_dir="$1"
    local num_clients=${2:-$CLIENTS}
    local scenario_raw="$3"

    # If no output directory provided, print usage hint and return
    if [ -z "$out_dir" ]; then
        echo "LATENCY_MODES set but no output dir provided. Call with: generate_latencies <out_dir> <num_clients> <scenario>"
        return 0
    fi

    # Determine scenario kind: local vs global. Default to global for 'simlat'.
    local kind="global"
    if [ -n "$scenario_raw" ]; then
        case "$scenario_raw" in
            *local* ) kind="local" ;;
            simlat ) kind="global" ;;
            *global* ) kind="global" ;;
            * ) kind="local" ;;
        esac
    fi

    # Set ranges according to chosen kind (no jitter).
    local client_min client_max sfu_lat
    if [ "$kind" = "local" ]; then
        client_min=20
        client_max=40
        sfu_lat=50
    else
        client_min=50
        client_max=400
        sfu_lat=40
    fi

    mkdir -p "${out_dir}"
    local out="${out_dir}/latencies_${kind}.txt"

    # Write SFU/host latency as first line
    echo "${sfu_lat}" > "$out"

    # Generate deterministic, evenly spaced client latencies across range
    if [ "$num_clients" -le 1 ] 2>/dev/null; then
        # single client -> midpoint
        val=$(( (client_min + client_max) / 2 ))
        echo "$val" >> "$out"
    else
        local span=$(( client_max - client_min ))
        for ii in $(seq 1 "$num_clients"); do
            # integer arithmetic: value = min + round((ii-1)*(span)/(num_clients-1))
            local step_num=$(( (ii-1) * span ))
            local step_den=$(( num_clients - 1 ))
            local add=$(( step_num / step_den ))
            local val=$(( client_min + add ))
            echo "$val" >> "$out"
        done
    fi

    echo "Generated latency file: $out (scenario=${kind}, clients=${num_clients}, sfu=${sfu_lat}ms)"
}

stop_bandwidth_monitor() {
    if [ -n "$BW_MONITOR_PID" ]; then
        # Create stopfile so background loop ends cleanly
        if [ -n "$BW_MONITOR_STOPFILE" ]; then
            touch "$BW_MONITOR_STOPFILE"
        fi
        # Wait a bit for background process to exit
        wait "$BW_MONITOR_PID" 2>/dev/null || true
        unset BW_MONITOR_PID
        echo "Bandwidth monitor stopped"
    fi
}

run_scenario() {
    # New signature: run_scenario <RESOLUTION> <ARCHITECTURE> <CLIENTS> <DOWNLOAD_BW> <UPLOAD_MODE> <LATENCY_MODE> <VIEW_MODE> <INPUT_FILE> <RUN_COUNT>
    local RESOLUTION="$1"
    local ARCHITECTURE="$2"
    # set CLIENTS as a global variable so other helper functions (cleanup, record_container_logs)
    # see the number of clients.
    CLIENTS="$3"
    local DOWNLOAD_BW="$4"
    local UPLOAD_MODE="$5"
    local LATENCY_MODE_PARAM="$6"
    local VIEW_MODE="$7"
    local INPUT_FILE="$8"
    local RUN_COUNT="${9}"

    # Build a result folder name that encodes key parameters to make runs
    # self-describing and unique: resolution and latency mode (upload/view are global for the run)
    local LATENCY_MODE_PARAM="${LATENCY_MODE_PARAM:-none}"
    # Include upload and view modes in scenario tag so output paths are unique
    local scen_tag="${RESOLUTION}_lat-${LATENCY_MODE_PARAM}_ul-${UPLOAD_MODE}_view-${VIEW_MODE}"
    local base_output_folder="${RUN_FOLDER}/${scen_tag}/${ARCHITECTURE}-${CLIENTS}"

    for run_index in $(seq 1 "$RUN_COUNT"); do

        local run_output_folder="${base_output_folder}/run_${run_index}"
        mkdir -p "${run_output_folder}"

        # Current time
        local current_time_ms
        current_time_ms=$(date +%s%3N)

        # Setup + warmup + experiment
        # setup time scales with number of clients and configured waits (in seconds)
        # Each client: WAIT_AFTER_INVITE seconds after call, plus one initial settings wait
        local setup_time_ms=$(( CLIENTS * WAIT_AFTER_INVITE * 1000 + WAIT_AFTER_SETTINGS * 1000 ))
        local warmup_time_ms=$(( WARMUP_TIME * 1000 ))
        local experiment_time_ms=$(( EXPERIMENT_TIME * 1000 ))
        local cooldown_time_ms=$(( COOLDOWN_TIME * 1000 ))
        local margin_ms=$(timer_margin_ms "$CLIENTS")

        local experiment_start_ms=$((current_time_ms + setup_time_ms + warmup_time_ms))
        local experiment_end_ms=$((experiment_start_ms + experiment_time_ms))

        echo "---------------------------------------------------------"
        echo "Running scenario: $scen_tag"
        echo "Run ${run_index}/${RUN_COUNT}"
        echo "Architecture: $ARCHITECTURE, Clients: $CLIENTS"
        echo "Resolution: $RESOLUTION, DL: ${DOWNLOAD_BW} Mbps, UL_mode: ${UPLOAD_MODE}"
        echo "Latency mode: $LATENCY_MODE_PARAM, View: $VIEW_MODE"
        echo "---------------------------------------------------------"

         write_metadata "$scen_tag" "$ARCHITECTURE" "$CLIENTS" "$RESOLUTION" \
             "$DOWNLOAD_BW" "$UPLOAD_MODE" "$LATENCY_MODE_PARAM" "$VIEW_MODE" \
             "$run_output_folder" "$experiment_start_ms" "$experiment_end_ms" "$run_index"

        # Create host first so it is ready when clients call in sequence.
        create_host "$run_output_folder" "$ARCHITECTURE" "$CLIENTS" "$RESOLUTION" "$DOWNLOAD_BW" "$warmup_time_ms" "$experiment_time_ms" "$cooldown_time_ms" "$LATENCY_MODE_PARAM"

        generate_client_configs "${UPLOAD_MODE}" "${VIEW_MODE}" "${CLIENTS}" || true
        create_clients "$CLIENTS" "$INPUT_FILE" "$run_output_folder" "$LATENCY_MODE_PARAM"

        # Start bandwidth monitor (polling interval 1s) - writes per-container CSVs
        start_bandwidth_monitor "$run_output_folder" 1

        countdown_timer "$run_output_folder" "$current_time_ms" "$setup_time_ms" "$warmup_time_ms" "$experiment_time_ms" "$cooldown_time_ms" "$margin_ms"

        # Stop bandwidth monitor and collect logs
        stop_bandwidth_monitor
        record_container_logs "$run_output_folder"
        cleanup

        # Update and print global progress (one line per completed run)
        COMPLETED_RUNS=$((COMPLETED_RUNS + 1))
        now_ms=$(date +%s%3N)
        elapsed_s=$(( (now_ms - GLOBAL_START_MS) / 1000 ))
        elapsed_hms=$(format_duration_hms "$elapsed_s")
        echo "Progress: ${COMPLETED_RUNS}/${TOTAL_RUNS} — Elapsed: ${elapsed_hms} — Estimated total: ${total_hms}"

    done
}

cleanup() {
    echo "Stopping and removing containers if they exist"
    # Ensure bandwidth monitor stopped if running
    if [ -n "${BW_MONITOR_PID-}" ]; then
        stop_bandwidth_monitor || true
    fi
    # Remove any client containers matching prefix (handles leftover clients beyond current CLIENTS)
    if command -v docker >/dev/null 2>&1; then
        for cname in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E "^${CLIENT_PREFIX}[0-9]+$" 2>/dev/null || true); do
            docker rm -f "$cname" 2>/dev/null || true
        done
        # Also remove host container if present
        docker rm -f "$HOST_NAME" 2>/dev/null || true
    fi
}


# ----------------- start of the script -----------------------

cleanup # make sure the containers don't exist
trap cleanup EXIT # remove containers if this script crashes

# Capture the original CLI invocation (best-effort, shell-escaped)
ORIGINAL_CMDLINE="$0"
for _arg in "$@"; do
    ORIGINAL_CMDLINE+=" $(printf '%q' "${_arg}")"
done

# Parse CLI args (if any) to override defaults/env
parse_args "$@"

    # Normalize ARCHS/RESOLUTIONS/CLIENTS_LIST (spaces -> commas) so validation
# can treat comma-separated lists consistently, then validate strictly.
ARCHS=$(echo "$ARCHS" | tr ' ' ',')
RESOLUTIONS=$(echo "$RESOLUTIONS" | tr ' ' ',')
CLIENTS_LIST=$(echo "$CLIENTS_LIST" | tr ' ' ',')
SEND_BW_MODE=$(echo "$SEND_BW_MODE" | tr ' ' ',')

# Run strict validation now (will exit on error). validate_params will also
# populate ARCHS_ARRAY, RESOLUTIONS_ARRAY and CLIENTS_ARRAY on success.
validate_params

# Print the selected docker image (one-line): Repository:Tag ID CreatedAt (first match)
echo "Docker image: $(docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedAt}}' 2>/dev/null | grep -E "^${DOCKER_IMAGE}:" | head -n1 || echo "${DOCKER_IMAGE}: not found")"

echo "Running with RUN_COUNT=${RUN_COUNT}, CLIENTS=${CLIENTS_LIST}, ARCHS=${ARCHS}, RESOLUTIONS=${RESOLUTIONS}, LATENCY_MODES=${LATENCY_MODES}, VIEW_MODE=${VIEW_MODE}"

total_ms=0
num_resolutions=${#RESOLUTIONS_ARRAY[@]}
num_arch=${#ARCHS_ARRAY[@]}
# compute number of latency and bandwidth modes correctly from comma lists
IFS=',' read -r -a LATENCY_ARRAY <<< "${LATENCY_MODES}"
num_latency_modes=${#LATENCY_ARRAY[@]}
num_views=${#VIEW_MODES_ARRAY[@]}
num_bw_modes=${#SEND_BW_ARRAY[@]}

for clients in ${CLIENTS_LIST//,/ } ; do
    per_run_ms=$(( clients * WAIT_AFTER_INVITE * 1000 + WAIT_AFTER_SETTINGS * 1000 + WARMUP_TIME * 1000 + EXPERIMENT_TIME * 1000 + COOLDOWN_TIME * 1000 + $(timer_margin_ms "$clients") ))
    total_ms=$(( total_ms + per_run_ms * RUN_COUNT * num_resolutions * num_arch * num_latency_modes * num_views * num_bw_modes ))
done

total_seconds=$(( total_ms / 1000 ))
total_hms=$(format_duration_hms "$total_seconds")
echo "Estimated total run time (at least): ${total_hms}"

# Ask for confirmation before preparing tests so the user can abort after
# seeing parameters and estimated runtime. If no TTY is available, proceed.
if [ -t 0 ]; then
    while true; do
        read -r -p "Proceed with the evaluation? (y/N): " yn
        case "$yn" in
            [Yy]* ) break ;;
            ""|[Nn]* ) echo "Aborted by user."; exit 0 ;;
            * ) echo "Please answer y or n." ;;
        esac
    done
else
    echo "No TTY detected; proceeding without interactive confirmation."
fi

prepare_tests # prepares test files and creates network

# Record start timestamp for the overall run and write the minimal params file
GLOBAL_START_MS=$(date +%s%3N)
write_run_parameters
ulimit -c unlimited # in case experiment crashes

# Compute total number of scenario runs for progress reporting
TOTAL_RUNS=$(( num_latency_modes * num_resolutions * num_views * ${#CLIENTS_ARRAY[@]} * num_arch * num_bw_modes * RUN_COUNT ))
COMPLETED_RUNS=0

# Iterate latency modes (if any), resolutions, client counts and architectures
if [ -n "${LATENCY_MODES}" ]; then
    IFS=',' read -r -a LATENCY_RUNS <<< "${LATENCY_MODES}"
else
    LATENCY_RUNS=("none")
fi

for LATENCY_MODE in "${LATENCY_RUNS[@]}"; do
    for RES in "${RESOLUTIONS_ARRAY[@]}"; do
        # Mapping: 3840x2160 -> 6.0 Mbps, 1920x1080 -> 3.0 Mbps, 1280x720 -> 1.0 Mbps
        case "$RES" in
            "3840x2160") DOWNLOAD_BW="6.0" ;;
            "1920x1080") DOWNLOAD_BW="3.0" ;;
            "1280x720") DOWNLOAD_BW="1.0" ;;
            *) DOWNLOAD_BW="1.0" ;;
        esac
        for VIEW in "${VIEW_MODES_ARRAY[@]}"; do
            for clients in ${CLIENTS_LIST//,/ } ; do
                for arch in "${ARCHS_ARRAY[@]}"; do
                    for BW_MODE in "${SEND_BW_ARRAY[@]}"; do

                        # Choose input file per-resolution: prefer matching extracted inputs
                        if [ "${RES}" = "3840x2160" ]; then
                            input_file="${INPUT_FILE_4K}"
                        elif [ "${RES}" = "1920x1080" ]; then
                            input_file="${INPUT_FILE_FULLHD}"
                        else
                            input_file="${INPUT_FILE_720}"
                        fi

                        run_scenario "$RES" "$arch" "$clients" "$DOWNLOAD_BW" "$BW_MODE" "$LATENCY_MODE" "$VIEW" "$input_file" "$RUN_COUNT"
                    done
                done
            done
        done
    done
done

# Cleanup will be triggered automatically by trap
END_MS=$(date +%s%3N)
if [ -n "${GLOBAL_START_MS-}" ]; then
    elapsed_ms=$((END_MS - GLOBAL_START_MS))
    elapsed_s=$((elapsed_ms / 1000))
    actual_hms=$(format_duration_hms "$elapsed_s")

    params_file="${RUN_FOLDER}/run_params.txt"
    if [ -f "${params_file}" ]; then
        {
            echo "end_ms: ${END_MS}"
            echo "actual_total: ${actual_hms}"
            echo "actual_total_ms: ${elapsed_ms}"
        } >> "${params_file}"
    fi
fi

echo "Experiment finished"
