"""Unity Catalog integration — metadata, volumes, identifier safety, domain I/O."""

from .constants import FS_FILES_PATH, FS_DIRS_PATH
from .identifiers import (
    UC_IDENTIFIER_RE,
    quote_uc_fqn,
    quote_uc_identifier,
    validate_uc_identifier,
)
from .VolumeFileService import VolumeFileService
from .UnityCatalog import UnityCatalog
from .MetadataService import MetadataService
from .UCDomainIO import UCDomainIO
from .MetricViewDefinition import MetricViewDefinition
from .GlossaryConnector import GlossaryConnector
from .NotAvailableGlossaryConnector import NotAvailableGlossaryConnector

__all__ = [
    "UnityCatalog",
    "UCDomainIO",
    "VolumeFileService",
    "MetadataService",
    "MetricViewDefinition",
    "GlossaryConnector",
    "NotAvailableGlossaryConnector",
    "FS_FILES_PATH",
    "FS_DIRS_PATH",
    "UC_IDENTIFIER_RE",
    "quote_uc_fqn",
    "quote_uc_identifier",
    "validate_uc_identifier",
]
