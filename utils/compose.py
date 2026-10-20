"""Read this API container's local image and Docker Compose deployment metadata."""

import os
import re
import socket

# Compose service this API is deployed as, used to find our own container when the
# hostname lookup fails (see own_container).
COMPOSE_SERVICE = "hummingbot-api"

# Compose stamps these on every container it creates; they say which project the API
# belongs to and which files were used to bring it up.
LABEL_PROJECT = "com.docker.compose.project"
LABEL_WORKING_DIR = "com.docker.compose.project.working_dir"
LABEL_CONFIG_FILES = "com.docker.compose.project.config_files"


def _container_id_from_mountinfo():
    """This process's own container id, read from the files Docker bind-mounts into it.

    Docker mounts /var/lib/docker/containers/<id>/{hostname,hosts,resolv.conf} into every
    container, so the id is in our own mount table whatever `hostname:` was set to.
    """
    try:
        with open("/proc/self/mountinfo") as f:
            match = re.search(r"/containers/([0-9a-f]{64})/", f.read())
    except OSError:
        return None
    return match.group(1) if match else None


def _in_container():
    return os.path.exists("/.dockerenv") or _container_id_from_mountinfo() is not None


def own_container(client):
    """Find the container this API process runs in.

    Docker sets a container's hostname to its own short id, which is how a process
    identifies itself from the inside; when the deployment overrides `hostname:` the id
    is still in the mount table. Only when both miss, and only from inside a container,
    fall back to the compose service label -- and only when exactly one container carries
    it. A process running from source on the host is not any container, and with two
    candidates picking one would report another deployment's image as ours:
    None is the honest answer in both cases.

    Returns:
        A docker-py Container, or None when this process cannot be placed.
    """
    for ref in (socket.gethostname(), _container_id_from_mountinfo()):
        if not ref:
            continue
        try:
            return client.containers.get(ref)
        except Exception:
            pass
    if not _in_container():
        return None
    try:
        matches = client.containers.list(filters={"label": f"com.docker.compose.service={COMPOSE_SERVICE}"})
    except Exception:
        return None
    return matches[0] if len(matches) == 1 else None


def _repository(ref):
    """The repository of an image reference, without tag, digest or the Docker Hub host.

    ``docker.io/example/api:latest`` and ``example/api@sha256:..``
    are both ``example/api``. A colon after the last slash is a tag; one
    before it is a registry port.
    """
    repo = (ref or "").split("@")[0]
    if ":" in repo.rsplit("/", 1)[-1]:
        repo = repo.rsplit(":", 1)[0]
    for host in ("docker.io/", "index.docker.io/"):
        if repo.startswith(host):
            repo = repo[len(host):]
    return repo


def describe_container(container):
    """Identity, image and compose provenance of a container.

    Returns:
        Dictionary with the container's id and name, the image reference it was started
        from, the image id, an available digest matching that image repository, and the
        three compose labels that say where the deployment lives. No registry is queried.
    """
    labels = container.labels or {}
    image = container.image
    image_ref = container.attrs.get("Config", {}).get("Image")
    digests = [
        digest for digest in ((image.attrs.get("RepoDigests") or []) if image is not None else [])
        if _repository(digest) == _repository(image_ref)
    ]
    return {
        "id": container.id,
        "name": container.name,
        "image": image_ref,
        "image_id": image.id if image is not None else None,
        "digest": digests[0] if digests else None,
        "compose_project": labels.get(LABEL_PROJECT),
        "compose_working_dir": labels.get(LABEL_WORKING_DIR),
        "compose_config_files": labels.get(LABEL_CONFIG_FILES),
    }
