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

    local frame_rate = 30

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
    local output_file="$1"  # path to save generated host script
    local wait_between_calls=5
    local wait_after_calls=10

    if [[ ! -f "$USERS_FILE" ]]; then
        echo "Error: USERS_FILE not found: $USERS_FILE"
        return 1
    fi

    echo "# Auto-generated host script" > "$output_file"

    # Read host IP (not used in calls, but could be logged)
    local host_line
    host_line=$(grep "^host=" "$USERS_FILE")
    if [[ -n "$host_line" ]]; then
        host_user="${host_line#*=}"
        host_user="${host_user%%:*}"  # username only
    fi

    # Read clients
    client_count=0
    while IFS= read -r line; do
        [[ "$line" =~ ^#.*$ ]] && continue        # skip comments
        [[ "$line" =~ ^host=.*$ ]] && continue    # skip host line
        client_count=$((client_count + 1))
        [[ $client_count -gt $CLIENTS ]] && break

        client_user="${line#*=}"
        client_name="${client_user%%:*}"
        client_ip="${client_user##*:}"

        echo "wait $wait_between_calls" >> "$output_file"
        echo "call $client_name $client_ip" >> "$output_file"
    done < "$USERS_FILE"

    # Hangup and quit
    echo "wait $wait_after_calls" >> "$output_file"
    echo "hangup" >> "$output_file"
    echo "wait 5" >> "$output_file"
    echo "quit" >> "$output_file"

    echo "Host script generated at: $output_file"
}

create_host() {
    HOST_SCRIPT_FILE="${RUN_FOLDER}/script.txt"
    create_host_script "$HOST_SCRIPT_FILE"

    echo "Starting host"
    docker run -d --name $HOST_NAME --network $NETWORK_NAME --ip 172.28.0.2 \
        -v ${CONFIG_FOLDER}/uvgComm.ini:$CONTAINER_CONFIG_FILE \
        -v ${HOST_SCRIPT_FILE}:${CONTAINER_HOST_SCRIPT_FILE} \
        ${DOCKER_IMAGE}:latest --script $CONTAINER_HOST_SCRIPT_FILE
}

countdown_timer() {
    local duration_s="$1"
    echo "Experiment running for $duration_s seconds..."
    CPU_LOG="${RUN_FOLDER}/cpu_usage.csv"
    echo "time_sec;cpu_percent" > $CPU_LOG
    START_TIME=$(date +%s)

    while true; do
        NOW=$(date +%s)
        ELAPSED=$((NOW - START_TIME))
        if [ $ELAPSED -ge $duration_s ]; then
            break
        fi

        # CPU measurement using mpstat with non-blocking mode
        CPU=$(mpstat 1 1 | awk '/Average/ {print 100-$12}' | tr ',' '.')
        echo "$ELAPSED;$CPU" >> $CPU_LOG
        echo "Time elapsed: $ELAPSED s, CPU usage: $CPU%"

        # Sleep until the next second boundary
        NEXT=$((START_TIME + ELAPSED + 1))
        SLEEP_TIME=$((NEXT - $(date +%s)))
        [ $SLEEP_TIME -gt 0 ] && sleep $SLEEP_TIME
    done
}

record_container_logs() {
    echo "Recording logs"
    for i in $(seq 1 $CLIENTS); do
        docker logs ${CLIENT_PREFIX}${i} &> ${RUN_FOLDER}/${CLIENT_PREFIX}${i}/docker.log
    done
    docker logs $HOST_NAME &> ${RUN_FOLDER}/${HOST_NAME}.log
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

    echo "---------------------------------------------------------"
    echo "Running scenario: $SCENARIO"
    echo "Architecture: $ARCHITECTURE, Clients: $CLIENTS"
    echo "Resolution: $RESOLUTION, DL: ${DOWNLOAD_BW}Mbps, UL: ${UPLOAD_BW}Mbps"
    echo "Latency: $LATENCY, View: $VIEW_MODE"
    echo "---------------------------------------------------------"

    create_clients "$CLIENTS" "$INPUT_FILE" "$RUN_FOLDER"
    create_host
    countdown_timer 30
    record_container_logs
    cleanup
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

prepare_tests

# P2P Mesh, 720p, no latencies, gallery view
run_scenario "720p" "P2P" 2 "1280x720" 1.0 1.0 false "gallery"

# Cleanup will be triggered automatically by trap
echo "Experiment finished"
