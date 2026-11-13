"""Package exceptions.

Error messages intentionally never include PII values.
"""


class AuLedgerError(Exception):
    """Base class for expected application errors."""


class ConfigError(AuLedgerError):
    """Configuration or key material is missing or invalid."""


class CryptoError(AuLedgerError):
    """Encryption, decryption or key validation failed."""


class SchemaError(AuLedgerError):
    """Workflow schema or disposition metadata is invalid."""


class TransactionError(AuLedgerError):
    """A database transaction could not be started or completed."""


class IngestError(AuLedgerError):
    """A lead file or ingestion run could not be processed safely."""
