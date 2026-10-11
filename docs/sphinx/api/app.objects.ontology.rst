``back.objects.ontology`` -- Ontology management (domain)
=========================================================

Ontology facade
---------------

.. automodule:: back.objects.ontology.Ontology
   :members:
   :undoc-members:
   :show-inheritance:

Class model
-----------

.. automodule:: back.objects.ontology.ClassModel
   :members:
   :undoc-members:
   :show-inheritance:

Editor
------

.. automodule:: back.objects.ontology.Editor
   :members:
   :undoc-members:
   :show-inheritance:

Entity import
-------------

.. automodule:: back.objects.ontology.EntityImport
   :members:
   :undoc-members:
   :show-inheritance:

Import
------

.. automodule:: back.objects.ontology.Import
   :members:
   :undoc-members:
   :show-inheritance:

Owl
---

.. automodule:: back.objects.ontology.Owl
   :members:
   :undoc-members:
   :show-inheritance:

Rules
-----

.. automodule:: back.objects.ontology.Rules
   :members:
   :undoc-members:
   :show-inheritance:

Groups
------

.. automodule:: back.objects.ontology.Groups
   :members:
   :undoc-members:
   :show-inheritance:

JSON views
----------

.. automodule:: back.objects.ontology.JsonViews
   :members:
   :undoc-members:
   :show-inheritance:

Generate draft/contract layer (staged Generate)
------------------------------------------------

Durable draft/entity model, source fingerprinting, and the validation
primitives consumed by the staged agent entry points
(``agents.agent_owl_generator.staged``) and the async workflow below.

.. automodule:: back.objects.ontology.generate.GenerateDraft
   :members:
   :undoc-members:
   :show-inheritance:

Generate workflow (staged Generate)
-------------------------------------

Orchestrates detect -> human review -> checkpointed relations/attributes/
axioms completion -> append-only merge, on top of ``GenerateDraft`` and the
staged agent entry points; exposed via the ``POST /ontology/wizard/
generate/detect``, ``GET/POST /ontology/wizard/generate/draft(/update|
/discard)``, and ``POST /ontology/wizard/generate/complete`` routes.

.. automodule:: back.objects.ontology.generate.GenerateWorkflow
   :members:
   :undoc-members:
   :show-inheritance:
