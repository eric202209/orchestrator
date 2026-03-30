"""Uvicorn production configuration with timeouts and performance optimizations."""

from uvicorn.config import Config
from uvicorn.main import Server

# Production-ready timeout configuration
CONFIG = {
    # Network settings
    "host": "0.0.0.0",
    "port": 8080,
    # Timeout settings (in seconds)
    "timeout_keep_alive": 5,  # Keep-alive timeout (default: 5)
    "limit_concurrency": 200,  # Max concurrent connections
    "backlog": 2048,  # Max pending connections
    # Request handling timeouts
    "timeout_graceful_shutdown": 30,  # Grace period for shutdown (30s)
    # Proxy settings (for behind reverse proxy/load balancer)
    "proxy_headers": True,  # Trust X-Forwarded-* headers
    "forwarded_allow_ips": "*",  # Allow all IPs (adjust for production)
    # Logging
    "log_level": "info",
    "access_log": True,
    # Performance optimizations
    "workers": 1,  # Single worker for simplicity (use gunicorn+uvicorn for multiple)
    "loop": "asyncio",
    "http": "httptools",  # Faster HTTP parser
    # File handling
    "reload": False,  # Disable in production
}


def get_config():
    """Get uvicorn configuration dict."""
    return CONFIG


def create_server(config_dict=None):
    """Create a uvicorn server with the given config."""
    config = Config(**(config_dict or CONFIG))
    return Server(config=config)
