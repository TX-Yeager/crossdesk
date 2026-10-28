"""Application-facing API backed by the permanent, preloaded host verifier.

No download server or account is required. A user imports a publisher-signed
``.crossdesk-update`` file, closes input sharing, then restarts the same host.
"""
from trustedhost import HOST_API, UpdateError, UpdateManager, runtime_info


def confirm_started(state_dir):
    """Call once after App initialization; never confirms an unstarted update."""
    UpdateManager(state_dir).confirm_started()


__all__ = ["HOST_API", "UpdateError", "UpdateManager", "confirm_started", "runtime_info"]
