"""The gallery's stock Python runtime, cached once per image id."""
from pathlib import Path

from mmw.core.container_image import ImageResult, container_image

DOCKER = "docker"
IMAGE = "python:3.13-slim"
PLATFORM = "linux/amd64"
TAR_NAME = "python-3.13-slim-linux-amd64.tar"


def gallery_image(cache: Path, *, progress=None, stop_event=None) -> ImageResult:
    return container_image(cache, image=IMAGE, platform=PLATFORM, docker=DOCKER,
                           label="Gallery", progress=progress, stop_event=stop_event)
