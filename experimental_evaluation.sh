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

USERS_FILE="./usernames.conf"

# Per-benchmark wait values (seconds). Edit these two values to tune timing.
# - WAIT_AFTER_INVITE: seconds to wait after each client is called
# - WAIT_AFTER_SETTINGS: seconds to wait after initial settings before calling clients
WAIT_AFTER_INVITE=10
WAIT_AFTER_SETTINGS=5

# ----------------------- functions ------------------------

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

    if [ ! -f "$SOURCE_4K_ARCHIVE" ]; then
        wget -O "$SOURCE_4K_ARCHIVE" "$SOURCE_4K_URL"
    fi

    if [ ! -f "$EXTRACTED_4K_FILE" ] && [ -f "$SOURCE_4K_ARCHIVE" ]; then
        if command -v 7z >/dev/null 2>&1; then
            7z x -y -o"input" "$SOURCE_4K_ARCHIVE"
        else
            p7zip -d "$SOURCE_4K_ARCHIVE" || true
        fi
    fi

    if [ ! -f "$EXTRACTED_4K_FILE" ]; then
        extracted=$(find input -maxdepth 1 -type f -iname '*3840*2160*.yuv' -print -quit)
        if [ -n "$extracted" ]; then
            mv "$extracted" "$EXTRACTED_4K_FILE" 2>/dev/null || true
        fi
    fi

    if [ ! -f "$INPUT_FILE_4K" ] && [ -f "$EXTRACTED_4K_FILE" ]; then
        ffmpeg -y -f rawvideo -pixel_format yuv420p -video_size 3840x2160 -framerate 120 -i "$EXTRACTED_4K_FILE" \
            -vf "select='not(mod(n,4))',setpts=N/($frame_rate*TB)" -vsync vfr -c:v rawvideo -pix_fmt yuv420p "$INPUT_FILE_4K"
    fi

    mkdir -p "$RUN_FOLDER"
    echo "Preparation complete. Input files ready: $INPUT_FILE_720 and $INPUT_FILE_4K"
}

write_metadata() {
    local scenario="$1"
    local architecture="$2"
    local clients="$3"
    local resolution="$4"
    local download_bw="$5"
    local upload_bw="$6"
    local latency="$7"
    local view_mode="$8"
    local output_folder="$9"
    local start="${10}"
    local end="${11}"
    local run="${12}"

    local cpu_log="${output_folder}/cpu_usage.csv"

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
        echo "Upload_BW: ${upload_bw} Mbps"
        echo "Simulated_latencies: $latency"
        echo "View_Mode: $view_mode"
        echo "Start_Timestamp: $start"
        echo "End_timestamp: $end"
        echo "Run: $run"
    } > "$metadata_file"

    echo "Metadata written successfully."
}


create_clients() {
    local num_clients="$1"
    local input_file="$2"
    local output_folder="$3"

    for i in $(seq 1 "$num_clients"); do
        CONTAINER_NAME="${CLIENT_PREFIX}${i}"
        local config_file="${CONFIG_FOLDER}/uvgComm${i}.ini"
        local client_output="${output_folder}/${CLIENT_PREFIX}$i"

        mkdir -p "$client_output"
        echo "Starting client $i"
        docker run -d --name $CONTAINER_NAME --network $NETWORK_NAME --ip 172.28.0.$((2+i)) \
            -v "${input_file}:${CONTAINER_INPUT_FILE}:ro" \
            -v "${config_file}:${CONTAINER_CONFIG_FILE}" \
            -v "${client_output}:${CONTAINER_STATS_FOLDER}" \
            ${DOCKER_IMAGE}:latest \
            --stats=${CONTAINER_STATS_FOLDER} \
            --siplog=${CONTAINER_STATS_FOLDER}/siplog.txt
    done
}


create_host_script() {
    local output_file=$1
    local architecture=$2
    local clients=$3
    local resolution=$4
    local download_bw=$5
    local upload_bw=$6
    local setup_duration_ms=$7
    local warmup_duration_ms=$8
    local experiment_duration_ms=$9
    local cooldown_duration_ms=${10}

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

    echo "setting sip/upBandwidth $upload_bw" >> "$output_file"
    echo "setting sip/downBandwidth $download_bw" >> "$output_file"
    echo "setCall" >> "$output_file"

    echo "setting video/FileResolutionWidth $width" >> "$output_file"
    echo "setting video/FileResolutionHeight $height" >> "$output_file"
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

    # Wait remaining warmup period
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
    local script_file="$1/script.txt"
    local architecture="$2"
    local clients="$3"
    local resolution="$4"
    local download_bw="$5"
    local upload_bw="$6"
    local setup_time="$7"
    local warmup_time="$8"
    local experiment_time="$9"
    local cooldown_time=${10}

    create_host_script "${script_file}" $architecture $clients $resolution $download_bw $upload_bw $setup_time $warmup_time $experiment_time $cooldown_time

    echo "Starting host"
    docker run -d --name $HOST_NAME --network $NETWORK_NAME --ip 172.28.0.2 \
        -v ${CONFIG_FOLDER}/uvgComm.ini:$CONTAINER_CONFIG_FILE \
        -v ${script_file}:${CONTAINER_HOST_SCRIPT_FILE} \
        ${DOCKER_IMAGE}:latest --script $CONTAINER_HOST_SCRIPT_FILE
}

countdown_timer() {
    local output_location=$1
    local start_time_ms=$2
    local setup_time_ms=$3
    local warmup_time_ms=$4
    local experiment_time_ms=$5
    local cooldown_time_ms=$6

    # Total duration in ms
    local total_duration_ms=$((setup_time_ms + warmup_time_ms + experiment_time_ms + cooldown_time_ms))

    CPU_LOG="${output_location}/cpu_usage.csv"
    echo "timestamp_ms;cpu_percent" > "$CPU_LOG"

    echo "Starting countdown timer..."

    local last_phase=""

    while true; do
        local now_ms=$(($(date +%s%N)/1000000))
        local elapsed_ms=$((now_ms - start_time_ms))

        # Stop when total duration exceeded
        if [ $elapsed_ms -ge $total_duration_ms ]; then
            break
        fi

        # Determine phase
        local phase=""
        if [ $elapsed_ms -lt $setup_time_ms ]; then
            phase="Setup ${setup_time_ms} ms"
        elif [ $elapsed_ms -lt $((setup_time_ms + warmup_time_ms)) ]; then
            phase="Warmup ${warmup_time_ms} ms"
        elif [ $elapsed_ms -lt $((setup_time_ms + warmup_time_ms + experiment_time_ms)) ]; then
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
        local CPU=$(mpstat 1 1 | awk '/Average/ {print 100-$12}' | tr ',' '.')

        # Log to file
        echo "$now_ms;$CPU" >> "$CPU_LOG"

        # Simple inline print (no formatting headaches)
        echo "${now_ms}: $CPU%"


        # Sleep until the next millisecond boundary (approx 1s intervals)
        local next_ms=$((start_time_ms + ((elapsed_ms/1000)+1)*1000))
        local sleep_ms=$((next_ms - $(($(date +%s%N)/1000000))))
        [ $sleep_ms -gt 0 ] && sleep $(awk "BEGIN {print $sleep_ms/1000}")

    done
    echo "Countdown finished"
}

record_container_logs() {
    local output_location=$1
    echo "Recording logs"
    for i in $(seq 1 $CLIENTS); do
        docker logs ${CLIENT_PREFIX}${i} &> ${output_location}/${CLIENT_PREFIX}${i}/docker.log
    done
    docker logs $HOST_NAME &> ${output_location}/${HOST_NAME}.log
}

run_scenario() {
    local SCENARIO="$1"
    local ARCHITECTURE="$2"
    # set CLIENTS as a global variable so other helper functions (cleanup, record_container_logs)
    # see the number of clients.
    CLIENTS="$3"
    local RESOLUTION="$4"
    local DOWNLOAD_BW="$5"
    local UPLOAD_BW="$6"
    local LATENCY="$7"
    local VIEW_MODE="$8"
    local INPUT_FILE="$9"
    local RUN_COUNT="${10}"

    local base_output_folder="${RUN_FOLDER}/${SCENARIO}/${ARCHITECTURE}-${CLIENTS}"

    for run_index in $(seq 1 $RUN_COUNT); do

        local run_output_folder="${base_output_folder}/run_${run_index}"
        mkdir -p  ${run_output_folder}

        # Current time
        local current_time_ms=$(date +%s%3N)

        # Setup + warmup + experiment
        # setup time scales with number of clients and configured waits (in seconds)
        # Each client: WAIT_AFTER_INVITE seconds after call, plus one initial settings wait
        local setup_time_ms=$(( CLIENTS * WAIT_AFTER_INVITE * 1000 + WAIT_AFTER_SETTINGS * 1000 ))
        local warmup_time_ms=10000                      # 10 seconds
        local experiment_time_ms=60000                  # 1 minute
        local cooldown_time_ms=5000                     # 5 seconds


        local experiment_start_ms=$((current_time_ms + setup_time_ms + warmup_time_ms))
        local experiment_end_ms=$((experiment_start_ms + experiment_time_ms))

        echo "---------------------------------------------------------"
        echo "Running scenario: $SCENARIO"
        echo "Run ${run_index}/${RUN_COUNT}"
        echo "Architecture: $ARCHITECTURE, Clients: $CLIENTS"
        echo "Resolution: $RESOLUTION, DL: ${DOWNLOAD_BW} Mbps, UL: ${UPLOAD_BW} Mbps"
        echo "Latency: $LATENCY, View: $VIEW_MODE"
        echo "---------------------------------------------------------"

        write_metadata "$SCENARIO" "$ARCHITECTURE" "$CLIENTS" "$RESOLUTION" \
                   "$DOWNLOAD_BW" "$UPLOAD_BW" "$LATENCY" "$VIEW_MODE" \
                   "$run_output_folder" "$experiment_start_ms" "$experiment_end_ms" "$run_index"

    create_clients "$CLIENTS" "$INPUT_FILE" $run_output_folder
    create_host $run_output_folder $ARCHITECTURE $CLIENTS "$RESOLUTION" "$DOWNLOAD_BW" "$UPLOAD_BW" $setup_time_ms $warmup_time_ms $experiment_time_ms $cooldown_time_ms
        countdown_timer $run_output_folder $current_time_ms $setup_time_ms $warmup_time_ms $experiment_time_ms $cooldown_time_ms
        record_container_logs $run_output_folder
        cleanup
    done
}

run_architectures() {
    local SCENARIO="$1"
    local CLIENTS="$2"
    local RESOLUTION="$3"
    local DOWNLOAD_BW="$4"
    local UPLOAD_BW="$5"
    local LATENCY="$6"
    local VIEW_MODE="$7"
    local INPUT_FILE="$8"
    local RUN_COUNT="$9"

    run_scenario "$SCENARIO" "P2P_Mesh" "$CLIENTS" "$RESOLUTION" "$DOWNLOAD_BW" "$UPLOAD_BW" "$LATENCY" "$VIEW_MODE" "$INPUT_FILE" "$RUN_COUNT"
    run_scenario "$SCENARIO" "SFU"      "$CLIENTS" "$RESOLUTION" "$DOWNLOAD_BW" "$UPLOAD_BW" "$LATENCY" "$VIEW_MODE" "$INPUT_FILE" "$RUN_COUNT"
    run_scenario "$SCENARIO" "Hybrid"   "$CLIENTS" "$RESOLUTION" "$DOWNLOAD_BW" "$UPLOAD_BW" "$LATENCY" "$VIEW_MODE" "$INPUT_FILE" "$RUN_COUNT"
}


cleanup() {
    echo "Stopping and removing containers if they exist"
    for i in $(seq 1 $CLIENTS); do
        docker rm -f "${CLIENT_PREFIX}${i}" 2>/dev/null || true
    done
    docker rm -f $HOST_NAME 2>/dev/null || true
}


# ----------------- start of the script -----------------------

cleanup # make sure the containers don't exist
trap cleanup EXIT # remove containers if this script crashes

prepare_tests # prepares test files and creates network

# Print the selected docker image (one-line): Repository:Tag ID CreatedAt (first match)
echo "Docker image: $(docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedAt}}' 2>/dev/null | grep -E "^${DOCKER_IMAGE}:" | head -n1 || echo "${DOCKER_IMAGE}: not found")"

run_count=1

for clients in 2 3 4 5 6; do
    run_architectures "720p" "$clients" "1280x720" 1.0 1.0 false "gallery" "$INPUT_FILE_720" "$run_count"

    # TODO: 4K test needs implementation of input file in application 
    #run_architectures "4K" "$clients" "3840x2160" 6.0 6.0 false "gallery" "$INPUT_FILE_4K" "$run_count"


done

# Cleanup will be triggered automatically by trap
echo "Experiment finished"
