# uvgcomm-benchmark

Minimal benchmarking harness for running reproducible uvgComm Docker-based experiments and parsing the produced metrics into summary CSVs and plots.

## Supported platform

Linux only.

## uvgComm Docker image requirement

The experiment runner expects a locally built uvgComm Docker image named:

`uvgcomm-docker:latest`

If you built the image with a different name/tag, tag it to `uvgcomm-docker:latest`:

```bash
docker tag <your-image>:<tag> uvgcomm-docker:latest
```

## Dependencies

### To run `experimental_evaluation.sh`

Required tools used directly by the script:

- Docker Engine + CLI (`docker`), separation for multiple clients
- `ffmpeg` (to convert downloaded source and generate 30fps YUV inputs)
- `wget` (to download test videos)
- `7z` (from `p7zip-full`, to extract `.7z` archives for source videos)
- `mpstat` (from `sysstat`, to log CPU usage)
- `awk` (`gawk`, used for formatting CPU logs)

On Debian/Ubuntu, the following is usually sufficient:

```bash
sudo apt update
sudo apt install -y docker.io ffmpeg wget p7zip-full sysstat gawk ca-certificates
```

Notes:

- The script also relies on standard Unix utilities like `bash`, `sed`, `grep`, `find`, `date`, and `tr` which are typically present on Linux by default.
- You’ll need permission to run Docker (e.g. in the `docker` group) and enough disk space under `./results/`.
- Latency simulation uses `tc` (Linux traffic control) and `ip` inside the containers via `docker exec`. These come from the `iproute2` package and are commonly available in Linux images.

### To run `parse_results.py`

System packages:

- `python3`
- `python3-pip` (or another way to install Python packages)

Python packages (pip):

- `pandas`
- `numpy`
- `matplotlib`

Example install on Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-pip
python3 -m pip install --user pandas numpy matplotlib
```

## Command-line options (`experimental_evaluation.sh`)

- `-r RUNS`: repeat each scenario this many times.
- `-c CLIENTS`: comma-separated list of participant counts evaluated.
- `-a ARCHS`: comma-separated list of architectures evaluated: `P2P_Mesh,SFU,Hybrid`.
- `-s RESOLUTIONS`: comma-separated resolutions evaluated: `1280x720,1920x1080,3840x2160`.
- `-w VIEW`: `gallery`, `speaker`, or both separated by comma.
- `-v VISIBLE`: visible participants in gallery view, default is 9.
- `-e SECONDS`: evaluation duration, default 60 (seconds).
- `-l MODES`: simulated latency mode(s): `none`, `local`, `global`, `dataset-PlanetLab`, `dataset-Seattle`, or a comma-separated list.
- `-b MODE`: send bandwidth mode(s): `all1000`, `all10`, `all1`, `inc1`, `inc5`, `inc10`, or a comma-separated list.
- `-h`: show help.

## Example for running the evaluation

```bash
./experimental_evaluation.sh \
	-r 2 \
	-c "2,3,4,5,6,7,8,9,10,11,12,13,14,15,16" \
	-a "P2P_Mesh,SFU,Hybrid" \
	-s "1280x720,1920x1080,3840x2160" \
	-w "gallery,speaker" \
	-v 9 \
	-e 60 \
	-l "none,global" \
	-b "all1000,all1"
```

This creates a timestamped folder under `./results/<timestamp>` containing per-run logs and CSV traces. You will get a handly time estimate before starting the full test and allowing you to adjust based on the time you have available.

## Parsing results

```bash
./parse_results.py ./results/20260101_123456
```

Outputs are written under `./results/<timestamp>/analysis/` (CSV summaries + SVG plots).
