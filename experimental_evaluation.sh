#!/bin/bash

# Network and container names
DOCKER_IMAGE="uvgcomm-docker"
NETWORK_NAME="uvgcomm-net"
HOST_NAME="uvgcomm-host"
CLIENT_PREFIX="uvgcomm-client"

# Inputs and configs
INPUT_FILE="./input/johnny30.yuv"
SOURCE_FILE="./input/johnny60.y4m"
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

    # Download source video if missing
    if [ ! -f "$SOURCE_FILE" ]; then
        echo "Downloading Johnny test video..."
        wget -O "$SOURCE_FILE" "https://media.xiph.org/video/derf/y4m/Johnny_1280x720_60.y4m"
    fi

    local frame_rate=30

    if [ ! -f "$INPUT_FILE" ]; then
        echo "Converting source file $SOURCE_FILE to $INPUT_FILE"
        ffmpeg -y -i "$SOURCE_FILE" \
               -vf "select='not(mod(n,2))',setpts=N/($frame_rate*TB)" \
               -vsync vfr \
               -c:v rawvideo \
               -pix_fmt yuv420p \
               "$INPUT_FILE"
    fi

    mkdir -p "$RUN_FOLDER"
    echo "Preparation complete. Input file ready: $INPUT_FILE"
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
    local end="${10}"
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
        echo "Download_BW: ${download_bw}Mbps"
        echo "Upload_BW: ${upload_bw}Mbps"
        echo "Latency: $latency ms"
        echo "View_Mode: $view_mode"
        echo "Start_Timestamp: $start"
        echo "End_timestamp: $end"
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
    local output_file="$1"
    local architecture="$2"
    local clients="$3"
    local setup_duration="$4"       # in milliseconds
    local warmup_duration="$5"      # in milliseconds
    local experiment_duration="$6"  # in milliseconds

    local wait_between_calls=6      # seconds between calling each client
    local wait_after_calls=10       # seconds after last client before hangup

    echo "# Auto-generated host script" > "$output_file"
    echo "setting sip/Topology $architecture" >> "$output_file"
    echo "setCall" >> "$output_file"

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
        echo "wait $wait_between_calls" >> "$output_file"
        echo "call $client_name $client_ip" >> "$output_file"
    done < "$USERS_FILE"

    # Wait remaining warmup period
    # Convert milliseconds to seconds (round down)
    local warmup_s=$((warmup_duration / 1000))
    echo "wait $warmup_s" >> "$output_file"

    # Experiment period
    local experiment_s=$((experiment_duration / 1000))
    echo "# Experiment running for $experiment_s seconds" >> "$output_file"
    echo "wait $experiment_s" >> "$output_file"

    # Hangup and quit
    echo "hangup" >> "$output_file"
    echo "wait $wait_after_calls" >> "$output_file"
    echo "quit" >> "$output_file"

    echo "Host script generated at: $output_file"
}

create_host() {
    local script_file="$1/script.txt"
    local architecture="$2"
    local clients="$3"
    local setup_time="$4"
    local warmup_time="$5"
    local experiment_time="$6"

    create_host_script "${script_file}" $architecture $clients $setup_time $warmup_time $experiment_time

    echo "Starting host"
    docker run -d --name $HOST_NAME --network $NETWORK_NAME --ip 172.28.0.2 \
        -v ${CONFIG_FOLDER}/uvgComm.ini:$CONTAINER_CONFIG_FILE \
        -v ${script_file}:${CONTAINER_HOST_SCRIPT_FILE} \
        ${DOCKER_IMAGE}:latest --script $CONTAINER_HOST_SCRIPT_FILE
}

countdown_timer() {
    local output_location=$1
    local duration_ms=$2
    echo "Experiment running for $duration_s seconds..."

    CPU_LOG="${output_location}/cpu_usage.csv"
    echo "timestamp_ms;cpu_percent" > "$CPU_LOG"

    local START_TIME_MS=$(($(date +%s%N)/1000000))

    while true; do
        local NOW_MS=$(($(date +%s%N)/1000000))
        local ELAPSED_MS=$((NOW_MS - START_TIME_MS))
        if [ $((ELAPSED_MS)) -ge $duration_ms ]; then
            break
        fi

        # CPU measurement using mpstat
        local CPU=$(mpstat 1 1 | awk '/Average/ {print 100-$12}' | tr ',' '.')

        echo "$NOW_MS;$CPU" >> "$CPU_LOG"
        echo "Timestamp: $NOW_MS ms, CPU usage: $CPU%"

        # Sleep until the next millisecond boundary (approx 1s intervals)
        local NEXT_MS=$((START_TIME_MS + ((ELAPSED_MS/1000)+1)*1000))
        local SLEEP_MS=$((NEXT_MS - $(($(date +%s%N)/1000000))))
        [ $SLEEP_MS -gt 0 ] && sleep $(awk "BEGIN {print $SLEEP_MS/1000}")
    done
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
    local CLIENTS="$3"
    local RESOLUTION="$4"
    local DOWNLOAD_BW="$5"
    local UPLOAD_BW="$6"
    local LATENCY="$7"
    local VIEW_MODE="$8"
    local RUN_COUNT="$9"

    local base_output_folder="${RUN_FOLDER}/${SCENARIO}/${ARCHITECTURE}-${CLIENTS}"

    for run_index in $(seq 1 $RUN_COUNT); do

        local run_output_folder="${base_output_folder}/run_${run_index}"

        # Current time
        local current_time_ms=$(date +%s%3N)

        # Setup + warmup + experiment
        local setup_time=120000        # 2 minutes
        local warmup_time=30000        # 30 seconds
        local experiment_time=120000   # 2 minutes
        local cooldown_time=10000      # 10 seconds

        local duration_ms=$((setup_time+warmup_time+experiment_time+cooldown_time))

        local experiment_start=$((current_time_ms + setup_time + warmup_time))
        local experiment_end=$((experiment_start + experiment_time))

        echo "---------------------------------------------------------"
        echo "Running scenario: $SCENARIO"
        echo "Run ${run_index}/${RUN_COUNT}"
        echo "Architecture: $ARCHITECTURE, Clients: $CLIENTS"
        echo "Resolution: $RESOLUTION, DL: ${DOWNLOAD_BW}Mbps, UL: ${UPLOAD_BW}Mbps"
        echo "Latency: $LATENCY, View: $VIEW_MODE"
        echo "---------------------------------------------------------"

        write_metadata "$SCENARIO" "$ARCHITECTURE" "$CLIENTS" "$RESOLUTION" \
                   "$DOWNLOAD_BW" "$UPLOAD_BW" "$LATENCY" "$VIEW_MODE" \
                   "$run_output_folder" "$experiment_start" "$experiment_end"

        create_clients "$CLIENTS" "$INPUT_FILE" $run_output_folder
        create_host $run_output_folder $ARCHITECTURE $CLIENTS $setup_time $warmup_time $experiment_time
        countdown_timer $run_output_folder $duration_ms
        record_container_logs $run_output_folder
        cleanup
    done
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

run_count=3

for clients in {2..6}; do
    run_scenario "720p" "P2P_Mesh" "$clients" "1280x720" 1.0 1.0 false "gallery" "$run_count"
    run_scenario "720p" "SFU" "$clients" "1280x720" 1.0 1.0 false "gallery" "$run_count"
    run_scenario "720p" "Hybrid" "$clients" "1280x720" 1.0 1.0 false "gallery" "$run_count"
done

# Cleanup will be triggered automatically by trap
echo "Experiment finished"
