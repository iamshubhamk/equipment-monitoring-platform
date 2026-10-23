class PermanentError(Exception):
    """Retrying cannot help (e.g. the referenced batch doesn't exist): dead-letter now."""


class TransientError(Exception):
    """Retrying may help (e.g. a temporary outage). Retried with backoff.

    Any exception that is not a PermanentError is treated the same way, so an unexpected
    bug is retried a few times and then dead-lettered rather than lost.
    """
