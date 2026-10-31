"""The gallery's stock Python runtime, cached once per image id."""
import os
from pathlib import Path

from mmw.core.container_image import ImageResult, container_image

# MMW_DOCKER: the docker command (tests set one that does not exist, so a
# CLI run in a subprocess gets no Docker either).
DOCKER = os.environ.get("MMW_DOCKER") or "docker"
IMAGE = "python:3.13-slim"
PLATFORM = "linux/amd64"
TAR_NAME = "python-3.13-slim-linux-amd64.tar"


def gallery_image(cache: Path, *, progress=None, stop_event=None) -> ImageResult:
    return container_image(cache, image=IMAGE, platform=PLATFORM, docker=DOCKER,
                           label="Gallery", progress=progress, stop_event=stop_event)
