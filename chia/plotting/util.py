from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:
    from chia.plotting.prover import ProverProtocol

from chia_rs import G1Element, PrivateKey
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32
from typing_extensions import final

from chia.util.config import load_config, lock_and_load_config, save_config
from chia.util.streamable import Streamable, streamable

log = logging.getLogger(__name__)

DEFAULT_PARALLEL_DECOMPRESSOR_COUNT = 0
DEFAULT_DECOMPRESSOR_THREAD_COUNT = 0
DEFAULT_DECOMPRESSOR_TIMEOUT = 20
DEFAULT_DISABLE_CPU_AFFINITY = False
DEFAULT_MAX_COMPRESSION_LEVEL_ALLOWED = 7
DEFAULT_USE_GPU_HARVESTING = False
DEFAULT_GPU_INDEX = 0
DEFAULT_ENFORCE_GPU_INDEX = False
DEFAULT_RECURSIVE_PLOT_SCAN = False


@streamable
@dataclass(frozen=True)
class PlotsRefreshParameter(Streamable):
    interval_seconds: uint32 = uint32(120)
    retry_invalid_seconds: uint32 = uint32(1200)
    batch_size: uint32 = uint32(300)
    batch_sleep_milliseconds: uint32 = uint32(1)


@dataclass
class PlotInfo:
    prover: ProverProtocol
    pool_public_key: Optional[G1Element]
    pool_contract_puzzle_hash: Optional[bytes32]
    plot_public_key: G1Element
    file_size: int
    time_modified: float


class PlotRefreshEvents(Enum):
    """
    This are the events the `PlotManager` will trigger with the callback during a full refresh cycle:

      - started: This event indicates the start of a refresh cycle and contains the total number of files to
                 process in `PlotRefreshResult.remaining`.

      - batch_processed: This event gets triggered if one batch has been processed. The values of
                         `PlotRefreshResult.{loaded|removed|processed}` are the results of this specific batch.

      - done: This event gets triggered after all batches has been processed. The values of
              `PlotRefreshResult.{loaded|removed|processed}` are the totals of all batches.

      Note: The values of `PlotRefreshResult.{remaining|duration}` have the same meaning for all events.
    """

    started = 0
    batch_processed = 1
    done = 2


@dataclass
class PlotRefreshResult:
    loaded: list[PlotInfo] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    processed: int = 0
    remaining: int = 0
    duration: float = 0


@final
@dataclass
class Params:
    size: int
    num: int
    buffer: int
    num_threads: int
    buckets: int
    tmp_dir: Path
    tmp2_dir: Optional[Path]
    final_dir: Path
    plotid: Optional[str]
    memo: Optional[str]
    nobitfield: bool
    stripe_size: int = 65536


class HarvestingMode(IntEnum):
    CPU = 1
    GPU = 2


def get_plot_directories(root_path: Path, config: Optional[dict] = None) -> list[str]:
    if config is None:
        config = load_config(root_path, "config.yaml")
    return config["harvester"]["plot_directories"] or []


def get_plot_filenames(root_path: Path) -> dict[Path, list[Path]]:
    # Returns a map from directory to a list of all plots in the directory
    all_files: dict[Path, list[Path]] = {}
    config = load_config(root_path, "config.yaml")
    recursive_scan: bool = config["harvester"].get("recursive_plot_scan", DEFAULT_RECURSIVE_PLOT_SCAN)
    recursive_follow_links: bool = config["harvester"].get("recursive_follow_links", False)
    for directory_name in get_plot_directories(root_path, config):
        try:
            directory = Path(directory_name).resolve()
        except (OSError, RuntimeError):
            log.exception(f"Failed to resolve {directory_name}")
            continue
        all_files[directory] = get_filenames(directory, recursive_scan, recursive_follow_links)
    return all_files


def add_plot_directory(root_path: Path, str_path: str) -> dict:
    path: Path = Path(str_path).resolve()
    if not path.exists():
        raise ValueError(f"Path doesn't exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Path is not a directory: {path}")
    log.debug(f"add_plot_directory {str_path}")
    with lock_and_load_config(root_path, "config.yaml") as config:
        if str(Path(str_path).resolve()) in get_plot_directories(root_path, config):
            raise ValueError(f"Path already added: {path}")
        if not config["harvester"]["plot_directories"]:
            config["harvester"]["plot_directories"] = []
        config["harvester"]["plot_directories"].append(str(Path(str_path).resolve()))
        save_config(root_path, "config.yaml", config)
    return config


def remove_plot_directory(root_path: Path, str_path: str) -> None:
    log.debug(f"remove_plot_directory {str_path}")
    with lock_and_load_config(root_path, "config.yaml") as config:
        str_paths: list[str] = get_plot_directories(root_path, config)
        # If path str matches exactly, remove
        if str_path in str_paths:
            str_paths.remove(str_path)

        # If path matches full path, remove
        new_paths = [Path(sp).resolve() for sp in str_paths]
        if Path(str_path).resolve() in new_paths:
            new_paths.remove(Path(str_path).resolve())

        config["harvester"]["plot_directories"] = [str(np) for np in new_paths]
        save_config(root_path, "config.yaml", config)


def remove_plot(path: Path):
    log.debug(f"remove_plot {path!s}")
    # Remove absolute and relative paths
    if path.exists():
        path.unlink()


def get_harvester_config(root_path: Path) -> dict[str, Any]:
    config = load_config(root_path, "config.yaml")

    plots_refresh_parameter = (
        config["harvester"].get("plots_refresh_parameter")
        if config["harvester"].get("plots_refresh_parameter") is not None
        else PlotsRefreshParameter().to_json_dict()
    )

    return {
        "use_gpu_harvesting": config["harvester"].get("use_gpu_harvesting", DEFAULT_USE_GPU_HARVESTING),
        "gpu_index": config["harvester"].get("gpu_index", DEFAULT_GPU_INDEX),
        "enforce_gpu_index": config["harvester"].get("enforce_gpu_index", DEFAULT_ENFORCE_GPU_INDEX),
        "disable_cpu_affinity": config["harvester"].get("disable_cpu_affinity", DEFAULT_DISABLE_CPU_AFFINITY),
        "parallel_decompressor_count": config["harvester"].get(
            "parallel_decompressor_count", DEFAULT_PARALLEL_DECOMPRESSOR_COUNT
        ),
        "decompressor_thread_count": config["harvester"].get(
            "decompressor_thread_count", DEFAULT_DECOMPRESSOR_THREAD_COUNT
        ),
        "recursive_plot_scan": config["harvester"].get("recursive_plot_scan", DEFAULT_RECURSIVE_PLOT_SCAN),
        "plots_refresh_parameter": plots_refresh_parameter,
    }


def update_harvester_config(
    root_path: Path,
    *,
    use_gpu_harvesting: Optional[bool] = None,
    gpu_index: Optional[int] = None,
    enforce_gpu_index: Optional[bool] = None,
    disable_cpu_affinity: Optional[bool] = None,
    parallel_decompressor_count: Optional[int] = None,
    decompressor_thread_count: Optional[int] = None,
    recursive_plot_scan: Optional[bool] = None,
    refresh_parameter: Optional[PlotsRefreshParameter] = None,
):
    with lock_and_load_config(root_path, "config.yaml") as config:
        if use_gpu_harvesting is not None:
            config["harvester"]["use_gpu_harvesting"] = use_gpu_harvesting
        if gpu_index is not None:
            config["harvester"]["gpu_index"] = gpu_index
        if enforce_gpu_index is not None:
            config["harvester"]["enforce_gpu_index"] = enforce_gpu_index
        if disable_cpu_affinity is not None:
            config["harvester"]["disable_cpu_affinity"] = disable_cpu_affinity
        if parallel_decompressor_count is not None:
            config["harvester"]["parallel_decompressor_count"] = parallel_decompressor_count
        if decompressor_thread_count is not None:
            config["harvester"]["decompressor_thread_count"] = decompressor_thread_count
        if recursive_plot_scan is not None:
            config["harvester"]["recursive_plot_scan"] = recursive_plot_scan
        if refresh_parameter is not None:
            config["harvester"]["plots_refresh_parameter"] = refresh_parameter.to_json_dict()

        save_config(root_path, "config.yaml", config)


def get_filenames(directory: Path, recursive: bool, follow_links: bool) -> list[Path]:
    try:
        if not directory.exists():
            log.warning(f"Directory: {directory} does not exist.")
            return []
    except OSError as e:
        log.warning(f"Error checking if directory {directory} exists: {e}")
        return []
    all_files: list[Path] = []
    try:
        if follow_links and recursive:
            import glob

            v1_file_strs = glob.glob(str(directory / "**" / "*.plot"), recursive=True)
            v2_file_strs = glob.glob(str(directory / "**" / "*.plot2"), recursive=True)

            for file in v1_file_strs + v2_file_strs:
                filepath = Path(file).resolve()
                if filepath.is_file() and not filepath.name.startswith("._"):
                    all_files.append(filepath)
        else:
            glob_function = directory.rglob if recursive else directory.glob
            v1_files: list[Path] = [
                child for child in glob_function("*.plot") if child.is_file() and not child.name.startswith("._")
            ]
            v2_files: list[Path] = [
                child for child in glob_function("*.plot2") if child.is_file() and not child.name.startswith("._")
            ]
            all_files = v1_files + v2_files
        log.debug(f"get_filenames: {len(all_files)} files found in {directory}, recursive: {recursive}")
    except Exception as e:
        log.warning(f"Error reading directory {directory} {e}")
    return all_files


def parse_plot_info(memo: bytes) -> tuple[Union[G1Element, bytes32], G1Element, PrivateKey]:
    # Parses the plot info bytes into keys
    if len(memo) == (48 + 48 + 32):
        # This is a public key memo
        return (
            G1Element.from_bytes(memo[:48]),
            G1Element.from_bytes(memo[48:96]),
            PrivateKey.from_bytes(memo[96:]),
        )
    elif len(memo) == (32 + 48 + 32):
        # This is a pool_contract_puzzle_hash memo
        return (
            bytes32(memo[:32]),
            G1Element.from_bytes(memo[32:80]),
            PrivateKey.from_bytes(memo[80:]),
        )
    else:
        raise ValueError(f"Invalid number of bytes {len(memo)}")


def stream_plot_info_pk(
    pool_public_key: G1Element,
    farmer_public_key: G1Element,
    local_master_sk: PrivateKey,
):
    # There are two ways to stream plot info: with a pool public key, or with a pool contract puzzle hash.
    # This one streams the public key, into bytes
    data = bytes(pool_public_key) + bytes(farmer_public_key) + bytes(local_master_sk)
    assert len(data) == (48 + 48 + 32)
    return data


def stream_plot_info_ph(
    pool_contract_puzzle_hash: bytes32,
    farmer_public_key: G1Element,
    local_master_sk: PrivateKey,
):
    # There are two ways to stream plot info: with a pool public key, or with a pool contract puzzle hash.
    # This one streams the pool contract puzzle hash, into bytes
    data = pool_contract_puzzle_hash + bytes(farmer_public_key) + bytes(local_master_sk)
    assert len(data) == (32 + 48 + 32)
    return data


def find_duplicate_plot_IDs(all_filenames=None) -> None:
    if all_filenames is None:
        all_filenames = []
    plot_ids_set = set()
    duplicate_plot_ids = set()
    all_filenames_str: list[str] = []

    for filename in all_filenames:
        filename_str: str = str(filename)
        all_filenames_str.append(filename_str)
        filename_parts: list[str] = filename_str.split("-")
        plot_id: str = filename_parts[-1]
        # Skipped parsing and verifying plot ID for faster performance
        # Skipped checking K size for faster performance
        # Only checks end of filenames: 64 char plot ID + .plot = 69 characters
        if len(plot_id) == 69:
            if plot_id in plot_ids_set:
                duplicate_plot_ids.add(plot_id)
            else:
                plot_ids_set.add(plot_id)
        else:
            log.warning(f"{filename} does not end with -[64 char plot ID].plot")

    for plot_id in duplicate_plot_ids:
        log_message: str = plot_id + " found in multiple files:\n"
        duplicate_filenames: list[str] = [filename_str for filename_str in all_filenames_str if plot_id in filename_str]
        for filename_str in duplicate_filenames:
            log_message += "\t" + filename_str + "\n"
        log.warning(f"{log_message}")


def validate_plot_size(root_path: Path, k: int, override_k: bool) -> None:
    config = load_config(root_path, "config.yaml")
    min_k = config["min_mainnet_k_size"]
    if k < min_k and not override_k:
        raise ValueError(
            f"k={min_k} is the minimum size for farming.\n"
            "If you are testing and you want to use smaller size please add the --override-k flag."
        )
    elif k < 25 and override_k:
        raise ValueError("Error: The minimum k size allowed from the cli is k=25.")
