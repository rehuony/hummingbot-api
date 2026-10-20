"""Read this API container's local image and Docker Compose deployment metadata."""

import os
import re
import socket

# Compose stamps these on every container it creates; they say which project the API
# belongs to and which files were used to bring it up.
LABEL_PROJECT = "com.docker.compose.project"
LABEL_WORKING_DIR = "com.docker.compose.project.working_dir"
LABEL_CONFIG_FILES = "com.docker.compose.project.config_files"
SOURCE_REPOSITORY = "https://github.com/rehuony/hummingbot-api"


def _build_metadata(image_attrs):
    """Read API provenance, excluding unrelated labels inherited from a base image."""
    labels = (image_attrs.get("Config") or {}).get("Labels") or {}
    source = labels.get("org.opencontainers.image.source")
    if source != SOURCE_REPOSITORY:
        return dict.fromkeys(("source", "revision", "branch", "created", "dirty"))
    dirty = labels.get("io.rehuony.hummingbot-api.dirty")
    return {
        "source": source,
        "revision": labels.get("org.opencontainers.image.revision") or None,
        "branch": labels.get("io.rehuony.hummingbot-api.branch") or None,
        "created": labels.get("org.opencontainers.image.created") or None,
        "dirty": {"true": True, "false": False}.get(dirty),
    }


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

    The mount table identifies this process even when its hostname is overridden.
    Without that evidence, only an in-container Docker ID hostname is considered,
    and the returned container must have that ID. Names and Compose service labels
    can identify another deployment and are never sufficient evidence.

    Returns:
        A docker-py Container, or None when this process cannot be placed.
    """
    if not _in_container():
        return None
    ref = _container_id_from_mountinfo()
    if ref is None:
        ref = socket.gethostname()
        if not re.fullmatch(r"[0-9a-f]{12,64}", ref):
            return None
    try:
        container = client.containers.get(ref)
    except Exception:
        return None
    container_id = getattr(container, "id", None)
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        return None
    return container if container_id.startswith(ref) else None


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
        from, the image id, an available digest matching that image repository, the
        running image's creation time and source-build labels, and its Compose metadata.
        No registry is queried.
    """
    labels = container.labels or {}
    # docker-py resolves this by the container's immutable image ID, not its mutable tag.
    image = container.image
    image_attrs = image.attrs if image is not None else {}
    image_ref = container.attrs.get("Config", {}).get("Image")
    digests = [
        digest for digest in (image_attrs.get("RepoDigests") or [])
        if _repository(digest) == _repository(image_ref)
    ]
    return {
        "id": container.id,
        "name": container.name,
        "image": image_ref,
        "image_id": image.id if image is not None else None,
        "digest": digests[0] if digests else None,
        "image_created": image_attrs.get("Created"),
        "build": _build_metadata(image_attrs),
        "compose_project": labels.get(LABEL_PROJECT),
        "compose_working_dir": labels.get(LABEL_WORKING_DIR),
        "compose_config_files": labels.get(LABEL_CONFIG_FILES),
    }
