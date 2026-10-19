"""Graph DB package constants."""

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

# Default row cap for reverse-edge lookups (``find_subjects_by_predicate_objects``),
# e.g. one GraphQL inverse field resolved for a batch of target entities.
REVERSE_LOOKUP_MAX_ROWS = 5000
