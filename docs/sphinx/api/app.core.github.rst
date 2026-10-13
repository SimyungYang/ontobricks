``back.core.github`` -- GitHub REST Client
==========================================

Minimal client over the GitHub Git Data API used by the registry GitHub
mirror (``back.objects.registry.gitsync``). A batch of file writes and
deletions lands as one atomic commit; no ``git`` binary or clone is needed.

.. automodule:: back.core.github.GitHubClient
   :members:
   :undoc-members:
   :show-inheritance:

.. automodule:: back.core.github.constants
   :members:
   :undoc-members:
