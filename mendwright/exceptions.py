"""Shared exceptions for the self-healing system."""


class AttentionRequired(RuntimeError):
    """A selector could not be resolved to a unique visible element and no automatic
    recovery was available. Callers surface this for human attention; the heal
    coordinator intercepts it to attempt an inline fix first.

    Subclasses RuntimeError so tools that historically defined their own
    RuntimeError-based 'needs attention' exception can re-export this as a drop-in."""
