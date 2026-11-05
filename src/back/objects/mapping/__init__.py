"""Mapping domain: entity/relationship mappings, R2RML, SQL preview, session merge."""

from back.objects.mapping.ChunkMappingCollector import ChunkMappingCollector
from back.objects.mapping.json_views import get_mapping_info
from back.objects.mapping.Mapping import Mapping
from back.objects.mapping.MetricViewBindings import MetricViewBindings

__all__ = ["ChunkMappingCollector", "Mapping", "MetricViewBindings", "get_mapping_info"]
