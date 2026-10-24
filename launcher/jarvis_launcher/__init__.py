"""JARVIS launcher: the front door of the JARVIS container.

It owns port 8642. Before JARVIS knows its Hermes it serves the welcome screen (install Hermes for me, or connect the
Hermes I already have). Afterwards it forwards everything outside ``/jarvis/`` to that Hermes, whose ``jarvis``
connector plugin serves the dashboard at ``/vesper``. When JARVIS installed Hermes itself ("managed"), the launcher also
runs its gateway and keeps it and the connector up to date.
"""

JARVIS_VERSION = "2.1.1"
