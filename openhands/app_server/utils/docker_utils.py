import os
from urllib.parse import urlparse, urlunparse

from openhands.utils.environment import is_running_in_docker


def is_container_using_host_network() -> bool:
    """Check if the container is using host networking mode.

    When using host networking, the container shares the host's network namespace,
    so localhost refers to the actual host. In this case, we should NOT replace
    localhost with host.docker.internal.

    Returns:
        True if OH_CONTAINER_HOST_NETWORK=true is set, False otherwise
    """
    return os.environ.get('OH_CONTAINER_HOST_NETWORK', '').lower() == 'true'


def replace_localhost_hostname_for_docker(
    url: str, replacement: str = 'host.docker.internal'
) -> str:
    """Replace localhost hostname in URL with the specified replacement when running in Docker.

    This function only performs the replacement when the code is running inside a Docker
    container AND not using host networking. When not running in Docker, or when using
    host networking mode, it returns the original URL unchanged.

    Only replaces the hostname if it's exactly 'localhost', preserving all other
    parts of the URL including port, path, query parameters, etc.

    Args:
        url: The URL to process
        replacement: The hostname to replace localhost with (default: 'host.docker.internal')

    Returns:
        URL with localhost hostname replaced if running in Docker (without host networking)
        and hostname is localhost, otherwise returns the original URL unchanged
    """
    if not is_running_in_docker():
        return url

    # When using host networking, the container shares the host's network namespace,
    # so localhost is already correct and host.docker.internal won't work
    if is_container_using_host_network():
        return url

    parsed = urlparse(url)
    if parsed.hostname == 'localhost':
        # Replace only the hostname part, preserving port and everything else
        netloc = parsed.netloc.replace('localhost', replacement, 1)
        return urlunparse(parsed._replace(netloc=netloc))
    return url
