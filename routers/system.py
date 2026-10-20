import logging
import os
from importlib import metadata

import psutil
from fastapi import APIRouter, Depends

from config import settings
from deps import get_docker_service
from services.docker_service import DockerService
from utils.compose import describe_container, own_container
from version import VERSION

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System"], prefix="/system")

# When running inside Docker, mount the host's /proc and root filesystem into the
# container and point these env vars at the mount locations so psutil reports the
# HOST machine's metrics rather than the container's. They default to the local
# paths so the route also works when running outside a container.
#   HOST_PROC      -> e.g. /host/proc   (host /proc mounted read-only)
#   HOST_DISK_PATH -> e.g. /host/root   (host / mounted read-only)
HOST_PROC = os.environ.get("HOST_PROC", "/proc")
HOST_DISK_PATH = os.environ.get("HOST_DISK_PATH", "/")

# psutil reads CPU, memory and load-average data from this path. Setting it to the
# mounted host /proc makes those metrics reflect the host instead of the container.
psutil.PROCFS_PATH = HOST_PROC

# Prime cpu_percent so the first call returns a meaningful value rather than 0.0
# (psutil measures CPU utilization between successive calls).
psutil.cpu_percent(interval=None)


@router.get("/resources")
async def get_system_resources():
    """
    Get host machine CPU, RAM, and disk usage.

    Returns:
        Dictionary with current CPU, memory, and disk utilization for the host.
    """
    try:
        load_avg = psutil.getloadavg()
    except (OSError, AttributeError):
        # getloadavg is not available on every platform.
        load_avg = (0.0, 0.0, 0.0)

    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(HOST_DISK_PATH)

    return {
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "count_logical": psutil.cpu_count(logical=True),
            "count_physical": psutil.cpu_count(logical=False),
            "load_avg_1m": load_avg[0],
            "load_avg_5m": load_avg[1],
            "load_avg_15m": load_avg[2],
        },
        "memory": {
            "total": vm.total,
            "available": vm.available,
            "used": vm.used,
            "percent": vm.percent,
        },
        "disk": {
            # Display label only; the stats are for the filesystem containing
            # HOST_DISK_PATH (the host root partition).
            "mountpoint": "host root",
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
        },
    }


def _hummingbot_version():
    """The version of the hummingbot library this API runs against, or None if absent."""
    try:
        return metadata.version("hummingbot")
    except metadata.PackageNotFoundError:
        return None


@router.get("/info")
async def get_system_info(docker_service: DockerService = Depends(get_docker_service)):
    """
    Get the versions this API server runs and how its own container was deployed.

    Read-only, and never raises: when docker.sock is not mounted, the daemon is
    unreachable or this process cannot find its own container, the version fields are
    still reported and the container block is None.

    Returns:
        Dictionary with the API version, the hummingbot library version, the market-data
        tunables this process is running with, whether the Docker daemon could be
        reached and the API's own container (id, name, image, digest and compose labels).
        This endpoint never contacts an image registry or updates the deployment.
    """
    info = {
        "api_version": VERSION,
        "hummingbot_version": _hummingbot_version(),
        # The MARKET_DATA_* knobs as this process resolved them. Reported rather than
        # editable: they come from the env_file, which is not mounted into the
        # container, so the API cannot write its own .env -- a dashboard shows them and
        # says where to change them. Keep the numeric tuning contract; the cache's
        # filesystem location is local deployment configuration, not a dashboard knob.
        "market_data": settings.market_data.model_dump(exclude={"historical_cache_path"}),
        "docker_available": False,
        "container": None,
    }

    # DockerService leaves `client` unset when docker.from_env() failed at startup.
    client = getattr(docker_service, "client", None)
    if client is None:
        return info
    try:
        client.ping()
    except Exception as e:
        logger.warning(f"Docker daemon unreachable while reporting system info: {e}")
        return info
    info["docker_available"] = True

    container = own_container(client)
    if container is None:
        logger.warning("Could not identify the API's own container while reporting system info")
        return info

    try:
        info["container"] = describe_container(container)
    except Exception as e:
        logger.warning(f"Could not inspect the API's own container while reporting system info: {e}")
        return info

    return info
