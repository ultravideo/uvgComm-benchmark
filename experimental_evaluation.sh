#!/bin/bash

# Network and container names
DOCKER_IMAGE="uvgcomm-docker"
NETWORK_NAME="uvgcomm-net"
HOST_NAME="uvgcomm-host"
CLIENT_PREFIX="uvgcomm-client"
CLIENTS=2
DURATION_S=30
FRAME_RATE=30
RESOLUTION="1280x720"

# Inputs and configs
INPUT_FILE="./input/johnny30.yuv"
SOURCE_FILE="./input/johnny60.y4m"
CONFIG_FOLDER="./configs"

# Container paths
CONTAINER_HOST_SCRIPT_FILE="/uvgcomm/build/script.txt"
CONTAINER_CONFIG_FILE="/uvgcomm/build/uvgComm.ini"
CONTAINER_STATS_FOLDER="/uvgcomm/build/stats_csv"
CONTAINER_INPUT_FILE="/uvgcomm/input/johnny30.yuv"

# Timestamped root folder for logs and stats
RUN_ID=$(date +"%Y%m%d_%H%M%S")
OUTPUT_FOLDER="./results/$RUN_ID"

USERS_FILE="./usernames.conf"

# ----------------------- functions ------------------------

prepare_tests() {
   # do all actions in preparation for tests
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

    if [ ! -f "$INPUT_FILE" ]; then
        echo "Converting source file $SOURCE_FILE to $INPUT_FILE"
        ffmpeg -y -i "$SOURCE_FILE" \
               -vf "select='not(mod(n,2))',setpts=N/($FRAME_RATE*TB)" \
               -vsync vfr \
               -c:v rawvideo \
               -pix_fmt yuv420p \
               "$INPUT_FILE"
    fi

    mkdir -p "$OUTPUT_FOLDER"
    echo "Preparation complete. Input file ready: $INPUT_FILE"
}


create_clients() {
    for i in $(seq 1 $CLIENTS); do
        CONTAINER_NAME="${CLIENT_PREFIX}${i}"
        CONFIG_FILE="${CONFIG_FOLDER}/uvgComm${i}.ini"
        CLIENT_OUTPUT="${OUTPUT_FOLDER}/${CLIENT_PREFIX}$i"
        mkdir -p $CLIENT_OUTPUT
        echo "Starting client $i"
        docker run -d --name $CONTAINER_NAME --network $NETWORK_NAME --ip 172.28.0.$((2+i)) \
            -v ${INPUT_FILE}:${CONTAINER_INPUT_FILE}:ro \
            -v ${CONFIG_FILE}:${CONTAINER_CONFIG_FILE} \
            -v ${CLIENT_OUTPUT}:${CONTAINER_STATS_FOLDER} \
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
    HOST_SCRIPT_FILE="${OUTPUT_FOLDER}/script.txt"
    create_host_script "$HOST_SCRIPT_FILE"

    echo "Starting host"
    docker run -d --name $HOST_NAME --network $NETWORK_NAME --ip 172.28.0.2 \
        -v ${CONFIG_FOLDER}/uvgComm.ini:$CONTAINER_CONFIG_FILE \
        -v ${HOST_SCRIPT_FILE}:${CONTAINER_HOST_SCRIPT_FILE} \
        ${DOCKER_IMAGE}:latest --script $CONTAINER_HOST_SCRIPT_FILE
}

countdown_timer() {
    echo "Experiment running for $DURATION_S seconds..."
    CPU_LOG="${OUTPUT_FOLDER}/cpu_usage.csv"
    echo "time_sec;cpu_percent" > $CPU_LOG
    START_TIME=$(date +%s)

    while true; do
        NOW=$(date +%s)
        ELAPSED=$((NOW - START_TIME))
        if [ $ELAPSED -ge $DURATION_S ]; then
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
        docker logs ${CLIENT_PREFIX}${i} &> ${OUTPUT_FOLDER}/${CLIENT_PREFIX}${i}/docker.log
    done
    docker logs $HOST_NAME &> ${OUTPUT_FOLDER}/${HOST_NAME}.log
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

# Create network if it does not exist
docker network inspect $NETWORK_NAME >/dev/null 2>&1 || \
    docker network create --subnet=172.28.0.0/16 $NETWORK_NAME

prepare_tests

# P2P Mesh, 720p, no latencies, gallery view
create_clients
create_host
countdown_timer
record_container_logs
cleanup

# Cleanup will be triggered automatically by trap
echo "Experiment finished"
